#!/usr/bin/env python3
"""Generate drafts for every deck listed in decks/services.json.

decks/services.json:
    [
      {"deck": "s3.pptx", "id": "s3", "name": "Amazon S3", "category": "Storage"},
      {"deck": ["ebs.pptx", "ebs-deep-dive.pptx"], "id": "ebs", "name": "Amazon EBS", "category": "Storage"},
      ...
    ]

Each deck is generated, fact-checked by a second Bedrock pass, and questions that pass are approved
and published into content/questions/ automatically. Flagged ones stay in content/drafts/ for
`python tools/review.py <id>`. Use --review to skip auto-approval and review everything yourself.

A deck is skipped once it has produced questions, so re-running only picks up new decks.
Use --only to run specific services, --force to re-run decks that already have questions
(new questions are added; existing ones are never duplicated).

Usage:
    python tools/batch.py --list                  # what would run, and why others are skipped
    python tools/batch.py --dry-run               # build every request, no Bedrock calls
    python tools/batch.py                         # generate drafts for all new decks
    python tools/batch.py --only s3 dynamodb      # just these
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
DECKS = ROOT / "decks"
MANIFEST = DECKS / "services.json"
CATEGORIES = set(json.loads((ROOT / "schema" / "question-bank.schema.json").read_text(encoding="utf-8"))
                 ["properties"]["service"]["properties"]["category"]["enum"])


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        sys.exit(f"missing {MANIFEST.relative_to(ROOT)}; see the docstring in tools/batch.py")
    entries = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    problems, ids = [], set()
    for e in entries:
        if isinstance(e.get("deck"), str):
            e["deck"] = [e["deck"]]
        for k in ("deck", "id", "name", "category"):
            if not e.get(k):
                problems.append(f"{e}: missing '{k}'")
        if e.get("category") and e["category"] not in CATEGORIES:
            problems.append(f"{e['id']}: category '{e['category']}' must be one of {sorted(CATEGORIES)}")
        if e.get("id") in ids:
            problems.append(f"duplicate id '{e['id']}'")
        ids.add(e.get("id"))
        for d in e.get("deck") or []:
            if not (DECKS / d).is_file():
                problems.append(f"{e.get('id')}: deck not found: decks/{d}")
    if problems:
        sys.exit("services.json problems:\n  - " + "\n  - ".join(problems))
    return entries


def bank_questions(sid: str) -> list[dict]:
    out = []
    for folder in ("questions", "drafts"):
        p = ROOT / "content" / folder / f"{sid}.json"
        if p.exists():
            out += json.loads(p.read_text(encoding="utf-8"))["questions"]
    return out


def deck_done(sid: str, deck: str) -> int | None:
    """Questions already produced from this deck, or None if it hasn't been run.
    Banks made before decks were tracked count as done for every deck."""
    qs = bank_questions(sid)
    if not qs:
        return None
    if not any("sourceDeck" in q for q in qs):
        return len(qs)
    n = sum(q.get("sourceDeck") == deck for q in qs)
    return n or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", metavar="ID", help="run just these service ids")
    ap.add_argument("--force", action="store_true", help="run even if the service already has questions")
    ap.add_argument("--list", action="store_true", help="show the plan and exit")
    ap.add_argument("--dry-run", action="store_true", help="pass --dry-run to generate.py")
    ap.add_argument("--deny-terms", default="deny-terms.txt")
    ap.add_argument("--review", action="store_true",
                    help="don't auto-approve: everything stays a draft for tools/review.py")
    args, args.extra = ap.parse_known_args()  # anything else is passed to generate.py

    entries = load_manifest()
    if args.only:
        unknown = set(args.only) - {e["id"] for e in entries}
        if unknown:
            sys.exit(f"not in services.json: {sorted(unknown)}")
        entries = [e for e in entries if e["id"] in args.only]

    plan = []
    for e in entries:
        for deck in e["deck"]:
            done = deck_done(e["id"], deck)
            run = args.force or done is None
            plan.append((e, deck, run))
            print(f"  {'RUN ' if run else 'skip'}  {e['id']:<16} {deck:<34} "
                  f"{'' if done is None else f'already has {done} questions'}")
    todo = [(e, d) for e, d, run in plan if run]
    print(f"\n{len(todo)} deck(s) to run, {len(plan) - len(todo)} skipped"
          + ("" if args.review else "; questions that pass the fact-check are approved and published"))
    if args.list or not todo:
        return 0

    deny = ROOT / args.deny_terms
    results = []
    for n, (e, deck) in enumerate(todo, 1):
        print(f"\n===== [{n}/{len(todo)}] {e['name']} ({deck}) =====", flush=True)
        cmd = [sys.executable, str(ROOT / "pipeline" / "generate.py"), str(DECKS / deck),
               "--service", e["id"], "--name", e["name"], "--category", e["category"]]
        if e.get("symbol"):
            cmd += ["--symbol", e["symbol"]]
        if deny.exists():
            cmd += ["--deny-terms", str(deny)]
        if not args.review:
            cmd.append("--auto-approve")
        if args.dry_run:
            cmd.append("--dry-run")
        cmd += args.extra
        start = time.time()
        rc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL if args.dry_run else None).returncode
        results.append((e, deck, rc, time.time() - start))

    if not args.dry_run and not args.review:
        print("\n===== publishing approved questions =====")
        for sid in dict.fromkeys(e["id"] for e, *_ in results):
            subprocess.run([sys.executable, str(ROOT / "tools" / "review.py"), sid, "--promote"], cwd=ROOT)

    print("\n===== summary =====")
    labels = {0: "ok", 1: "nothing usable", 2: "error", 3: "partial (some chunks failed)"}
    for e, deck, rc, secs in results:
        print(f"  {labels.get(rc, f'exit {rc}'):<30} {e['id']:<16} {deck:<34} {secs:5.0f}s")
    if not args.dry_run:
        print()
        for sid in dict.fromkeys(e["id"] for e, *_ in results):
            qs = bank_questions(sid)
            pub = sum(q["status"] == "approved" for q in qs)
            wait = sum(q["status"] == "draft" for q in qs)
            print(f"  {sid:<16} {pub:>3} published   {wait:>3} waiting for you"
                  + (f"  ->  python tools\\review.py {sid}" if wait else ""))
    failed = sorted({e["id"] for e, _, rc, _ in results if rc != 0})
    if failed:
        # decks that produced nothing are picked up again automatically; --force would redo the good ones too
        print(f"\nsome decks failed; re-run with: python tools\\batch.py --only {' '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
