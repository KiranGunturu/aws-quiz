#!/usr/bin/env python3
"""Render deck slides to PNG so diagrams, SmartArt and screenshots can be sent to the model.

Backends (auto-detected, in this order):
  powerpoint   Windows + PowerPoint installed + `pip install pywin32`   (most faithful)
  libreoffice  `soffice` on PATH (or LIBREOFFICE_PATH) + `pip install pymupdf`

Logos, footers, slide numbers and branded backgrounds are stripped first (see sanitize.py), so the
model never sees them; pass --keep-branding to render the deck as-is.

Renders are cached in build/renders/<deck>/ and reused until the deck changes.

Usage:
    python pipeline/render.py decks/lambda.pptx            # render all slides
    python pipeline/render.py decks/lambda.pptx --slides 7 26 42
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from sanitize import clean_copy  # noqa: E402

SANITIZE_VERSION = "3"
CACHE = ROOT / "build" / "renders"
WIDTH = 1568  # Claude downsizes anything with a longer edge, so rendering larger only wastes bytes


class RenderError(Exception):
    pass


def _cache_dir(deck: Path) -> Path:
    return CACHE / deck.stem


def _stamp(deck: Path, clean: bool) -> str:
    return f"{deck.stat().st_mtime_ns}:{'clean' + SANITIZE_VERSION if clean else 'raw'}"


def _cached(deck: Path, slides: list[int], clean: bool) -> dict[int, Path] | None:
    d = _cache_dir(deck)
    stamp = d / ".source-mtime"
    if not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != _stamp(deck, clean):
        return None
    out = {n: d / f"slide-{n:03d}.png" for n in slides}
    return out if all(p.exists() for p in out.values()) else None


def _soffice() -> str | None:
    env = os.environ.get("LIBREOFFICE_PATH")
    if env and Path(env).exists():
        return env
    for name in ("soffice", "libreoffice"):
        if shutil.which(name):
            return shutil.which(name)
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
            p = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if p.exists():
                return str(p)
    return None


def _render_powerpoint(deck: Path, out_dir: Path) -> None:
    import win32com.client  # pywin32
    app = win32com.client.Dispatch("PowerPoint.Application")
    was_open = app.Presentations.Count > 0
    pres = app.Presentations.Open(str(deck.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
    try:
        ratio = pres.PageSetup.SlideHeight / pres.PageSetup.SlideWidth
        for i in range(1, pres.Slides.Count + 1):
            pres.Slides(i).Export(str((out_dir / f"slide-{i:03d}.png").resolve()), "PNG",
                                  WIDTH, int(WIDTH * ratio))
    finally:
        pres.Close()
        if not was_open and app.Presentations.Count == 0:
            app.Quit()


def _render_libreoffice(deck: Path, out_dir: Path, soffice: str) -> None:
    try:
        import pymupdf
    except ImportError as e:
        raise RenderError("LibreOffice backend needs PyMuPDF: pip install pymupdf") from e
    with tempfile.TemporaryDirectory() as tmp:
        # a private profile dir avoids clashing with a LibreOffice window the user has open
        profile = Path(tmp, "profile").resolve().as_uri()
        r = subprocess.run([soffice, f"-env:UserInstallation={profile}", "--headless",
                            "--convert-to", "pdf", "--outdir", tmp, str(deck.resolve())],
                           capture_output=True, text=True, timeout=600)
        pdf = Path(tmp) / f"{deck.stem}.pdf"
        if not pdf.exists():
            raise RenderError(f"LibreOffice could not convert the deck: {r.stderr.strip() or r.stdout.strip()}")
        doc = pymupdf.open(pdf)
        for i, page in enumerate(doc, 1):
            zoom = WIDTH / page.rect.width
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(out_dir / f"slide-{i:03d}.png")
        doc.close()


def available_backend() -> str | None:
    if os.name == "nt":
        try:
            import win32com.client
            win32com.client.Dispatch("PowerPoint.Application")
            return "powerpoint"
        except Exception:
            pass
    return "libreoffice" if _soffice() else None


def render(deck: Path, slides: list[int], backend: str | None = None,
           clean: bool = True) -> dict[int, Path]:
    """Return {slide_number: png_path} for the requested slides, rendering the deck if needed."""
    hit = _cached(deck, slides, clean)
    if hit:
        return hit
    backend = backend or available_backend()
    if backend is None:
        raise RenderError(
            "no slide renderer found. On Windows with PowerPoint: pip install pywin32. "
            "Otherwise install LibreOffice and: pip install pymupdf. "
            "Or run generate.py with --images none to skip diagrams.")
    d = _cache_dir(deck)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        attempts = []  # (label, source file, backend), tried in order until one works
        if clean:
            cleaned = Path(tmp) / deck.name
            removed = clean_copy(deck, cleaned)
            print("  stripped before rendering: " +
                  (", ".join(f"{v} {k}" for k, v in removed.items()) or "nothing found"), file=sys.stderr)
            attempts.append(("cleaned copy", cleaned, backend))
            if backend == "powerpoint" and _soffice():
                attempts.append(("cleaned copy", cleaned, "libreoffice"))
        attempts.append(("original deck" if clean else "deck", deck, backend))

        errors = []
        for label, source, be in attempts:
            print(f"  rendering {label} of {deck.name} with {be}...", file=sys.stderr)
            try:
                if be == "powerpoint":
                    _render_powerpoint(source, d)
                else:
                    _render_libreoffice(source, d, _soffice())
                break
            except Exception as e:  # COM errors, LibreOffice failures
                errors.append(f"{label} with {be}: {e}")
                print(f"    failed: {e}", file=sys.stderr)
                for f in d.glob("slide-*.png"):
                    f.unlink()
                if label == "cleaned copy":
                    keep = d / "cleaned-copy-that-failed.pptx"
                    shutil.copyfile(source, keep)  # kept for diagnosis
        else:
            raise RenderError("could not render the deck:\n    " + "\n    ".join(errors))
        if clean and label != "cleaned copy":
            print("  warning: rendered the ORIGINAL deck, so logos and footers are visible in these images; "
                  "the prompt tells the model to ignore them", file=sys.stderr)
    (d / ".source-mtime").write_text(_stamp(deck, clean), encoding="utf-8")
    out = {n: d / f"slide-{n:03d}.png" for n in slides}
    missing = [n for n, p in out.items() if not p.exists()]
    if missing:
        raise RenderError(f"renderer produced no image for slide(s) {missing}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", type=Path)
    ap.add_argument("--slides", type=int, nargs="*")
    ap.add_argument("--backend", choices=["powerpoint", "libreoffice"])
    ap.add_argument("--keep-branding", action="store_true", help="don't strip logos/footers first")
    args = ap.parse_args()
    from pptx import Presentation
    count = len(Presentation(str(args.deck)).slides)
    try:
        out = render(args.deck, args.slides or list(range(1, count + 1)), args.backend,
                     clean=not args.keep_branding)
    except RenderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{len(out)} slide image(s) in {_cache_dir(args.deck).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
