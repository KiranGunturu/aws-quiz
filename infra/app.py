#!/usr/bin/env python3
"""CDK app for the quiz site.

Deploys dist/ (site) and dist-data/ (question banks, private), both built by tools/build_site.py.
Custom domain: set SITE_DOMAIN and HOSTED_ZONE in .env (or pass -c domainName=... -c hostedZoneName=...).
The sign-in pages then move to auth.<zone> (override with AUTH_DOMAIN, or AUTH_DOMAIN=none to keep
the amazoncognito.com address), and the bare zone name also serves the site.
Optional context:
    -c authDomainPrefix=my-quiz                                     sign-in page prefix (<prefix>.auth.<region>.amazoncognito.com)
Email + password sign-up (next to Google) is on with EMAIL_SIGNUP=on, once the SES domain identity
is verified and the account is out of the SES sandbox (README: Email sign-up).
"Continue with Google" is on when GOOGLE_CLIENT_ID is set in .env and the client secret is stored in
Secrets Manager as aws-quiz/google-client-secret (README: Google sign-in).
"""
import os
import sys
from pathlib import Path

import aws_cdk as cdk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import load_env  # noqa: E402  reuse the project's .env (AWS_PROFILE, AWS_REGION)
from quiz_site_stack import QuizSiteStack  # noqa: E402

load_env()

SITE_DIR = ROOT / "dist"
DATA_DIR = ROOT / "dist-data"
if (not (SITE_DIR / "index.html").exists() or not (SITE_DIR / "data" / "index.json").exists()
        or not any(DATA_DIR.glob("*.json"))):
    sys.exit("dist/ or dist-data/ is missing or incomplete. Run: python tools/build_site.py")

app = cdk.App()
domain = app.node.try_get_context("domainName") or os.environ.get("SITE_DOMAIN", "").strip() or None
zone = app.node.try_get_context("hostedZoneName") or os.environ.get("HOSTED_ZONE", "").strip() or None
auth = (app.node.try_get_context("authDomain") or os.environ.get("AUTH_DOMAIN", "").strip()
        or (f"auth.{zone}" if domain and zone else None))
if auth and auth.lower() == "none":
    auth = None

QuizSiteStack(
    app, "AwsQuizSite",
    site_dir=SITE_DIR,
    data_dir=DATA_DIR,
    context_prefix=app.node.try_get_context("authDomainPrefix") or None,
    google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip() or None,
    domain_name=domain,
    hosted_zone_name=zone,
    auth_domain_name=auth,
    google_site_verification=os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip() or None,
    email_signup=(app.node.try_get_context("emailSignup") or os.environ.get("EMAIL_SIGNUP", "")).strip().lower()
                 in ("on", "true", "yes", "1"),
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        # us-east-1 by default: required for a CloudFront certificate if you add a domain later
        region=os.environ.get("QUIZ_SITE_REGION", "us-east-1"),
    ),
    description="AWS service quiz: S3 + CloudFront site, Cognito sign-in, HTTP API + DynamoDB",
)
cdk.Tags.of(app).add("project", "aws-quiz")
app.synth()
