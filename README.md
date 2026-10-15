# Know Your Cloud

Short, practical AWS quizzes for engineers, with an explanation for every answer.

- Quiz: https://cloud.kirangunturu.com
- Author: https://kirangunturu.com

## Architecture

[![Know Your Cloud architecture](docs/architecture-numbered.png)](docs/architecture-numbered.png)

*Click the diagram for full size. Editable source: [docs/architecture.drawio](docs/architecture.drawio).*

Everything runs in one AWS account in us-east-1 and is created by a single CDK stack. The numbers on the
diagram match the steps and the table below.

## How the data flows

**Opening the site**

1. The visitor opens the site in a **browser**.
2. The browser looks up `cloud.kirangunturu.com` in **Route 53**, which points it at CloudFront.
3. **CloudFront** receives the request over HTTPS.
4. A **CloudFront Function** checks the host name: `kirangunturu.com` gets the homepage, `cloud.kirangunturu.com` gets the quiz.
5. The HTTPS connection uses the **ACM** certificate.
6. CloudFront returns the pages, styles and script from the private **S3** site bucket.

**Signing in**

7. The visitor signs in on **Cognito**'s hosted pages at `auth.kirangunturu.com`, with Google or with email and password.
8. On a first sign-in, the **pre sign-up Lambda** links a Google sign-in and an email account that share the same address.
9. For email sign-up, Cognito sends the verification code through **SES**.

**Taking a quiz**

10. The browser calls `/api/*` with its sign-in token. CloudFront forwards it to **API Gateway**, which checks the token and the rate limit.
11. API Gateway invokes the **quiz API Lambda**.
12. The Lambda reads the question bank from the private **S3** questions bucket and sends questions without their answers.
13. When an answer comes back, the Lambda grades it and writes progress and leaderboard points to **DynamoDB**.

**Behind the scenes**

14. **Secrets Manager** holds the Google sign-in secret that Cognito uses.
15. **CloudFormation** (through CDK) creates and updates everything above with one deploy.
16. **Bedrock** drafts and fact-checks the quiz questions on my computer before they are published.
17. **CloudWatch** keeps the Lambda logs and metrics.
18. **IAM** gives each part only the permissions it needs.

## What each AWS service does

| # | Service | Role in this project |
|---|---|---|
| 1 | **Browser** (not AWS) | Runs the quiz app: plain HTML, CSS and JavaScript that call the API with the visitor's sign-in token. |
| 2 | **Route 53** | Hosts the domain and points `kirangunturu.com`, `cloud.kirangunturu.com` and `auth.kirangunturu.com` to the right places. |
| 3 | **CloudFront** | Serves the site over HTTPS from edge locations, adds security headers, and forwards `/api/*` calls to the API. |
| 4 | **CloudFront Function** | Routes by host name: homepage on `kirangunturu.com`, quiz on `cloud.kirangunturu.com`. |
| 5 | **ACM** | Provides the free TLS certificate for the site and the sign-in pages. |
| 6 | **S3 (site files)** | Private bucket with the website files. Only CloudFront can read it. |
| 7 | **Cognito** | Handles sign-in, with Google or email and password, on hosted pages at `auth.kirangunturu.com`. |
| 8 | **Lambda (pre sign-up)** | Joins a Google sign-in and an email account with the same address, so each person has one account. |
| 9 | **SES** | Sends sign-up and password-reset codes from `no-reply@kirangunturu.com`. |
| 10 | **API Gateway** | Checks every request's sign-in token, routes it to Lambda, and limits traffic to 20 requests per second. |
| 11 | **Lambda (quiz API)** | Sends questions without answers, grades answers on the server, awards points, and saves progress. |
| 12 | **S3 (question banks)** | Private bucket with the questions. Only the quiz API can read it. |
| 13 | **DynamoDB** | Two tables: each user's progress, and the weekly and all-time leaderboards. |
| 14 | **Secrets Manager** | Stores the Google sign-in secret so it never appears in code or config files. |
| 15 | **CloudFormation (CDK)** | Creates and updates all of the above from code with one deploy command. |
| 16 | **Bedrock** | Drafts and fact-checks quiz questions from my notes and decks. Runs on my computer, not in the live site. |
| 17 | **CloudWatch** | Keeps Lambda logs and metrics for troubleshooting. |
| 18 | **IAM** | Gives each Lambda function only the permissions it needs. |
