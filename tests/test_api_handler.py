"""API handler tests against moto (pip install "moto[dynamodb,s3,cognitoidp]" pytest)."""
import importlib
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra" / "api"))

BANK = {"service": {"id": "s3", "name": "Amazon S3", "category": "Storage"}, "questions": [
    {"id": "s3-001", "type": "single", "difficulty": "easy", "prompt": "P1",
     "options": [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}], "answer": ["b"], "explanation": "E1"},
    {"id": "s3-002", "type": "multi", "difficulty": "hard", "prompt": "P2 select two",
     "options": [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}, {"id": "c", "text": "z"}],
     "answer": ["a", "c"], "explanation": "E2"},
]}


@pytest.fixture
def H():
    os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
                      DATA_BUCKET="data", PROGRESS_TABLE="progress", LEADERBOARD_TABLE="board")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="data")
        s3.put_object(Bucket="data", Key="questions/s3.json", Body=json.dumps(BANK))
        ddb = boto3.client("dynamodb")
        ddb.create_table(TableName="progress", BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"}])
        ddb.create_table(
            TableName="board", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": t}
                                  for a, t in (("pk", "S"), ("sk", "S"), ("points", "N"))],
            GlobalSecondaryIndexes=[{"IndexName": "byPoints", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"},
                                                   {"AttributeName": "points", "KeyType": "RANGE"}]}])
        cog = boto3.client("cognito-idp")
        pool = cog.create_user_pool(PoolName="p")["UserPool"]["Id"]
        cog.admin_create_user(UserPoolId=pool, Username="google_1", UserAttributes=[
            {"Name": "given_name", "Value": "Kiran"}, {"Name": "family_name", "Value": "Gunturu"}])
        os.environ["USER_POOL_ID"] = pool
        import handler
        importlib.reload(handler)
        yield handler


