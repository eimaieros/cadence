import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";

import {
  BACKUP_TAG,
  CadenceBudgetStack,
  CadenceStack,
  DEPLOY_PARAMETER,
  ORIGIN_HEADER,
} from "../lib/cadence-stack.js";

/*
  The properties the README promises about the deployment, checked against the
  synthesized template rather than against a diagram. Each test names one
  promise; if a change breaks it, the test says which.
*/

const env = { account: "123456789012", region: "eu-north-1" };

function synth(overrides: Partial<ConstructorParameters<typeof CadenceStack>[2]> = {}) {
  const stack = new CadenceStack(new App(), "Cadence", {
    env,
    githubRepo: "eimaieros/cadence",
    ...overrides,
  });
  return Template.fromStack(stack);
}

const template = synth();
const resources = (type: string) => Object.values(template.findResources(type));

describe("the network", () => {
  it("has no NAT gateway, which would cost more than everything else combined", () => {
    template.resourceCountIs("AWS::EC2::NatGateway", 0);
  });

  it("lets in port 80 from CloudFront's prefix list, and nothing else", () => {
    const ingress = [
      ...resources("AWS::EC2::SecurityGroupIngress").map((r) => r.Properties),
      ...resources("AWS::EC2::SecurityGroup").flatMap((r) => r.Properties.SecurityGroupIngress ?? []),
    ];
    assert.equal(ingress.length, 1, JSON.stringify(ingress));
    const [rule] = ingress;
    assert.equal(rule.FromPort, 80);
    assert.equal(rule.ToPort, 80);
    assert.ok(rule.SourcePrefixListId, "the source is a prefix list, not an address range");
    assert.equal(rule.CidrIp, undefined);
    assert.equal(rule.CidrIpv6, undefined);
  });

  it("looks the prefix list up by its name, so it never goes stale", () => {
    template.hasResourceProperties("Custom::AWS", {
      Create: Match.stringLikeRegexp("com.amazonaws.global.cloudfront.origin-facing"),
    });
  });
});

describe("the host", () => {
  it("requires IMDSv2 and keeps the credentials one hop away from its containers", () => {
    template.hasResourceProperties("AWS::EC2::Instance", {
      MetadataOptions: { HttpTokens: "required", HttpPutResponseHopLimit: 1 },
    });
  });

  it("resolves its image at launch, so a new AMI never replaces it on deploy", () => {
    template.hasResourceProperties("AWS::EC2::Instance", {
      ImageId: Match.stringLikeRegexp("^resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-.*arm64$"),
      InstanceType: "t4g.small",
    });
  });

  it("throttles rather than bills when it runs out of CPU credits", () => {
    template.hasResourceProperties("AWS::EC2::Instance", {
      CreditSpecification: { CPUCredits: "standard" },
    });
  });

  it("encrypts every volume", () => {
    template.hasResourceProperties("AWS::EC2::Instance", {
      BlockDeviceMappings: [Match.objectLike({ Ebs: Match.objectLike({ Encrypted: true }) })],
    });
    template.hasResourceProperties("AWS::EC2::Volume", { Encrypted: true });
  });

  it("keeps the database on its own volume, snapshotted on the way out", () => {
    template.hasResource("AWS::EC2::Volume", {
      DeletionPolicy: "Snapshot",
      Properties: Match.objectLike({
        Tags: Match.arrayWith([{ Key: BACKUP_TAG.key, Value: BACKUP_TAG.value }]),
      }),
    });
    template.resourceCountIs("AWS::EC2::VolumeAttachment", 1);
  });

  it("snapshots that volume every day and keeps a week", () => {
    template.hasResourceProperties("AWS::DLM::LifecyclePolicy", {
      PolicyDetails: Match.objectLike({
        TargetTags: [{ Key: BACKUP_TAG.key, Value: BACKUP_TAG.value }],
        Schedules: [Match.objectLike({ RetainRule: { Count: 7 } })],
      }),
    });
  });

  it("can read its own secrets and nobody else's", () => {
    const statements = resources("AWS::IAM::Policy").flatMap((p) => p.Properties.PolicyDocument.Statement);
    const reads = statements.filter((s) =>
      [s.Action].flat().includes("secretsmanager:GetSecretValue"),
    );
    assert.ok(reads.length > 0);
    for (const statement of reads) {
      assert.notEqual(statement.Resource, "*", "secret reads are scoped to the stack's secrets");
    }
  });
});

