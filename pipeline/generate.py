#!/usr/bin/env python3
"""Generate draft quiz questions from a deck with Amazon Bedrock (Converse API + forced tool use).

Drafts land in content/drafts/<service>.json with status "draft". Nothing here publishes;
a human approves drafts before they move to content/questions/.

Usage:
    export BEDROCK_MODEL_ID=<model id or inference profile id>   # e.g. a Claude inference profile
    python pipeline/generate.py decks/lambda.pptx --service lambda --name "AWS Lambda" --category Compute

    # see exactly what would be sent, without calling Bedrock
    python pipeline/generate.py decks/lambda.pptx --service lambda --name "AWS Lambda" --category Compute --dry-run

    # offline test with a canned model response
    python pipeline/generate.py ... --mock-response tests/fixtures/mock_tool_input.json
"""
import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "pipeline"))
from extract import DeckError, extract  # noqa: E402
from render import RenderError, render  # noqa: E402
from envfile import load_env  # noqa: E402
import factcheck  # noqa: E402
from validate import SCHEMA, semantic_errors, VALIDATOR  # noqa: E402

TOOL_NAME = "submit_questions"

SYSTEM_PROMPT = """You write quiz questions for an AWS learning site. Learners are engineers who attended a \
lunch-and-learn session on one AWS service; the quiz checks they understood it.

Rules:
- Ground every question in the slides provided. Do not test facts the slides don't cover.
- Write original questions. Never reproduce certification exam questions or wording.
- Prefer understanding over trivia: when to use a feature, what a setting changes, how two options differ.
- Mix styles: "recall" (a fact or limit), "scenario" (a situation, pick the right action), \
"best-fit" (which approach or service fits).
- Mix difficulty: roughly 40% easy, 40% medium, 20% hard.
- Distractors must be plausible to someone who half-understood the slide. No joke options, \
no "all of the above" / "none of the above".
- Spread the correct answer across positions; don't favour option a.
- single = exactly one correct option. multi = 2+ correct but not all options, and the prompt must say \
how many to select (e.g. "Select two.").
- explanation: 1-3 sentences on why the answer is right and why the most tempting wrong option is wrong. \
Never refer to "the slide", "the diagram" or "the chart"; learners only see the question.
- Before submitting, check every option against the prompt: every option that is true must be in \
answer, and the "Select N" count must equal the number of correct options. If two options could both \
be defended, rewrite the question so exactly the intended ones are right.
- If a scenario uses numbers (memory, duration, cost, concurrency), work the arithmetic out and make \
sure the stated outcome actually follows.
- Do not repeat a question from the "Already written" list, or test the same fact twice.
- slideRef: the slide number the question is based on.
- If a slide describes a specific company's internal setup (account names, internal system names, \
team names, IDs, URLs that aren't public AWS docs), do NOT include those details; write about the \
general AWS concept instead, or skip that slide.
- Don't test exact prices, dollar amounts or other figures that AWS changes over time; test the \
pricing model or the trade-off instead. Hard service limits (e.g. the 15-minute timeout) are fine.
- If a slide states something you know to be wrong or outdated about AWS, don't build a question \
on that claim.
- docUrl only if you are confident it is a real docs.aws.amazon.com page; otherwise omit it.
- Some slides come with an image of the slide (diagrams, architecture drawings, SmartArt, screenshots, \
charts). Read them: the arrows, groupings and labels carry the message. Turn what a diagram shows \
(how a request flows, what sits in which subnet, which component talks to which) into questions.
- Learners will NOT see the slide. Every question must be answerable from AWS knowledge alone; never \
ask "in the diagram, what..." or about colours, icons, layout or slide design.
- Ignore logos, slide footers, page numbers and decorative images. Never mention a company or \
brand shown in a logo.
- Screenshots may show real resource names (functions, files, log groups, accounts, ARNs, \
environments). Never copy them into a question; use generic names like "my-function" instead.
- If the slides have too little substance, return fewer questions rather than padding."""


def tool_input_schema() -> dict:
    """The question schema minus fields we assign ourselves (id, status)."""
    q = copy.deepcopy(SCHEMA["$defs"]["question"])
    for f in ("id", "status"):
        q["properties"].pop(f)
        q["required"].remove(f)
    q["properties"]["style"].pop("default", None)
    q["required"] += ["style", "slideRef"]
    return {
        "type": "object",
        "required": ["questions"],
        "properties": {"questions": {"type": "array", "items": q}},
    }


