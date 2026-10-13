#!/usr/bin/env python3
"""Validate question bank files against the schema plus rules JSON Schema can't express.

Usage:
    python tools/validate.py                      # all files in content/questions/
    python tools/validate.py content/questions/lambda.json
    python tools/validate.py --approved-only      # also fail on draft/rejected (use before publish)
"""
import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema" / "question-bank.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "2": 2, "3": 3, "4": 4, "5": 5}


def select_count(prompt: str) -> int | None:
    """The N in "Select N" / "Choose N" / "(Select three.)", if the prompt states one."""
    m = (re.search(r"\b(?:select|choose|pick)\s+(two|three|four|five|[2-5])\b", prompt, re.I)
         or re.search(r"\bwhich\s+(two|three|four|five)\b", prompt, re.I))
    return NUMBER_WORDS[m.group(1).lower()] if m else None


def semantic_errors(bank: dict, path: Path, approved_only: bool) -> list[str]:
    errs = []
    sid = bank["service"]["id"]
    if path.stem != sid:
        errs.append(f"file name '{path.name}' should be '{sid}.json'")

    seen_ids = set()
    for q in bank["questions"]:
        qid = q["id"]
        where = f"[{qid}]"
        if qid in seen_ids:
            errs.append(f"{where} duplicate question id")
        seen_ids.add(qid)
        if not qid.startswith(f"{sid}-"):
            errs.append(f"{where} id must start with '{sid}-'")

        opt_ids = [o["id"] for o in q["options"]]
        if len(set(opt_ids)) != len(opt_ids):
            errs.append(f"{where} duplicate option ids")
        expected = [chr(ord("a") + i) for i in range(len(opt_ids))]
        if opt_ids != expected:
            errs.append(f"{where} option ids must be sequential {expected}, got {opt_ids}")
        texts = [o["text"].strip().lower() for o in q["options"]]
        if len(set(texts)) != len(texts):
            errs.append(f"{where} duplicate option text")

        missing = [a for a in q["answer"] if a not in opt_ids]
        if missing:
            errs.append(f"{where} answer references unknown option(s) {missing}")
        if q["type"] == "single" and len(q["answer"]) != 1:
            errs.append(f"{where} single-choice must have exactly 1 answer")
        stated = select_count(q["prompt"])
        if stated is not None and stated != len(q["answer"]):
            errs.append(f"{where} prompt says select {stated} but {len(q['answer'])} answer(s) are marked correct")
        if q["type"] == "multi":
            if len(q["answer"]) < 2:
                errs.append(f"{where} multi-select must have at least 2 answers")
            if len(q["answer"]) >= len(opt_ids):
                errs.append(f"{where} multi-select can't have every option correct")

        if approved_only and q["status"] != "approved":
            errs.append(f"{where} status is '{q['status']}' (only approved allowed)")
    return errs


def validate_file(path: Path, approved_only: bool) -> list[str]:
    try:
        bank = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"invalid JSON: {e}"]
    schema_errs = [
        f"schema: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in sorted(VALIDATOR.iter_errors(bank), key=lambda e: list(e.absolute_path))
    ]
    if schema_errs:  # semantic checks assume a structurally valid file
        return schema_errs
    return semantic_errors(bank, path, approved_only)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--approved-only", action="store_true")
    args = ap.parse_args()

    files = args.files or sorted((ROOT / "content" / "questions").glob("*.json"))
    if not files:
        print("no question files found")
        return 1

    failed = 0
    for f in files:
        errs = validate_file(f, args.approved_only)
        if errs:
            failed += 1
            print(f"FAIL {f.name}")
            for e in errs:
                print(f"   - {e}")
        else:
            n = len(json.loads(f.read_text(encoding="utf-8"))["questions"])
            print(f"OK   {f.name} ({n} questions)")
    print(f"\n{len(files) - failed}/{len(files)} files valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