describe("the edge", () => {
  const distribution = () => resources("AWS::CloudFront::Distribution")[0].Properties.DistributionConfig;

  it("redirects to HTTPS and never caches pages or API responses", () => {
    const behavior = distribution().DefaultCacheBehavior;
    assert.equal(behavior.ViewerProtocolPolicy, "redirect-to-https");
    // Managed-CachingDisabled and Managed-AllViewerAndCloudFrontHeaders-2022-06.
    assert.equal(behavior.CachePolicyId, "4135ea2d-6df8-44a3-9df3-4b5a84be39ad");
    assert.equal(behavior.OriginRequestPolicyId, "33f36d7e-f396-46d9-90e0-52428a34d9dc");
    assert.equal(behavior.Compress, false, "compressing at the edge would buffer the event stream");
  });

  it("caches only the content-hashed Next.js assets", () => {
    const cached = distribution().CacheBehaviors;
    assert.equal(cached.length, 1);
    assert.equal(cached[0].PathPattern, "/_next/static/*");
  });

  it("signs every request to the origin with a secret only it knows", () => {
    const [origin] = distribution().Origins;
    const header = origin.OriginCustomHeaders.find(
      (h: { HeaderName: string }) => h.HeaderName === ORIGIN_HEADER,
    );
    assert.ok(header, `the origin gets ${ORIGIN_HEADER}`);
    assert.match(JSON.stringify(header.HeaderValue), /resolve:secretsmanager:/);
  });

  it("reaches the host on its private address, so the plain-HTTP hop stays inside AWS", () => {
    const [origin] = distribution().Origins;
    assert.equal(origin.DomainName["Fn::GetAtt"][1], "PrivateDnsName");
    assert.ok(origin.VpcOriginConfig, "a VPC origin, not a custom origin on a public name");
    assert.equal(origin.CustomOriginConfig, undefined);
    assert.equal(origin.VpcOriginConfig.OriginReadTimeout, 60, "streamed questions pause between tokens");
    template.hasResourceProperties("AWS::CloudFront::VpcOrigin", {
      VpcOriginEndpointConfig: Match.objectLike({ OriginProtocolPolicy: "http-only", HTTPPort: 80 }),
    });
    template.resourceCountIs("AWS::EC2::EIP", 0);
  });
});

describe("deploying from GitHub", () => {
  const deployRole = () =>
    resources("AWS::IAM::Role").find((r) =>
      JSON.stringify(r.Properties.AssumeRolePolicyDocument).includes("token.actions.githubusercontent.com"),
    );

  it("trusts one repository's production environment, and nothing else", () => {
    const role = deployRole();
    assert.ok(role, "there is a role GitHub can assume");
    const [statement] = role.Properties.AssumeRolePolicyDocument.Statement;
    assert.equal(statement.Action, "sts:AssumeRoleWithWebIdentity");
    assert.deepEqual(statement.Condition, {
      StringEquals: {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:eimaieros/cadence:environment:production",
      },
    });
    assert.equal(role.Properties.MaxSessionDuration, 3600);
  });

  it("can run commands on this host, through one document, and on nothing else", () => {
    const statements = resources("AWS::IAM::Policy").flatMap((p) => p.Properties.PolicyDocument.Statement);
    const send = statements.filter((s) => [s.Action].flat().includes("ssm:SendCommand"));
    assert.equal(send.length, 1);
    const targets = JSON.stringify(send[0].Resource);
    assert.match(targets, /instance\//);
    assert.match(targets, /document\/AWS-RunShellScript/);
    assert.doesNotMatch(targets, /"\*"/);
  });

  it("reuses an existing OIDC provider when asked to", () => {
    synth().resourceCountIs("AWS::IAM::OIDCProvider", 1);
    synth({ createGithubOidcProvider: false }).resourceCountIs("AWS::IAM::OIDCProvider", 0);
  });

  it("publishes where everything is under one parameter name", () => {
    template.hasResourceProperties("AWS::SSM::Parameter", { Name: DEPLOY_PARAMETER });
  });

  it("refuses a repository that is not owner/name", () => {
    assert.throws(() => synth({ githubRepo: "cadence" }), /owner\/name/);
  });
});

describe("images", () => {
  it("are immutable by tag, scanned on push and pruned to ten releases", () => {
    for (const repository of resources("AWS::ECR::Repository")) {
      assert.equal(repository.Properties.ImageTagMutability, "IMMUTABLE");
      assert.equal(repository.Properties.ImageScanningConfiguration.ScanOnPush, true);
      assert.match(repository.Properties.LifecyclePolicy.LifecyclePolicyText, /"countNumber":10/);
    }
    template.resourceCountIs("AWS::ECR::Repository", 2);
  });
});

describe("the cost alarm", () => {
  const budget = () =>
    Template.fromStack(
      new CadenceBudgetStack(new App(), "CadenceBudget", {
        env: { ...env, region: "us-east-1" },
        alertEmail: "alerts@example.com",
      }),
    );

  it("measures cost before credits, which is what runs the credits out", () => {
    budget().hasResourceProperties("AWS::Budgets::Budget", {
      Budget: Match.objectLike({ CostTypes: { IncludeCredit: false, IncludeRefund: false } }),
    });
  });

  it("emails at 50 and 80 percent spent and at a forecast of 100", () => {
    const [resource] = Object.values(budget().findResources("AWS::Budgets::Budget"));
    const notes = resource.Properties.NotificationsWithSubscribers.map(
      (n: { Notification: { Threshold: number; NotificationType: string } }) =>
        `${n.Notification.Threshold}:${n.Notification.NotificationType}`,
    );
    assert.deepEqual(notes, ["50:ACTUAL", "80:ACTUAL", "100:FORECASTED"]);
  });

  it("refuses an address that is not one", () => {
    assert.throws(
      () =>
        new CadenceBudgetStack(new App(), "B", {
          env: { ...env, region: "us-east-1" },
          alertEmail: "nobody",
        }),
      /not an email address/,
    );
  });
});