def chunk(slides: list[dict], size: int) -> list[list[dict]]:
    usable = [s for s in slides if not s["skip"]]
    return [usable[i:i + size] for i in range(0, len(usable), size)]


def build_content(service_name: str, slides: list[dict], n_questions: int,
                  images: dict[int, Path], existing: list[str] | None = None) -> list[dict]:
    """Converse content blocks: text per slide, followed by that slide's image when it has one."""
    blocks: list[dict] = []
    buf = [f"Service: {service_name}", f"Write {n_questions} questions from these slides.", ""]
    if existing:
        buf += ["Already written (don't repeat these or test the same facts):"]
        buf += [f"- {p}" for p in existing] + [""]
    for s in slides:
        buf.append(f"--- Slide {s['slide']}: {s['title'] or '(untitled)'} ---")
        if s["body"]:
            buf.append(s["body"])
        if s["notes"]:
            buf.append(f"Speaker notes: {s['notes']}")
        img = images.get(s["slide"])
        if img:
            buf.append(f"(image of slide {s['slide']} follows)")
            blocks.append({"text": "\n".join(buf)})
            blocks.append({"image": {"format": "png", "source": {"bytes": img.read_bytes()}}})
            buf = []
        buf.append("")
    if any(line.strip() for line in buf):
        blocks.append({"text": "\n".join(buf)})
    return blocks


def describe_content(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        if "text" in b:
            out.append(b["text"])
        else:
            out.append(f"[image: {len(b['image']['source']['bytes']) // 1024} KB png]")
    return "\n".join(out)


def call_bedrock(client, model_id: str, content: list[dict], max_tokens: int) -> dict:
    resp = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": content}],
        toolConfig={
            "tools": [{"toolSpec": {
                "name": TOOL_NAME,
                "description": "Submit the generated quiz questions.",
                "inputSchema": {"json": tool_input_schema()},
            }}],
            "toolChoice": {"tool": {"name": TOOL_NAME}},
        },
        inferenceConfig={"maxTokens": max_tokens},
    )
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block and block["toolUse"]["name"] == TOOL_NAME:
            return block["toolUse"]["input"]
    raise RuntimeError(f"model did not call {TOOL_NAME}; stopReason={resp.get('stopReason')}")


# ---------- post-processing ----------

ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


