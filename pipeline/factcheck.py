"""Independent AI fact-check of generated questions (second Bedrock pass).

The reviewer never sees the slides: it judges each question the way a learner meets it, using AWS
knowledge alone. Questions it approves can be published automatically; anything it flags keeps
status "draft" with a reviewNote for a human.
"""
import json

TOOL = "submit_reviews"
BATCH = 8

ISSUES = [
    "wrong_answer",        # the marked answer is not correct
    "other_option_correct",  # an unmarked option is also correct / defensible
    "ambiguous",           # reasonable experts could pick differently
    "outdated",            # true once, not now (renamed services, changed limits)
    "explanation_wrong",   # explanation contains a false statement
    "needs_slide",         # can't be answered without seeing the deck
    "internal_detail",     # company/internal names, accounts, environments
    "price_or_volatile",   # tests exact prices or figures that change often
    "trivial",             # nothing worth learning
    "duplicate",           # tests the same fact as another question listed
]

SYSTEM = f"""You are a meticulous AWS fact-checker reviewing multiple-choice quiz questions before they \
are published on a public learning site under a named author's reputation. Be strict: a wrong \
question published is far worse than a good question held back.

For each question decide:
- "approve" only if you are confident that: the marked answer(s) are correct for AWS as it works \
today; every unmarked option is clearly wrong; a multi-select asks for exactly as many answers as are \
marked; the explanation contains no false statement; and the question can be answered without \
seeing any slide.
- "flag" otherwise, listing every issue that applies from: {", ".join(ISSUES)}.

Also flag "duplicate" when a question tests the same fact as one in the "Other questions" list.
The note must be one or two sentences a human can act on: say what is wrong and what the fix is \
(for example "Option c is also correct: SQS is a valid DLQ target; mark it or replace it").
Review every question you are given, by id."""


def _tool_schema() -> dict:
    return {
        "type": "object",
        "required": ["reviews"],
        "properties": {"reviews": {"type": "array", "items": {
            "type": "object",
            "required": ["id", "verdict", "issues", "note"],
            "properties": {
                "id": {"type": "string"},
                "verdict": {"enum": ["approve", "flag"]},
                "issues": {"type": "array", "items": {"enum": ISSUES}},
                "note": {"type": "string"},
            },
        }}},
    }


def _render(q: dict) -> dict:
    return {"id": q["id"], "type": q["type"], "prompt": q["prompt"],
            "options": {o["id"]: o["text"] for o in q["options"]},
            "marked_correct": q["answer"], "explanation": q["explanation"]}


def review(client, model_id: str, service_name: str, questions: list[dict],
           others: list[str], max_tokens: int = 4096) -> dict[str, dict]:
    """Return {question id: {"verdict", "issues", "note"}}. Missing ids mean the call failed."""
    results: dict[str, dict] = {}
    for start in range(0, len(questions), BATCH):
        batch = questions[start:start + BATCH]
        text = (f"Service: {service_name}\n\n"
                f"Other questions already in this quiz (for duplicate checks):\n"
                + "\n".join(f"- {p}" for p in others[:150])
                + "\n\nQuestions to review:\n" + json.dumps([_render(q) for q in batch], indent=1,
                                                             ensure_ascii=False))
        resp = client.converse(
            modelId=model_id,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [{"text": text}]}],
            toolConfig={"tools": [{"toolSpec": {"name": TOOL, "description": "Submit one review per question.",
                                                "inputSchema": {"json": _tool_schema()}}}],
                        "toolChoice": {"tool": {"name": TOOL}}},
            inferenceConfig={"maxTokens": max_tokens},
        )
        for block in resp["output"]["message"]["content"]:
            if "toolUse" in block and block["toolUse"]["name"] == TOOL:
                for r in block["toolUse"]["input"].get("reviews", []):
                    if r.get("id") in {q["id"] for q in batch} and r.get("verdict") in ("approve", "flag"):
                        results[r["id"]] = r
    return results


def apply(questions: list[dict], results: dict[str, dict], auto_approve: bool) -> tuple[int, int, int]:
    """Set status/reviewNote from review results. Returns (approved, flagged, unreviewed)."""
    approved = flagged = unreviewed = 0
    for q in questions:
        r = results.get(q["id"])
        if r is None:
            q["reviewNote"] = "AI fact-check did not return a verdict; review by hand."
            unreviewed += 1
        elif r["verdict"] == "approve" and not r.get("issues"):
            q.pop("reviewNote", None)
            if auto_approve:
                q["status"] = "approved"
            approved += 1
        else:
            issues = ", ".join(r.get("issues") or []) or "unspecified"
            q["reviewNote"] = f"AI fact-check flagged [{issues}]: {r.get('note', '').strip()}"[:1000]
            flagged += 1
    return approved, flagged, unreviewed