def call(H, route, sub="u1", username="google_1", body=None, qs=None, path=None):
    ev = {"routeKey": route, "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub, "username": username}}}}}
    if body is not None:
        ev["body"] = json.dumps(body)
    if qs:
        ev["queryStringParameters"] = qs
    if path:
        ev["pathParameters"] = path
    r = H.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def test_questions_hide_answers(H):
    code, body = call(H, "GET /api/questions/{id}", path={"id": "s3"})
    assert code == 200
    q = body["questions"]
    assert all("answer" not in x and "explanation" not in x for x in q)
    assert [x["need"] for x in q] == [1, 2]
    assert call(H, "GET /api/questions/{id}", path={"id": "../etc"})[0] == 404


def test_grading_and_first_answer_points(H):
    code, r = call(H, "POST /api/answer", body={"qid": "s3-001", "picked": ["a"]})
    assert code == 200 and r["correct"] is False and r["answer"] == ["b"] and r["explanation"] == "E1"
    assert r["point"] is False and r["firstTry"] is True
    # second try is right but doesn't score: only the first answer counts
    _, r = call(H, "POST /api/answer", body={"qid": "s3-001", "picked": ["b"]})
    assert r["correct"] is True and r["point"] is False and r["firstTry"] is False
    _, r = call(H, "POST /api/answer", body={"qid": "s3-002", "picked": ["c", "a"]})
    assert r["correct"] is True and r["point"] is True
    _, lb = call(H, "GET /api/leaderboard", qs={"board": "all"})
    assert lb["top"] == [{"rank": 1, "name": "Kiran G.", "points": 1, "me": True}]
    assert lb["me"]["rank"] == 1
    _, wk = call(H, "GET /api/leaderboard")
    assert wk["board"] == "week" and wk["top"][0]["points"] == 1


def test_ranks_and_ties(H):
    for q, picks, sub in (("s3-001", ["b"], "u2"), ("s3-002", ["a", "c"], "u2"),
                          ("s3-001", ["b"], "u3"), ("s3-001", ["b"], "u1")):
        call(H, "POST /api/answer", sub=sub, username="google_1" if sub == "u1" else "nobody",
             body={"qid": q, "picked": picks})
    _, lb = call(H, "GET /api/leaderboard", sub="u3", qs={"board": "all"})
    assert [(x["rank"], x["points"]) for x in lb["top"]] == [(1, 2), (2, 1), (2, 1)]
    assert lb["me"] == {"rank": 2, "points": 1, "name": "Player U3"}
    names = {x["name"] for x in lb["top"]}
    assert "Kiran G." in names  # u1 has a Cognito profile (fixture user)


def test_bad_input(H):
    for body in ({"qid": "s3-001", "picked": []}, {"qid": "s3-001", "picked": ["z"]},
                 {"qid": "nope", "picked": ["a"]}, {"qid": "s3-001", "picked": "a"}):
        assert call(H, "POST /api/answer", body=body)[0] == 400
    assert call(H, "POST /api/answer", body={"qid": "s3-999", "picked": ["a"]})[0] == 404
    assert call(H, "POST /api/answer", body={"qid": "ec2-001", "picked": ["a"]})[0] == 404


def test_no_leaderboard_entry_until_a_point(H):
    call(H, "POST /api/answer", body={"qid": "s3-001", "picked": ["a"]})
    _, lb = call(H, "GET /api/leaderboard", qs={"board": "all"})
    assert lb["top"] == [] and lb["me"] is None


def test_progress_roundtrip(H):
    good = {"best": {"s3": {"score": 7, "total": 10, "at": "2026"}}, "missed": {"s3": ["s3-001", "s3-001"]}}
    assert call(H, "PUT /api/progress", body=good)[0] == 200
    assert call(H, "GET /api/progress")[1]["missed"] == {"s3": ["s3-001"]}
    assert call(H, "PUT /api/progress", body={"best": {"s3": {"score": 11, "total": 10}}})[0] == 400


def test_profile_name_for_email_user(H):
    cog = boto3.client("cognito-idp")
    cog.admin_create_user(UserPoolId=os.environ["USER_POOL_ID"], Username="pat@example.com",
                          UserAttributes=[{"Name": "email", "Value": "pat@example.com"}])
    me = dict(sub="e1", username="pat@example.com")
    assert call(H, "GET /api/profile", **me) == (200, {"name": "Player E1", "needsName": True})
    # earn a point first, so the board row gets renamed too
    call(H, "POST /api/answer", body={"qid": "s3-001", "picked": ["b"]}, **me)
    for bad in ("", "1abc", "<b>", "x" * 41):
        assert call(H, "PUT /api/profile", body={"name": bad}, **me)[0] == 400
    assert call(H, "PUT /api/profile", body={"name": "  Pat   van Dyke "}, **me) == (200, {"name": "Pat D.", "needsName": False})
    _, board = call(H, "GET /api/leaderboard", qs={"board": "all"}, **me)
    assert board["top"][0]["name"] == "Pat D."
    assert call(H, "GET /api/profile", sub="u1") == (200, {"name": "Kiran G.", "needsName": False})


@pytest.fixture
def P():
    os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x")
    with mock_aws():
        cog = boto3.client("cognito-idp")
        pool = cog.create_user_pool(PoolName="p", UsernameAttributes=["email"])["UserPool"]["Id"]
        import presignup
        importlib.reload(presignup)
        yield presignup, cog, pool


def ev(pool, source, username, email):
    return {"triggerSource": source, "userPoolId": pool, "userName": username,
            "request": {"userAttributes": {"email": email}}, "response": {}}


def test_presignup_links_google_to_confirmed_email_account(P):
    mod, cog, pool = P
    cog.admin_create_user(UserPoolId=pool, Username="a@example.com", MessageAction="SUPPRESS",
                          UserAttributes=[{"Name": "email", "Value": "a@example.com"},
                                          {"Name": "email_verified", "Value": "true"}])
    cog.admin_set_user_password(UserPoolId=pool, Username="a@example.com", Password="Passw0rd!x", Permanent=True)
    calls = []
    real = mod.cognito()
    class Spy:
        def __getattr__(self, n):
            if n == "admin_link_provider_for_user":
                return lambda **kw: calls.append(kw)
            return getattr(real, n)
    mod._cognito = Spy()
    mod.handler(ev(pool, "PreSignUp_ExternalProvider", "google_999", "a@example.com"), None)
    assert calls and calls[0]["SourceUser"]["ProviderAttributeValue"] == "999"
    assert calls[0]["DestinationUser"]["ProviderName"] == "Cognito"


def test_presignup_deletes_unconfirmed_squatter(P):
    mod, cog, pool = P
    cog.sign_up(ClientId=cog.create_user_pool_client(UserPoolId=pool, ClientName="c")["UserPoolClient"]["ClientId"],
                Username="b@example.com", Password="Passw0rd!x", UserAttributes=[{"Name": "email", "Value": "b@example.com"}])
    mod.handler(ev(pool, "PreSignUp_ExternalProvider", "google_5", "b@example.com"), None)
    assert cog.list_users(UserPoolId=pool)["Users"] == []


def test_presignup_refuses_email_signup_for_google_address(P):
    mod, cog, pool = P
    class Fake:
        def list_users(self, **kw):
            return {"Users": [{"Username": "google_7", "UserStatus": "EXTERNAL_PROVIDER"}]}
    mod._cognito = Fake()
    with pytest.raises(Exception, match="Continue with Google"):
        mod.handler(ev(pool, "PreSignUp_SignUp", "c@example.com", "c@example.com"), None)
    mod._cognito = type("E", (), {"list_users": lambda self, **kw: {"Users": []}})()
    assert mod.handler(ev(pool, "PreSignUp_SignUp", "d@example.com", "d@example.com"), None)["userName"] == "d@example.com"