def leak_reasons(q: dict, deny_terms: list[str]) -> list[str]:
    blob = json.dumps(q, ensure_ascii=False).lower()
    reasons = []
    if ACCOUNT_ID.search(blob):
        reasons.append("contains a 12-digit number (possible AWS account id)")
    for t in deny_terms:
        # whole-word match, where "_" and "-" also count as separators: "acme" hits "corp_acme_dev" but not "acmes"
        if re.search(rf"(?<![a-z0-9]){re.escape(t.lower())}(?![a-z0-9])", blob):
            reasons.append(f"contains denied term '{t}'")
    return reasons


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def load_bank(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def next_id_number(service_id: str, *banks: dict | None) -> int:
    nums = [0]
    for b in banks:
        for q in (b or {}).get("questions", []):
            m = re.fullmatch(rf"{re.escape(service_id)}-(\d{{3}})", q["id"])
            if m:
                nums.append(int(m.group(1)))
    return max(nums) + 1


def check_question(q: dict, service: dict, valid_slides: set[int]) -> list[str]:
    bank = {"schemaVersion": "1.0", "service": service, "questions": [q]}
    errs = [e.message for e in VALIDATOR.iter_errors(bank)]
    if errs:
        return errs
    errs = semantic_errors(bank, Path(f"{service['id']}.json"), approved_only=False)
    if q.get("slideRef") not in valid_slides:
        errs.append(f"slideRef {q.get('slideRef')} is not one of the slides sent")
    return errs


def main() -> int:
    loaded = load_env()  # before argparse, whose defaults read the environment
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("deck", type=Path, help=".pptx deck, or a .slides.json from extract.py")
    ap.add_argument("--service", required=True, help="service id / slug, e.g. lambda")
    ap.add_argument("--name", required=True, help='display name, e.g. "AWS Lambda"')
    ap.add_argument("--category", required=True)
    ap.add_argument("--symbol", help="short tile label on the site, e.g. TLS (optional)")
    ap.add_argument("--chunk-size", type=int, default=6, help="slides per model call")
    ap.add_argument("--images", choices=["auto", "all", "none"], default="auto",
                    help="send slide images: auto = slides with diagrams/pictures/SmartArt (default), "
                         "all = every slide, none = text only")
    ap.add_argument("--per-chunk", type=int, default=5, help="questions requested per call")
    ap.add_argument("--model-id", default=os.environ.get("BEDROCK_MODEL_ID"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--deny-terms", type=Path, help="file with one term per line to reject (internal names etc.)")
    ap.add_argument("--dry-run", action="store_true", help="print the requests; don't call Bedrock or write")
    ap.add_argument("--mock-response", type=Path, help="JSON tool input to use instead of calling Bedrock")
    ap.add_argument("--no-factcheck", action="store_true", help="skip the AI fact-check pass")
    ap.add_argument("--auto-approve", action="store_true",
                    help="mark questions that pass the fact-check as approved (flagged ones stay draft)")
    ap.add_argument("--review-model-id", default=os.environ.get("REVIEW_MODEL_ID"),
                    help="model for the fact-check (default: same as --model-id)")
    ap.add_argument("--mock-review", type=Path, help="JSON reviews to use instead of calling Bedrock")
    args = ap.parse_args()

    service = {"id": args.service, "name": args.name, "category": args.category}
    if args.symbol:
        service["symbol"] = args.symbol
    try:
        data = json.loads(args.deck.read_text(encoding="utf-8")) if args.deck.suffix == ".json" else extract(args.deck)
    except (DeckError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    is_deck = args.deck.suffix.lower() == ".pptx"
    if args.images != "none" and not is_deck:
        print("  note: input is not a .pptx, so slides can't be rendered; using text only", file=sys.stderr)
        args.images = "none"
    if args.images == "none":  # image-only slides have nothing to send
        for s in data["slides"]:
            if not s["skip"] and not s["body"] and not s["notes"]:
                s["skip"] = "image-only and --images none"
    chunks = chunk(data["slides"], args.chunk_size)
    usable = sum(len(c) for c in chunks)
    print(f"{data['deck']}: {usable}/{data['slideCount']} usable slides, {len(chunks)} chunk(s)",
          file=sys.stderr)
    for ln in data.get("removedBoilerplate", []):
        print(f"  removed repeated line: {ln!r}", file=sys.stderr)
    if not chunks:
        print("nothing to generate from", file=sys.stderr)
        return 1

    want = [s["slide"] for c in chunks for s in c
            if args.images == "all" or (args.images == "auto" and s.get("visual"))]
    images: dict[int, Path] = {}
    if want:
        try:
            images = render(args.deck, want)
        except Exception as e:  # no renderer, or every render attempt failed
            print(f"  warning: {e}\n  continuing TEXT-ONLY for this deck (diagram-only slides are skipped)",
                  file=sys.stderr)
            images = {}
            for c in chunks:
                for sl in c:
                    if not sl["body"] and not sl["notes"]:
                        sl["skip"] = "image-only and rendering failed"
            chunks = [[sl for sl in c if not sl["skip"]] for c in chunks]
            chunks = [c for c in chunks if c]
            if not chunks:
                print("error: nothing left to generate from without images", file=sys.stderr)
                return 2
    print(f"  {len(images)} slide image(s) attached", file=sys.stderr)
    existing = []
    for bank_path in (ROOT / "content" / "questions" / f"{args.service}.json",
                      ROOT / "content" / "drafts" / f"{args.service}.json"):
        bank = load_bank(bank_path)
        existing += [q["prompt"] for q in (bank or {}).get("questions", []) if q.get("status") != "rejected"]
    contents = [build_content(args.name, c, args.per_chunk, images, existing) for c in chunks]

    if args.dry_run:
        print(json.dumps({"system": SYSTEM_PROMPT, "tool_input_schema": tool_input_schema()}, indent=2))
        for i, content in enumerate(contents, 1):
            print(f"\n===== request {i}/{len(chunks)} =====\n{describe_content(content)}")
        return 0

    client = None
    if loaded:
        print(f"  from .env: {', '.join(loaded)}", file=sys.stderr)
    if not args.mock_response:
        if not args.model_id:
            ap.error("set BEDROCK_MODEL_ID in .env (see .env.example), or pass --model-id")
        import boto3
        from botocore.config import Config
        client = boto3.client("bedrock-runtime", region_name=args.region, config=Config(
            retries={"max_attempts": 8, "mode": "adaptive"},  # back off on throttling
            read_timeout=300))

    deny = [t.strip() for t in args.deny_terms.read_text(encoding="utf-8-sig").splitlines() if t.strip()] if args.deny_terms else []
    drafts_path = ROOT / "content" / "drafts" / f"{args.service}.json"
    published = load_bank(ROOT / "content" / "questions" / f"{args.service}.json")
    drafts = load_bank(drafts_path) or {"schemaVersion": "1.0", "service": service, "questions": []}

    seen = {norm(q["prompt"]) for b in (published, drafts) for q in (b or {}).get("questions", [])}
    next_n = next_id_number(args.service, published, drafts)
    kept, rejected, failed, new = 0, [], [], []

    for i, (c, content) in enumerate(zip(chunks, contents), 1):
        if args.mock_response:
            tool_input = json.loads(args.mock_response.read_text(encoding="utf-8"))
        else:
            print(f"  calling Bedrock for chunk {i}/{len(chunks)} (slides {c[0]['slide']}-{c[-1]['slide']})",
                  file=sys.stderr)
            try:
                tool_input = call_bedrock(client, args.model_id, content, args.max_tokens)
            except Exception as e:  # keep the other chunks' questions; report this one
                failed.append((i, c[0]["slide"], c[-1]["slide"], str(e)))
                print(f"    chunk {i} failed: {e}", file=sys.stderr)
                continue

        valid_slides = {s["slide"] for s in c}
        for raw in tool_input.get("questions", []):
            q = {"id": f"{args.service}-{next_n:03d}", **raw, "status": "draft", "sourceDeck": data["deck"]}
            label = f"chunk {i}: {raw.get('prompt', '')[:60]!r}"
            reasons = check_question(q, service, valid_slides) + leak_reasons(q, deny)
            if norm(q.get("prompt", "")) in seen:
                reasons.append("duplicate of an existing question")
            if reasons:
                rejected.append((label, reasons))
                continue
            drafts["questions"].append(q)
            new.append(q)
            seen.add(norm(q["prompt"]))
            next_n += 1
            kept += 1

    if new and not args.no_factcheck:
        others = [q["prompt"] for b in (published, drafts) for q in (b or {}).get("questions", [])
                  if q.get("status") != "rejected" and q not in new]
        print(f"\n  fact-checking {len(new)} new question(s)...", file=sys.stderr)
        try:
            if args.mock_review:
                results = {r["id"]: r for r in json.loads(args.mock_review.read_text(encoding="utf-8"))["reviews"]}
            else:
                results = factcheck.review(client, args.review_model_id or args.model_id, args.name, new, others)
        except Exception as e:
            print(f"    fact-check failed: {e}", file=sys.stderr)
            results = {}
        ok, flagged, unreviewed = factcheck.apply(new, results, args.auto_approve)
        print(f"  fact-check: {ok} passed{' (approved)' if args.auto_approve else ''}, "
              f"{flagged} flagged, {unreviewed} not reviewed", file=sys.stderr)

    drafts["source"] = {
        "deck": data["deck"],
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **({"model": args.model_id} if args.model_id and not args.mock_response else {}),
    }
    drafts_path.parent.mkdir(parents=True, exist_ok=True)
    drafts_path.write_text(json.dumps(drafts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\nkept {kept} draft question(s) -> {drafts_path.relative_to(ROOT)}", file=sys.stderr)
    if rejected:
        print(f"rejected {len(rejected)}:", file=sys.stderr)
        for label, reasons in rejected:
            print(f"  - {label}", file=sys.stderr)
            for r in reasons:
                print(f"      {r}", file=sys.stderr)
    if failed:
        print(f"\n{len(failed)} chunk(s) failed and produced no questions:", file=sys.stderr)
        for i, a, b, err in failed:
            print(f"  - chunk {i} (slides {a}-{b}): {err[:200]}", file=sys.stderr)
        print("  re-run the same command: questions already kept are not duplicated", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
