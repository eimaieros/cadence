import { App } from "aws-cdk-lib";

import { CadenceBudgetStack, CadenceStack } from "../lib/cadence-stack.js";

const app = new App();
const context = (key: string): string | undefined => app.node.tryGetContext(key);

const alertEmail = context("alertEmail");
if (!alertEmail) {
  throw new Error("Say where cost alerts go: cdk deploy --all -c alertEmail=you@example.com");
}
const account = process.env.CDK_DEFAULT_ACCOUNT;

new CadenceStack(app, "Cadence", {
  env: { account, region: context("region") ?? "eu-north-1" },
  description: "cadence: CloudFront in front of one Graviton instance running the app in Docker Compose",
  githubRepo: context("githubRepo") ?? "eimaieros/cadence",
  instanceType: context("instanceType"),
  createGithubOidcProvider: context("createGithubOidcProvider") !== "false",
});

new CadenceBudgetStack(app, "CadenceBudget", {
  env: { account, region: "us-east-1" },
  description: "cadence: monthly cost alert, gross of credits",
  alertEmail,
  monthlyBudgetUsd: context("monthlyBudgetUsd") ? Number(context("monthlyBudgetUsd")) : undefined,
});
