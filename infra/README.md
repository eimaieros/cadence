# cadence on AWS

One CDK stack (`lib/cadence-stack.ts`), the files the host runs (`../deploy/`)
and a workflow that ships `main` after CI passes (`../.github/workflows/deploy.yml`).

```
viewer ─ HTTPS ─> CloudFront ─ HTTP, inside the VPC ─> Caddy ─┬─ /api/* ─> FastAPI ─┐
                  (caches /_next/static)                      └─ /* ─────> Next.js   ├─> PostgreSQL
                                                  one t4g.small running Docker Compose    (own EBS volume)
```

## Why one instance

cadence's traffic is a few people trying an interview. The usual shape for a
web app on AWS — Fargate tasks behind an Application Load Balancer, with RDS —
would be right for a product with users and wrong for this one: it costs
several times as much before the first request, and every part of it would
idle.

Approximate monthly cost, from list prices (us-east-1 figures; eu-north-1 is
within a few percent):

| | This stack | Fargate + ALB + RDS |
|---|---|---|
| Compute | t4g.small, about 12.5 USD — free under AWS's t4g.small trial (750 h a month until 31 December 2026) | Fargate task, 0.5 vCPU / 1 GB on ARM, about 14.5 USD |
| Load balancer | — (CloudFront is the entry point) | ALB about 16.5 USD, plus usage |
| Database | PostgreSQL in a container, on an 8 GB volume | RDS db.t4g.micro about 11.5 USD, plus storage |
| Public IPv4 | one address, 3.65 USD | three addresses (ALB in two AZs, the task), about 11 USD |
| Storage | 24 GB of gp3 and a week of snapshots, about 2.5 USD | similar |
| Secrets | four secrets, 1.60 USD | similar |
| CloudFront, logs, SSM | inside the free tiers at this traffic | same |
| **Total** | **about 8 USD during the trial, about 20 after** | **about 55–60 USD** |

What the bigger shape buys, and when it would be worth it:

- **Deploys without a gap.** Here a release recreates the containers, and for a
  few seconds the site answers 502. Behind a load balancer, tasks are replaced
  one at a time. Worth it when someone is always using the product.
- **A managed database.** RDS gives point-in-time recovery and Multi-AZ; this
  stack gives a daily snapshot, so the worst case is losing a day. Worth it when
  a day of interview transcripts is worth more than the difference.
- **More than one machine.** The rate limiter is in-process and the app runs one
  replica on purpose (see the README's known limits). A second replica needs
  Redis first.

## What protects it

- **One way in.** The host's security group admits port 80 from CloudFront's
  managed prefix list and nothing else. There is no port 22; a shell is
  `aws ssm start-session --target <InstanceId>`.
- **A private hop.** CloudFront reaches the host as a *VPC origin*, on its
  private address. The edge-to-host hop is plain HTTP, and it never crosses the
  public internet. The host keeps a public address for outbound traffic only:
  image pulls, the model API, package updates.
- **Only this distribution.** Every CloudFront distribution in the world
  connects from the same prefix list, so Caddy also requires `X-Origin-Verify`,
  a generated secret only this distribution adds, and answers 403 without it.
- **Rate limits per viewer.** Behind a proxy every request comes from the proxy.
  The API is told to key anonymous limits on `CloudFront-Viewer-Address`
  (`CLIENT_IP_HEADER`), which it can believe only because of the two points above.
- **No long-lived keys.** The host pulls its images and reads its secrets through
  its instance role. GitHub Actions deploys through OIDC, as a role that trusts
  this repository's `production` environment and nothing else, for an hour.
- **IMDSv2 with a hop limit of 1.** The host can read its credentials; the
  containers it runs cannot.
- **Data survives the host.** PostgreSQL lives on its own encrypted EBS volume,
  snapshotted every day and kept for seven. Deleting the stack leaves a final
  snapshot behind.

`test/cadence-stack.test.ts` holds each of these to the synthesized template, so
a change that opens port 22 or widens the deploy role fails in CI.

## First deploy

From [AWS CloudShell](https://console.aws.amazon.com/cloudshell) in the target
region — it already has Node, git and credentials, so nothing is installed
locally and no key leaves AWS:

```bash
git clone https://github.com/eimaieros/cadence.git && cd cadence/infra
npm ci
npx cdk bootstrap                                   # once per account and region
npx cdk deploy --all -c alertEmail=you@example.com  # 15–20 minutes, mostly CloudFront
```

Then:

1. In the GitHub repository, add the variable `AWS_DEPLOY_ROLE_ARN` (Settings →
   Secrets and variables → Actions → Variables) with the stack output
   `DeployRoleArn`. It is not a secret: without GitHub's OIDC token it grants
   nothing.
2. Run the **Deploy** workflow (Actions → Deploy → Run workflow). It builds both
   images on arm64, pushes them to ECR, releases them on the host over SSM and
   checks `/api/ready` through CloudFront. From then on every push to `main`
   that passes CI is shipped.
3. Open the stack output `Url`.

It starts with the scripted interviewer, which costs nothing per session. To use
the model, put an Anthropic key in the secret named by the output
`AnthropicSecretName` and run Deploy again. Anyone can create an account, so the
per-session spend guardrail is then the only thing between a stranger and the
key's bill.

The budget alert (stack `CadenceBudget`) lives in us-east-1, the only region
with AWS Budgets' CloudFormation resource. It measures cost before credits: on
the free plan the net figure stays at zero until the credits run out, which is
the moment an alert stops being useful.

## Operating it

- **Logs:** CloudWatch Logs, group `/cadence/app`, one stream per container.
- **A shell:** `aws ssm start-session --target <InstanceId>`.
- **Roll back:** revert on `main` and push. To go back without a commit, run
  `sudo bash /opt/cadence/release.sh <older-sha>` on the host: ECR keeps the last
  ten images of each service. A release that fails its health checks puts the
  previous one back by itself. That covers code, not schema: the API migrates on
  start, so a migration has to leave the previous release able to run.
- **Restore the database:** create a volume from the snapshot you want, stop the
  host, attach it in place of the data volume and start the host. That happens
  outside CloudFormation, so the stack reports drift until the volume is imported.
- **Tear it down:** `npx cdk destroy --all`. The data volume leaves a final
  snapshot; delete it by hand once it is not needed.
