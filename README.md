# Know Your Cloud

Short, practical AWS quizzes for engineers, with an explanation for every answer.

- Quiz: https://cloud.kirangunturu.com
- Author: https://kirangunturu.com

## Architecture

[![Know Your Cloud architecture](docs/architecture.png)](docs/architecture.png)

*Click the diagram for full size. Editable source: [docs/architecture.drawio](docs/architecture.drawio).*

Everything runs in one AWS account in us-east-1 and is created by a single CDK stack.

## What each AWS service does

| Service | Role in this project |
|---|---|
| **Route 53** | Hosts the domain and points `kirangunturu.com`, `cloud.kirangunturu.com` and `auth.kirangunturu.com` to the right places. |
| **CloudFront** | Serves the site over HTTPS from edge locations, adds security headers, and forwards `/api/*` calls to the API. |
| **CloudFront Function** | Decides by host name: `kirangunturu.com` gets the homepage, `cloud.kirangunturu.com` gets the quiz. |
| **ACM** | Provides the free TLS certificate for the site and the sign-in pages. |
| **S3** | Two private buckets: one for the website files, one for the question banks. Only CloudFront and the API can read them. |
| **API Gateway** | Checks every request's sign-in token, routes it to Lambda, and limits traffic to 20 requests per second. |
| **Lambda (quiz API)** | Sends questions without answers, grades answers on the server, awards points, and saves progress. |
| **DynamoDB** | Two tables: one for each user's progress, one for the weekly and all-time leaderboards. |
| **Cognito** | Handles sign-in, with Google or email and password, on hosted pages at `auth.kirangunturu.com`. |
| **Lambda (pre sign-up)** | Joins a Google sign-in and an email account that share the same address, so each person has one account. |
| **SES** | Sends the sign-up and password-reset codes from `no-reply@kirangunturu.com`. |
| **Secrets Manager** | Stores the Google sign-in secret so it never appears in code or config files. |
| **IAM** | Gives each Lambda function only the permissions it needs. |
| **CloudWatch** | Keeps Lambda logs and metrics for troubleshooting. |
| **CloudFormation (CDK)** | Creates and updates all of the above from code with one deploy command. |
| **Bedrock** | Drafts and fact-checks quiz questions from my notes and decks. It runs on my computer, not in the live site. |
