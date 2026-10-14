"""Quiz API behind an HTTP API JWT authorizer (Cognito access tokens). Payload format 2.0.

GET  /api/questions/{id}   question bank for one topic, WITHOUT answers or explanations
POST /api/start            {qid} -> notes when this user first saw the question (for the points window)
POST /api/answer           {qid, picked[], left} -> graded on the server; returns the answer + explanation
                           and awards leaderboard points
GET  /api/leaderboard      ?board=week|all -> top players plus the caller's own rank
GET  /api/progress         this user's best scores and missed questions
PUT  /api/progress         replace them
GET  /api/profile          {name, needsName}: the leaderboard name, and whether the user should pick one
PUT  /api/profile          {name} -> set it (email sign-ups have no name until they choose one)

Points: one per question, decided by your FIRST answer to it (all-time board), and by your first
answer to it each week (weekly board). Retakes and option-guessing earn nothing.
A first answer earns its point only if it comes within POINT_WINDOW seconds of the question being shown
(POST /api/start) and the page stayed in the foreground ("left": false). Otherwise it's still graded and
explained, and it still uses up the first answer, but scores nothing. This makes copying questions into
another tool or device pointless for the leaderboard.

API Gateway rejects requests without a valid token before they reach this code.
"""
import datetime
import json
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ["DATA_BUCKET"]
PROGRESS_TABLE = os.environ["PROGRESS_TABLE"]
BOARD_TABLE = os.environ["LEADERBOARD_TABLE"]
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")

TOPIC_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
QUESTION_ID = re.compile(r"^([a-z0-9][a-z0-9-]{0,63})-(\d{3,4})$")
OPTION_ID = re.compile(r"^[a-f]$")
MAX_BODY = 64 * 1024
EMPTY = {"best": {}, "missed": {}}
PUBLIC_QUESTION = ("id", "type", "style", "difficulty", "prompt", "options", "tags", "slideRef", "docUrl")
BANK_TTL = 300          # seconds a warm Lambda keeps a question bank
TOP_N = 25
WEEK_CREDIT_DAYS = 15   # weekly "already answered" markers expire after this
POINT_WINDOW = 90       # seconds from first seeing a question to answering it, for a point
START_REUSE = 3600      # a "seen" timestamp older than this is replaced (new week, coming back later)

_clients = {}
_banks = {}             # topic -> (loaded_at, {qid: question})
_names = {}             # sub -> (loaded_at, display name, has a real name)
NAME_TTL = 300
DISPLAY_NAME = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .'-]){0,39}$")  # letters, then letters . ' - and spaces


def client(name):
    if name not in _clients:
        _clients[name] = (boto3.resource("dynamodb") if name == "ddb" else boto3.client(name))
    return _clients[name]


def resp(status, body, cache="no-store"):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": cache},
        "body": body if isinstance(body, str) else json.dumps(body, separators=(",", ":")),
    }


def read_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw) > MAX_BODY:
        raise ValueError("too large")
    return json.loads(raw)


# ---------- questions ----------
def load_bank(topic):
    hit = _banks.get(topic)
    if hit and time.time() - hit[0] < BANK_TTL:
        return hit[1]
    try:
        obj = client("s3").get_object(Bucket=BUCKET, Key=f"questions/{topic}.json")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied", "404"):
            return None
        raise
    bank = json.loads(obj["Body"].read())
    entry = {"service": bank["service"], "questions": {q["id"]: q for q in bank["questions"]}}
    _banks[topic] = (time.time(), entry)
    return entry


def get_questions(topic):
    bank = load_bank(topic) if TOPIC_ID.match(topic or "") else None
    if not bank:
        return resp(404, {"message": "unknown topic"})
    qs = [{**{k: q[k] for k in PUBLIC_QUESTION if k in q}, "need": len(q["answer"])}
          for q in bank["questions"].values()]
    return resp(200, {"service": bank["service"], "questions": qs}, cache="private, max-age=300")


# ---------- leaderboard ----------
def week_key(now=None):
    y, w, _ = (now or datetime.datetime.now(datetime.timezone.utc)).isocalendar()
    return f"W#{y}-W{w:02d}"


def display_name(claims):
    return _lookup_name(claims)[0]


def _lookup_name(claims):
    sub = claims["sub"]
    hit = _names.get(sub)
    if hit and time.time() - hit[0] < NAME_TTL:
        return hit[1], hit[2]
    attrs = {}
    username = claims.get("username")
    if USER_POOL_ID and username:
        try:
            u = client("cognito-idp").admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
            attrs = {a["Name"]: a["Value"] for a in u.get("UserAttributes", [])}
        except ClientError:
            attrs = {}
    first = (attrs.get("given_name") or "").strip()
    last = (attrs.get("family_name") or "").strip()
    if not first and attrs.get("name"):
        parts = attrs["name"].split()
        first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
    name = (f"{first} {last[0]}." if first and last else first or f"Player {sub[:4].upper()}")[:40]
    _names[sub] = (time.time(), name, bool(first))
    return name, bool(first)


