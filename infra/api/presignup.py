"""Cognito pre sign-up trigger: one person, one account, whichever way they sign in.

Google first sign-in (PreSignUp_ExternalProvider):
    a CONFIRMED email account with the same address exists -> link Google to it, so the person keeps
    one account (same scores, same leaderboard row). Cognito then fails that one sign-in with
    "Already found an entry for username"; the site retries it automatically and the retry works.
    an UNCONFIRMED email account exists -> delete it. Its owner never proved the address, and
    linking to it would hand a Google user's account to whoever typed their address.
Email sign-up (PreSignUp_SignUp):
    the address already signs in with Google -> refuse, and say to use Google.
"""
import boto3

_cognito = None


def cognito():
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def users_with_email(pool_id, email):
    safe = email.replace("\\", "").replace('"', "")
    return cognito().list_users(UserPoolId=pool_id, Filter=f'email = "{safe}"', Limit=10)["Users"]


def handler(event, _context):
    source = event.get("triggerSource")
    pool_id = event["userPoolId"]
    email = ((event.get("request") or {}).get("userAttributes") or {}).get("email", "").strip()
    if not email:
        return event

    if source == "PreSignUp_ExternalProvider":
        # userName looks like "google_1234567890"; Cognito names the provider "Google"
        provider, _, subject = event["userName"].partition("_")
        if provider.lower() != "google" or not subject:
            return event
        for u in users_with_email(pool_id, email):
            status = u.get("UserStatus")
            if status == "EXTERNAL_PROVIDER":
                continue
            if status == "CONFIRMED":
                cognito().admin_link_provider_for_user(
                    UserPoolId=pool_id,
                    DestinationUser={"ProviderName": "Cognito", "ProviderAttributeValue": u["Username"]},
                    SourceUser={"ProviderName": "Google", "ProviderAttributeName": "Cognito_Subject",
                                "ProviderAttributeValue": subject},
                )
                return event
            cognito().admin_delete_user(UserPoolId=pool_id, Username=u["Username"])
        return event

    if source == "PreSignUp_SignUp":
        if any(u.get("UserStatus") == "EXTERNAL_PROVIDER" for u in users_with_email(pool_id, email)):
            # Cognito shows this text on the sign-up page
            raise Exception("This email already signs in with Google. Go back and choose Continue with Google.")
    return event
