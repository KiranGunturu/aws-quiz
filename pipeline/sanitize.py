#!/usr/bin/env python3
"""Make a de-branded copy of a deck for rendering: no logos, footers, slide numbers or branded backgrounds.

The original deck is never modified. What gets removed:
  master + layouts   every picture and non-placeholder decoration (logos, background art, footer text
                     boxes), plus footer / date / slide-number placeholders
  every slide        footer / date / slide-number placeholders; pictures that repeat on many slides
                     (a logo pasted per slide); small pictures in the footer band; text boxes whose
                     text is a line repeated across most slides (banners like "Confidential")
  backgrounds        replaced with plain white on master, layouts and slides

Usage:
    python pipeline/sanitize.py decks/lambda.pptx build/lambda.clean.pptx
"""
import hashlib
import sys
from collections import Counter
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import in_edge_band  # noqa: E402

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
CHROME = {PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.SLIDE_NUMBER}
WHITE_BG = (f'<p:bg xmlns:p="{NS["p"]}" xmlns:a="{NS["a"]}"><p:bgPr><a:solidFill>'
            '<a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>')


def _remove(shape) -> None:
    el = shape.element
    el.getparent().remove(el)


def _white_background(part_with_csld) -> None:
    csld = part_with_csld.element.find("p:cSld", NS)
    old = csld.find("p:bg", NS)
    if old is not None:
        csld.remove(old)
    csld.insert(0, etree.fromstring(WHITE_BG))


def _is_chrome_placeholder(shape) -> bool:
    if not shape.is_placeholder:
        return False
    try:
        return shape.placeholder_format.type in CHROME
    except ValueError:
        return False


def _picture_hash(shape) -> str | None:
    try:
        return hashlib.sha1(shape.image.blob).hexdigest()
    except Exception:  # linked or missing image
        return None


def _walk(shapes):
    for sh in shapes:
        yield sh
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk(sh.shapes)


def _fix_negative_extents(part_element) -> int:
    """PowerPoint refuses files with negative widths/heights (e.g. a connector drawn right-to-left by a
    script). Rewrite each as a positive size, shifted and flipped, so the drawing is unchanged."""
    fixed = 0
    for xfrm in part_element.iter(f"{{{NS['a']}}}xfrm", f"{{{NS['p']}}}xfrm"):
        off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
        if off is None or ext is None:
            continue
        for size, pos, flip in (("cx", "x", "flipH"), ("cy", "y", "flipV")):
            v = int(ext.get(size, "0"))
            if v < 0:
                ext.set(size, str(-v))
                off.set(pos, str(int(off.get(pos, "0")) + v))
                if xfrm.tag == f"{{{NS['a']}}}xfrm":  # graphicFrame's p:xfrm has no flip attributes
                    xfrm.set(flip, "0" if xfrm.get(flip) in ("1", "true") else "1")
                fixed += 1
        ch = xfrm.find("a:chExt", NS)
        if ch is not None:
            for size in ("cx", "cy"):
                if int(ch.get(size, "0")) < 0:
                    ch.set(size, str(-int(ch.get(size))))
                    fixed += 1
    return fixed


def clean_copy(src: Path, dest: Path, min_share: float = 0.4) -> dict:
    prs = Presentation(str(src))
    W, H = prs.slide_width, prs.slide_height
    report = Counter()

    # 1. masters and layouts: keep only content placeholders
    for master in prs.slide_masters:
        for part in [master, *master.slide_layouts]:
            for sh in list(part.shapes):
                if not sh.is_placeholder or _is_chrome_placeholder(sh):
                    _remove(sh)
                    report["master/layout decorations"] += 1
            _white_background(part)

    # 2. find things repeated across slides
    slides = list(prs.slides)
    n = len(slides)
    pic_counts, text_counts = Counter(), Counter()
    for s in slides:
        pic_counts.update({h for sh in _walk(s.shapes)
                           if sh.shape_type == MSO_SHAPE_TYPE.PICTURE and (h := _picture_hash(sh))})
        text_counts.update({sh.text_frame.text.strip() for sh in s.shapes
                            if sh.has_text_frame and sh.text_frame.text.strip() and in_edge_band(sh, H)})
    threshold = max(3, min_share * n) if n >= 5 else float("inf")
    repeated_pics = {h for h, c in pic_counts.items() if c >= threshold}
    repeated_text = {t for t, c in text_counts.items() if c >= threshold}

    # 3. slides
    for s in slides:
        for sh in list(s.shapes):  # top-level only; removing inside groups risks breaking a diagram
            if _is_chrome_placeholder(sh):
                _remove(sh); report["footer/date/number placeholders"] += 1
                continue
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                in_footer_band = (sh.top or 0) > 0.88 * H and (sh.width or 0) < 0.25 * W
                if _picture_hash(sh) in repeated_pics or in_footer_band:
                    _remove(sh); report["logo pictures"] += 1
                    continue
            if (not sh.is_placeholder and sh.has_text_frame and in_edge_band(sh, H)
                    and sh.text_frame.text.strip() in repeated_text):
                _remove(sh); report["repeated banner text boxes"] += 1
                continue
        _white_background(s)

    for master in prs.slide_masters:
        for part in [master, *master.slide_layouts]:
            report["negative sizes repaired"] += _fix_negative_extents(part.element)
    for s in slides:
        report["negative sizes repaired"] += _fix_negative_extents(s.element)
    if not report["negative sizes repaired"]:
        del report["negative sizes repaired"]

    dest.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(dest))
    return dict(report)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    rep = clean_copy(Path(sys.argv[1]), Path(sys.argv[2]))
    for k, v in rep.items():
        print(f"  removed {v} {k}")
    print(f"wrote {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