def award(board, user, qid, correct, name, expires=None, earns=True):
    """Record the first answer to qid on this board; add a point if it was right (and earns). True if it counted."""
    credit = {"pk": f"C#{board}#{user}", "sk": qid, "correct": correct, "ranked": earns}
    if expires:
        credit["expires"] = expires
    items = [{"Put": {"TableName": BOARD_TABLE, "Item": _ddb(credit),
                      "ConditionExpression": "attribute_not_exists(pk)"}}]
    if correct and earns:
        items.append({"Update": {
            "TableName": BOARD_TABLE,
            "Key": _ddb({"pk": board, "sk": user}),
            "UpdateExpression": "ADD points :one SET #n = :name, updatedAt = :now"
                                + (", expires = :exp" if expires else ""),
            "ExpressionAttributeNames": {"#n": "name"},
            "ExpressionAttributeValues": _ddb({":one": 1, ":name": name, ":now": int(time.time()),
                                               **({":exp": expires} if expires else {})}),
        }})
    try:
        client("dynamodb").transact_write_items(TransactItems=items)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            return False  # already answered on this board
        raise


def _ddb(d):
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    return {k: ser.serialize(v) for k, v in d.items()}


def post_start(claims, event):
    try:
        qid = str(read_body(event).get("qid", ""))
        m = QUESTION_ID.match(qid)
        if not m:
            raise ValueError("bad question id")
    except (ValueError, json.JSONDecodeError) as e:
        return resp(400, {"message": str(e)})
    bank = load_bank(m.group(1))
    if not (bank and qid in bank["questions"]):
        return resp(404, {"message": "unknown question"})
    now = int(time.time())
    try:  # keep the first time it was shown; replace only a stale one
        client("ddb").Table(BOARD_TABLE).put_item(
            Item={"pk": f"S#{claims['sub']}", "sk": qid, "started": now, "expires": now + 2 * 86400},
            ConditionExpression="attribute_not_exists(pk) OR started < :stale",
            ExpressionAttributeValues={":stale": now - START_REUSE})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return resp(200, {"ok": True, "window": POINT_WINDOW})


def eligibility(user, qid, left):
    """None if this answer may earn a point, otherwise why not."""
    if left:
        return "left"
    seen = client("ddb").Table(BOARD_TABLE).get_item(Key={"pk": f"S#{user}", "sk": qid}).get("Item")
    if not seen:
        return "unseen"
    if time.time() - int(seen["started"]) > POINT_WINDOW:
        return "slow"
    return None


def post_answer(claims, event):
    try:
        body = read_body(event)
        qid = str(body.get("qid", ""))
        picked = body.get("picked")
        left = bool(body.get("left", False))
        m = QUESTION_ID.match(qid)
        if not m or not isinstance(picked, list) or not 0 < len(picked) <= 6 \
                or not all(isinstance(p, str) and OPTION_ID.match(p) for p in picked):
            raise ValueError("bad answer")
    except (ValueError, json.JSONDecodeError) as e:
        return resp(400, {"message": str(e)})
    bank = load_bank(m.group(1))
    q = bank and bank["questions"].get(qid)
    if not q:
        return resp(404, {"message": "unknown question"})
    correct = set(picked) == set(q["answer"])

    user = claims["sub"]
    name = display_name(claims)
    week = week_key()
    expires = int(time.time()) + WEEK_CREDIT_DAYS * 86400
    blocked = eligibility(user, qid, left)
    counted = award("ALL", user, qid, correct, name, earns=blocked is None)
    award(week, user, qid, correct, name, expires=expires, earns=blocked is None)
    return resp(200, {
        "correct": correct, "answer": q["answer"], "explanation": q.get("explanation", ""),
        "docUrl": q.get("docUrl"), "slideRef": q.get("slideRef"),
        "point": bool(correct and counted and blocked is None),   # did this answer earn an all-time point
        "firstTry": counted,
        "noPoint": blocked if (correct and counted) else None,     # why a right first answer scored nothing
        "window": POINT_WINDOW,
    })


def get_leaderboard(claims, event):
    which = ((event.get("queryStringParameters") or {}).get("board") or "week").lower()
    board = "ALL" if which == "all" else week_key()
    table = client("ddb").Table(BOARD_TABLE)
    from boto3.dynamodb.conditions import Key
    top = table.query(IndexName="byPoints", KeyConditionExpression=Key("pk").eq(board),
                      ScanIndexForward=False, Limit=TOP_N)["Items"]
    user = claims["sub"]
    rows, rank, prev = [], 0, None
    for i, it in enumerate(top):
        pts = int(it["points"])
        if pts != prev:
            rank, prev = i + 1, pts
        rows.append({"rank": rank, "name": it.get("name", "Player"), "points": pts, "me": it["sk"] == user})
    me = None
    mine = table.get_item(Key={"pk": board, "sk": user}).get("Item")
    if mine:
        pts = int(mine["points"])
        above, kwargs = 0, {}
        while True:  # competition ranking: 1 + number of players with more points
            r = table.query(IndexName="byPoints", Select="COUNT",
                            KeyConditionExpression=Key("pk").eq(board) & Key("points").gt(pts), **kwargs)
            above += r["Count"]
            if "LastEvaluatedKey" not in r:
                break
            kwargs = {"ExclusiveStartKey": r["LastEvaluatedKey"]}
        me = {"rank": above + 1, "points": pts, "name": mine.get("name")}
    return resp(200, {"board": "all" if board == "ALL" else "week", "top": rows, "me": me,
                      "weekStarts": "Monday 00:00 UTC"})


