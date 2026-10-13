#!/usr/bin/env python3
"""Review generated drafts, then promote approved ones into the published bank.

Usage:
    python tools/review.py s3              # walk through drafts: approve / reject / edit / skip
    python tools/review.py s3 --list       # counts only
    python tools/review.py s3 --promote    # move approved drafts into content/questions/s3.json

Rejected questions stay in the drafts file on purpose: generate.py de-duplicates against
them, so a re-run won't hand you the same bad question again.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from validate import VALIDATOR, semantic_errors  # noqa: E402

DRAFTS = ROOT / "content" / "drafts"
PUBLISHED = ROOT / "content" / "questions"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, bank: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bank, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def question_errors(q: dict, service: dict) -> list[str]:
    bank = {"schemaVersion": "1.0", "service": service, "questions": [q]}
    errs = [e.message for e in VALIDATOR.iter_errors(bank)]
    return errs or semantic_errors(bank, Path(f"{service['id']}.json"), approved_only=False)


def show(q: dict, pos: int, total: int) -> None:
    print("\n" + "=" * 72)
    print(f"[{pos}/{total}] {q['id']}  {q['type']} | {q.get('style', '-')} | {q['difficulty']}"
          f" | slide {q.get('slideRef', '-')}")
    print("-" * 72)
    print(q["prompt"] + "\n")
    for o in q["options"]:
        mark = "*" if o["id"] in q["answer"] else " "
        print(f"  {mark} {o['id']}) {o['text']}")
    print(f"\n  Why: {q['explanation']}")
    if q.get("docUrl"):
        print(f"  Doc: {q['docUrl']}")
    if q.get("reviewNote"):
        print(f"\n  ! {q['reviewNote']}")


def edit(q: dict, service: dict) -> dict | None:
    """Open the question JSON in an editor; return the edited question if valid."""
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(q, f, indent=2, ensure_ascii=False)
        tmp = f.name
    try:
        while True:
            try:
                subprocess.run(shlex.split(editor, posix=os.name != "nt") + [tmp], check=False)
            except FileNotFoundError:
                print(f"  editor not found: {editor!r} (set EDITOR, e.g. 'code --wait')")
                return None
            try:
                edited = json.loads(Path(tmp).read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"  invalid JSON: {e}")
            else:
                edited["id"], edited["status"] = q["id"], "draft"  # id and status aren't editable here
                errs = question_errors(edited, service)
                if not errs:
                    return edited
                print("  edit has problems:\n    - " + "\n    - ".join(errs))
            if input("  re-open editor? [y/N] ").strip().lower() != "y":
                return None
    finally:
        os.unlink(tmp)


def review(path: Path) -> None:
    bank = load(path)
    pending = [q for q in bank["questions"] if q["status"] == "draft"]
    if not pending:
        print("no drafts waiting for review")
        return
    print(f"{len(pending)} draft(s) to review. Keys: a=approve  r=reject  e=edit  s=skip  q=quit")
    for i, q in enumerate(pending, 1):
        show(q, i, len(pending))
        while True:
            choice = input("\n  [a/r/e/s/q] > ").strip().lower()
            if choice == "a":
                q["status"] = "approved"
                break
            if choice == "r":
                q["status"] = "rejected"
                break
            if choice == "s":
                break
            if choice == "q":
                save(path, bank)
                print("saved; progress kept")
                return
            if choice == "e":
                edited = edit(q, bank["service"])
                if edited:
                    q.clear()
                    q.update(edited)
                    show(q, i, len(pending))
                    print("  (edited; now approve, reject or skip)")
                continue
            print("  type a, r, e, s or q")
        save(path, bank)  # after every decision, so a crash loses nothing
    print("\ndone. " + summary(bank))


def summary(bank: dict) -> str:
    c = Counter(q["status"] for q in bank["questions"])
    return f"draft {c['draft']}, approved {c['approved']}, rejected {c['rejected']}"


def promote(service_id: str, drafts_path: Path) -> int:
    drafts = load(drafts_path)
    approved = [q for q in drafts["questions"] if q["status"] == "approved"]
    if not approved:
        print("nothing approved to promote")
        return 0
    pub_path = PUBLISHED / f"{service_id}.json"
    pub = load(pub_path) if pub_path.exists() else {
        "schemaVersion": "1.0", "service": drafts["service"], "questions": []}
    taken = {q["id"] for q in pub["questions"]}
    clash = [q["id"] for q in approved if q["id"] in taken]
    if clash:
        print(f"refusing: ids already published: {clash}")
        return 1

    candidate = {**pub, "questions": pub["questions"] + approved}
    errs = [e.message for e in VALIDATOR.iter_errors(candidate)]
    errs = errs or semantic_errors(candidate, pub_path, approved_only=True)
    if errs:
        print("refusing: result would not validate:\n  - " + "\n  - ".join(errs))
        return 1

    save(pub_path, candidate)
    drafts["questions"] = [q for q in drafts["questions"] if q["status"] != "approved"]
    save(drafts_path, drafts)
    print(f"promoted {len(approved)} -> {pub_path.relative_to(ROOT)} "
          f"(now {len(candidate['questions'])} published)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("service", help="service id, e.g. s3")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true")
    g.add_argument("--promote", action="store_true")
    args = ap.parse_args()

    path = DRAFTS / f"{args.service}.json"
    if not path.exists():
        print(f"no drafts at {path.relative_to(ROOT)}; run pipeline/generate.py first")
        return 1
    if args.list:
        print(f"{args.service}: " + summary(load(path)))
        return 0
    if args.promote:
        return promote(args.service, path)
    review(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
