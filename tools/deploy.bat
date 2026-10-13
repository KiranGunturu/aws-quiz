@echo off
rem Build the site and deploy it to S3 + CloudFront.
rem Usage:  tools\deploy.bat            (CloudFront URL)
rem         tools\deploy.bat -c domainName=quiz.example.com -c hostedZoneName=example.com
setlocal
cd /d "%~dp0\.."

rem The CDK CLI resolves credentials itself, so pass AWS_PROFILE from .env if the shell hasn't set it.
if not defined AWS_PROFILE if exist .env (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="AWS_PROFILE" if not "%%b"=="" set "AWS_PROFILE=%%b"
  )
)

python tools\build_site.py || exit /b 1
python tools\validate.py --approved-only || exit /b 1

cd infra
call cdk deploy AwsQuizSite %* || exit /b 1