# ---------- profile ----------
def get_profile(claims):
    name, real = _lookup_name(claims)
    return resp(200, {"name": name, "needsName": not real})


def put_profile(claims, event):
    try:
        body = read_body(event)
        name = " ".join(str(body.get("name", "")).split())
        if not DISPLAY_NAME.match(name):
            raise ValueError("Use 1 to 40 letters; spaces, dots, apostrophes and hyphens are fine.")
    except (ValueError, json.JSONDecodeError) as e:
        return resp(400, {"message": str(e)})
    username = claims.get("username")
    if not (USER_POOL_ID and username):
        return resp(400, {"message": "no user"})
    first, _, rest = name.partition(" ")
    last = rest.split()[-1] if rest.split() else ""
    client("cognito-idp").admin_update_user_attributes(
        UserPoolId=USER_POOL_ID, Username=username,
        UserAttributes=[{"Name": "given_name", "Value": first}, {"Name": "family_name", "Value": last},
                        {"Name": "name", "Value": name}])
    _names.pop(claims["sub"], None)
    shown = display_name(claims)
    # rename the rows already on the boards; later points keep it up to date
    table = client("ddb").Table(BOARD_TABLE)
    for board in ("ALL", week_key()):
        try:
            table.update_item(Key={"pk": board, "sk": claims["sub"]}, UpdateExpression="SET #n = :n",
                              ConditionExpression="attribute_exists(pk)",
                              ExpressionAttributeNames={"#n": "name"}, ExpressionAttributeValues={":n": shown})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return resp(200, {"name": shown, "needsName": False})


# ---------- personal progress ----------
def clean_progress(data):
    """Keep only the shape the site writes; drop anything else."""
    if not isinstance(data, dict):
        raise ValueError("progress must be an object")
    best, missed = data.get("best", {}), data.get("missed", {})
    if not isinstance(best, dict) or not isinstance(missed, dict) or len(best) > 200 or len(missed) > 200:
        raise ValueError("bad best/missed")
    out = {"best": {}, "missed": {}}
    for topic, b in best.items():
        if not TOPIC_ID.match(topic) or not isinstance(b, dict):
            raise ValueError(f"bad best entry {topic!r}")
        score, total = b.get("score"), b.get("total")
        if not (isinstance(score, int) and isinstance(total, int) and 0 <= score <= total <= 10000 and total > 0):
            raise ValueError(f"bad score for {topic!r}")
        out["best"][topic] = {"score": score, "total": total, "at": str(b.get("at", ""))[:40]}
    for topic, ids in missed.items():
        if not TOPIC_ID.match(topic) or not isinstance(ids, list) or len(ids) > 2000:
            raise ValueError(f"bad missed entry {topic!r}")
        if not all(isinstance(i, str) and QUESTION_ID.match(i) for i in ids):
            raise ValueError(f"bad question id in {topic!r}")
        out["missed"][topic] = list(dict.fromkeys(ids))
    return out


def get_progress(user):
    item = client("ddb").Table(PROGRESS_TABLE).get_item(Key={"userId": user}).get("Item")
    return resp(200, json.loads(item["data"]) if item else EMPTY)


def put_progress(user, event):
    try:
        data = clean_progress(read_body(event))
    except (ValueError, json.JSONDecodeError) as e:
        return resp(413 if str(e) == "too large" else 400, {"message": str(e)})
    client("ddb").Table(PROGRESS_TABLE).put_item(
        Item={"userId": user, "data": json.dumps(data, separators=(",", ":"))})
    return resp(200, data)


def handler(event, _context):
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    user = claims["sub"]
    route = event["routeKey"]
    if route == "GET /api/questions/{id}":
        return get_questions((event.get("pathParameters") or {}).get("id"))
    if route == "POST /api/start":
        return post_start(claims, event)
    if route == "POST /api/answer":
        return post_answer(claims, event)
    if route == "GET /api/leaderboard":
        return get_leaderboard(claims, event)
    if route == "GET /api/progress":
        return get_progress(user)
    if route == "PUT /api/progress":
        return put_progress(user, event)
    if route == "GET /api/profile":
        return get_profile(claims)
    if route == "PUT /api/profile":
        return put_profile(claims, event)
    return resp(404, {"message": "not found"})
