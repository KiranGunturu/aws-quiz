"""Quiz site: private S3 bucket + CloudFront (OAC), Cognito sign-in, and a small API for questions and progress.

    /            static site (public: welcome page, topic list, about)
    /api/*       HTTP API with a Cognito JWT authorizer -> Lambda
                   questions come from a separate private bucket, progress from DynamoDB
"""
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    SecretValue,
    RemovalPolicy,
    Stack,
    Fn,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_ses as ses,
    aws_iam as iam,
)
from constructs import Construct

API_CODE = Path(__file__).resolve().parent / "api"


# Fonts come from Google; sign-in tokens come from the Cognito domain; everything else from this site.
def csp(auth_origin: str) -> str:
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        f"connect-src 'self' {auth_origin}",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


HOST_ROUTER = """
function handler(event) {
  var req = event.request;
  var host = req.headers.host ? req.headers.host.value.toLowerCase() : "";
  var uri = req.uri;
  if (host === "%(apex)s") {
    if (uri === "/" || uri === "/index.html") { req.uri = "/home/index.html"; return req; }
    if (uri === "/robots.txt" || uri === "/sitemap.xml") { req.uri = "/home" + uri; return req; }
    if (uri === "/favicon.svg" || uri.indexOf("/home/") === 0) return req;
    return redirect("https://%(site)s" + uri + query(req.querystring));
  }
  if (uri === "/home" || uri === "/home/" || uri === "/home/index.html") return redirect("https://%(apex)s/");
  return req;
}
function query(qs) {
  var parts = [];
  for (var k in qs) {
    var v = qs[k];
    if (v.multiValue) { for (var i = 0; i < v.multiValue.length; i++) parts.push(k + "=" + v.multiValue[i].value); }
    else parts.push(v.value === "" ? k : k + "=" + v.value);
  }
  return parts.length ? "?" + parts.join("&") : "";
}
function redirect(url) {
  return { statusCode: 301, statusDescription: "Moved Permanently",
           headers: { location: { value: url }, "cache-control": { value: "max-age=3600" } } };
}
"""


class QuizSiteStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, site_dir: Path, data_dir: Path,
                 domain_name: str | None = None, hosted_zone_name: str | None = None,
                 auth_domain_name: str | None = None,
                 context_prefix: str | None = None,
                 google_client_id: str | None = None, google_secret_name: str = "aws-quiz/google-client-secret",
                 email_signup: bool = False,
                 google_site_verification: str | None = None,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self, "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,  # keep content if the stack is deleted by mistake
        )

        # ---------- custom domain (optional) ----------
        # quiz.example.com serves the site, auth.example.com serves the sign-in pages. The bare zone name
        # (example.com) also points at the site: Cognito requires the parent of a custom sign-in domain to
        # have an A record, and it gives visitors to example.com something to see until it has its own page.
        certificate = zone = None
        site_names: list[str] = []
        if domain_name:
            if not hosted_zone_name:
                raise ValueError("domainName needs hostedZoneName too (the Route 53 zone it lives in)")
            if self.region != "us-east-1":
                raise ValueError("a CloudFront certificate must be in us-east-1; deploy this stack there")
            zone = route53.HostedZone.from_lookup(self, "Zone", domain_name=hosted_zone_name)
            site_names = list(dict.fromkeys([domain_name, hosted_zone_name]))
            certificate = acm.Certificate(
                self, "Certificate", domain_name=domain_name,
                subject_alternative_names=[n for n in site_names[1:]] + ([auth_domain_name] if auth_domain_name else []),
                validation=acm.CertificateValidation.from_dns(zone))

        # ---------- email: Amazon SES for the domain ----------
        # Created whenever there's a hosted zone: DKIM records, a custom MAIL FROM (mail.<zone>, with
        # its MX and SPF records) and a DMARC policy, so mail from no-reply@<zone> passes Gmail's and
        # Outlook's checks. Cognito only switches to it with email_signup, because Cognito needs the
        # domain verified and the account out of the SES sandbox first (README: Email sign-up).
        mail_domain = None
        if zone:
            ses.EmailIdentity(
                self, "MailDomain",
                identity=ses.Identity.public_hosted_zone(zone),
                mail_from_domain=f"mail.{hosted_zone_name}",
            )
            route53.TxtRecord(self, "Dmarc", zone=zone, record_name=f"_dmarc.{hosted_zone_name}",
                              values=["v=DMARC1; p=quarantine; adkim=r; aspf=r"])
            mail_domain = hosted_zone_name
        if email_signup and not mail_domain:
            raise ValueError("email sign-up sends through SES on your own domain: set SITE_DOMAIN and HOSTED_ZONE")

        # ---------- sign-in: Cognito user pool ----------
        # Google, plus email + password when email_signup is on. Without Google, email + password only.
        federated_only = bool(google_client_id) and not email_signup
        user_pool = cognito.UserPool(
            self, "Users",
            user_pool_name="aws-quiz-users",
            feature_plan=cognito.FeaturePlan.ESSENTIALS,  # needed for the newer managed login pages
            self_sign_up_enabled=not federated_only,  # federated users are still created on first sign-in
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)),
            password_policy=cognito.PasswordPolicy(min_length=10, require_symbols=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            email=(cognito.UserPoolEmail.with_ses(
                       from_email=f"no-reply@{mail_domain}", from_name="Know Your Cloud",
                       ses_region=self.region, ses_verified_domain=mail_domain)
                   if email_signup else None),
            user_verification=cognito.UserVerificationConfig(
                email_subject="Your Know Your Cloud verification code",
                email_body="Your Know Your Cloud verification code is {####}. "
                           "It expires in 24 hours. If you didn't sign up, you can ignore this email.",
                email_style=cognito.VerificationEmailStyle.CODE),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,  # never lose user accounts to a stack mistake
        )
        # prefix must be globally unique and can't contain "aws"/"amazon"/"cognito";
        # derive it from this stack's id so it's stable and needs no input
        prefix = context_prefix or "quiz-" + Fn.select(0, Fn.split("-", Fn.select(2, Fn.split("/", self.stack_id))))
        domain = user_pool.add_domain(
            "SignInDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=prefix),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        auth_origin = f"https://{domain.domain_name}.auth.{self.region}.amazoncognito.com"
        custom_auth = None
        if auth_domain_name and certificate:
            # The amazoncognito.com prefix domain stays too, so old Google redirect URIs keep working.
            custom_auth = user_pool.add_domain(
                "CustomSignInDomain",
                custom_domain=cognito.CustomDomainOptions(domain_name=auth_domain_name, certificate=certificate),
                managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
            )
            auth_origin = f"https://{auth_domain_name}"

        # Optional "Continue with Google": on when GOOGLE_CLIENT_ID is set. The client secret is read
        # from Secrets Manager at deploy time, so it never appears in the template or in .env.
        google = None
        if google_client_id:
            google = cognito.UserPoolIdentityProviderGoogle(
                self, "Google",
                user_pool=user_pool,
                client_id=google_client_id,
                client_secret_value=SecretValue.secrets_manager(google_secret_name),
                scopes=["openid", "email", "profile"],
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.GOOGLE_EMAIL,
                    given_name=cognito.ProviderAttribute.GOOGLE_GIVEN_NAME,   # leaderboard shows "First L."
                    family_name=cognito.ProviderAttribute.GOOGLE_FAMILY_NAME,
                    fullname=cognito.ProviderAttribute.GOOGLE_NAME,
                    custom={"email_verified": cognito.ProviderAttribute.other("email_verified")},
                ),
            )

        idps = [p for p in (google,) if p]

        # One person, one account: a Google sign-in joins an existing confirmed email account with the
        # same address, and email sign-up is refused for an address that already uses Google.
        if google and email_signup:
            linker = lambda_.Function(
                self, "AccountLinker",
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler="presignup.handler",
                code=lambda_.Code.from_asset(str(API_CODE)),
                memory_size=128,
                timeout=Duration.seconds(5),
                log_group=logs.LogGroup(self, "LinkerLogs", retention=logs.RetentionDays.ONE_MONTH,
                                        removal_policy=RemovalPolicy.DESTROY),
            )
            # the pool ARN can't be named here (the pool depends on this function), so scope to the
            # account's user pools in this region
            linker.add_to_role_policy(iam.PolicyStatement(
                actions=["cognito-idp:ListUsers", "cognito-idp:AdminLinkProviderForUser",
                         "cognito-idp:AdminDeleteUser"],
                resources=[f"arn:{self.partition}:cognito-idp:{self.region}:{self.account}:userpool/*"]))
            user_pool.add_trigger(cognito.UserPoolOperation.PRE_SIGN_UP, linker)

        # ---------- API: questions + progress ----------
        progress = dynamodb.Table(
            self, "Progress",
            partition_key=dynamodb.Attribute(name="userId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        # Leaderboard: totals per board (pk "ALL" or "W#2026-W41", sk = user) with a "points" sort index,
        # plus "already answered" markers (pk "C#<board>#<user>", sk = question id) that have no points
        # attribute, so they stay out of the index. Weekly items expire via TTL.
        board = dynamodb.Table(
            self, "Leaderboard",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        board.add_global_secondary_index(
            index_name="byPoints",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="points", type=dynamodb.AttributeType.NUMBER),
        )
        data_bucket = s3.Bucket(  # question banks; only the API function can read them
            self, "QuestionData",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        s3deploy.BucketDeployment(
            self, "DeployQuestions",
            sources=[s3deploy.Source.asset(str(data_dir))],
            destination_bucket=data_bucket,
            destination_key_prefix="questions/",
            prune=True,
            memory_limit=512,
        )
        fn = lambda_.Function(
            self, "ApiFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(API_CODE)),
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={"PROGRESS_TABLE": progress.table_name, "LEADERBOARD_TABLE": board.table_name,
                         "DATA_BUCKET": data_bucket.bucket_name, "USER_POOL_ID": user_pool.user_pool_id},
            log_group=logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY),
        )
        progress.grant_read_write_data(fn)
        board.grant_read_write_data(fn)
        # first/last name for the leaderboard; email users choose a display name on the site
        user_pool.grant(fn, "cognito-idp:AdminGetUser", "cognito-idp:AdminUpdateUserAttributes")
        data_bucket.grant_read(fn, "questions/*")

        api = apigw.HttpApi(self, "Api", api_name="aws-quiz-api", create_default_stage=False)
        apigw.HttpStage(self, "ApiStage", http_api=api, stage_name="$default", auto_deploy=True,
                        throttle=apigw.ThrottleSettings(rate_limit=20, burst_limit=40))
        # client and authorizer are wired up below, once the site URL (callback) is known

        headers = cloudfront.ResponseHeadersPolicy(
            self, "SecurityHeaders",
            comment="AWS quiz security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp(auth_origin), override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True),
            ),
        )

        # The bare domain is the personal homepage (dist/home/), not a second copy of the quiz:
        # "/" -> /home/index.html, its own robots.txt and sitemap, and anything else redirects to the
        # quiz address. The quiz address sends /home/ back to the bare domain. One URL per page for Google.
        host_router = None
        if domain_name and hosted_zone_name and hosted_zone_name != domain_name:
            host_router = cloudfront.Function(
                self, "HostRouter",
                comment="bare domain = homepage; everything else lives on the quiz address",
                runtime=cloudfront.FunctionRuntime.JS_2_0,
                code=cloudfront.FunctionCode.from_inline(HOST_ROUTER % {
                    "apex": hosted_zone_name, "site": domain_name}),
            )

        distribution = cloudfront.Distribution(
            self, "Distribution",
            comment="AWS service quiz",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
                compress=True,
                function_associations=[cloudfront.FunctionAssociation(
                    function=host_router, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST)]
                    if host_router else None,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(f"{api.api_id}.execute-api.{self.region}.{self.url_suffix}"),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # forwards the Authorization header and query string, not Host
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    response_headers_policy=headers,
                ),
            },
            error_responses=[cloudfront.ErrorResponse(  # OAC returns 403 for missing keys
                http_status=403, response_http_status=404, response_page_path="/index.html",
                ttl=Duration.minutes(5))],
            domain_names=site_names or None,
            certificate=certificate,
            minimum_protocol_version=(cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021
                                      if certificate else None),
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,  # North America + Europe edges
        )

        site_url = f"https://{domain_name or distribution.distribution_domain_name}"
        client = user_pool.add_client(
            "WebClient",
            user_pool_client_name="aws-quiz-web",
            generate_secret=False,  # browser app: authorization code + PKCE
            auth_flows=cognito.AuthFlow(),  # no direct password APIs; only the managed pages
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"https://{n}/" for n in site_names] or [f"{site_url}/"],
                logout_urls=[f"https://{n}/" for n in site_names] or [f"{site_url}/"],
            ),
            supported_identity_providers=(
                ([cognito.UserPoolClientIdentityProvider.GOOGLE] if google else [])
                + ([cognito.UserPoolClientIdentityProvider.COGNITO] if (email_signup or not google) else [])),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            enable_token_revocation=True,
        )
        for idp in idps:
            client.node.add_dependency(idp)  # the client can't name a provider that doesn't exist yet
        cognito.CfnManagedLoginBranding(  # default Cognito look; restyle in the console's branding editor
            self, "SignInBranding",
            user_pool_id=user_pool.user_pool_id,
            client_id=client.user_pool_client_id,
            use_cognito_provided_values=True,
        )

        jwt = authorizers.HttpJwtAuthorizer(
            "CognitoJwt",
            f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[client.user_pool_client_id],  # checked against the access token's client_id
        )
        integration = integrations.HttpLambdaIntegration("ApiIntegration", fn)
        for path, methods in (("/api/questions/{id}", [apigw.HttpMethod.GET]),
                              ("/api/answer", [apigw.HttpMethod.POST]),
                              ("/api/start", [apigw.HttpMethod.POST]),
                              ("/api/leaderboard", [apigw.HttpMethod.GET]),
                              ("/api/progress", [apigw.HttpMethod.GET, apigw.HttpMethod.PUT]),
                              ("/api/profile", [apigw.HttpMethod.GET, apigw.HttpMethod.PUT])):
            api.add_routes(path=path, methods=methods, integration=integration, authorizer=jwt)

        # File names aren't content-hashed, so keep browser caching short and
        # invalidate CloudFront on every deploy.
        s3deploy.BucketDeployment(
            self, "DeploySite",
            sources=[
                s3deploy.Source.asset(str(site_dir)),
                # filled in at deploy time, so the site needs no hand-copied IDs
                s3deploy.Source.json_data("config.json", {
                    "auth": {"domain": auth_origin, "clientId": client.user_pool_client_id,
                             "google": bool(google), "email": not federated_only},
                }),
            ],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            cache_control=[s3deploy.CacheControl.from_string("public, max-age=300")],
            prune=True,
            memory_limit=512,
        )

        if domain_name and zone:
            target = route53.RecordTarget.from_alias(targets.CloudFrontTarget(distribution))
            route53.ARecord(self, "AliasA", zone=zone, record_name=domain_name, target=target)
            route53.AaaaRecord(self, "AliasAAAA", zone=zone, record_name=domain_name, target=target)
            if hosted_zone_name != domain_name:
                apex_a = route53.ARecord(self, "ApexA", zone=zone, record_name=hosted_zone_name, target=target)
                route53.AaaaRecord(self, "ApexAAAA", zone=zone, record_name=hosted_zone_name, target=target)
                if custom_auth:
                    custom_auth.node.add_dependency(apex_a)  # Cognito checks the parent's A record
            if google_site_verification:  # Google Search Console: proves you own the domain
                route53.TxtRecord(self, "SearchConsole", zone=zone, record_name=hosted_zone_name,
                                  values=[google_site_verification if google_site_verification.startswith(
                                      "google-site-verification=") else
                                          f"google-site-verification={google_site_verification}"])
            if custom_auth:
                route53.ARecord(self, "AuthA", zone=zone, record_name=auth_domain_name,
                                target=route53.RecordTarget.from_alias(targets.UserPoolDomainTarget(custom_auth)))

        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "SignInDomain", value=auth_origin)
        CfnOutput(self, "GoogleRedirectUri", value=f"{auth_origin}/oauth2/idpresponse",
                  description="Authorized redirect URI to enter in the Google OAuth client")
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
