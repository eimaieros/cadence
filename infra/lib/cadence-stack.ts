import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  BootstraplessSynthesizer,
  CfnOutput,
  Duration,
  RemovalPolicy,
  SecretValue,
  Size,
  Stack,
  Tags,
  Validations,
  type StackProps,
} from "aws-cdk-lib";
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as dlm from "aws-cdk-lib/aws-dlm";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as cr from "aws-cdk-lib/custom-resources";
import type { Construct } from "constructs";

/*
  cadence on AWS, for a product whose traffic is a handful of interviewers a week.

      viewer ─ HTTPS ─> CloudFront ─ HTTP, inside the VPC ─> Caddy ─┬─ /api/* ─> FastAPI ─┐
                        (caches /_next/static)                      └─ /* ─────> Next.js   ├─> PostgreSQL
                                                        one t4g.small running Docker Compose    (own EBS volume)

  The shape a bigger product would have -- Fargate behind a load balancer, RDS --
  costs several times as much before the first request, and every part of it
  would idle here.

  What each piece is for:
  * CloudFront ends TLS on a *.cloudfront.net certificate (no domain needed),
    caches the content-hashed Next.js assets and is the only way in.
  * It reaches the host as a VPC origin: on the private address, through an
    interface CloudFront places in this VPC. The edge-to-host hop is plain HTTP
    and never crosses the public internet.
  * The host keeps a public address for outbound traffic only (image pulls, the
    model API). Its security group admits port 80 from CloudFront's managed
    prefix list and nothing else; there is no port 22, a shell is
    `aws ssm start-session`.
  * Every distribution in the world connects from that prefix list, so the origin
    also requires a secret header that only this distribution adds.
  * The instance is disposable. PostgreSQL lives on its own encrypted volume,
    snapshotted daily, which survives the instance being replaced.
  * Nothing holds a long-lived key. The host pulls images and reads its secrets
    through its instance role; GitHub Actions deploys through OIDC, as a role
    that trusts one repository's production environment and nothing else.
*/

const BOOTSTRAP = readFileSync(
  fileURLToPath(new URL("../../deploy/bootstrap.sh", import.meta.url)),
  "utf8",
);

/** Where the deploy workflow and the host find everything else. */
export const DEPLOY_PARAMETER = "/cadence/deploy";
/** The header CloudFront adds so the origin can tell this distribution apart. */
export const ORIGIN_HEADER = "X-Origin-Verify";
/** Volumes with this tag get a daily snapshot, kept for a week. */
export const BACKUP_TAG = { key: "cadence:backup", value: "daily" } as const;

const CLOUDFRONT_ORIGIN_FACING = "com.amazonaws.global.cloudfront.origin-facing";
// Resolved when the instance launches, not when the template is written: an AMI
// id in the template changes every time Amazon publishes an image, and
// CloudFormation would replace the host on the next unrelated deploy.
const AL2023_ARM64 = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64";
const GITHUB_OIDC = "token.actions.githubusercontent.com";

export interface CadenceStackProps extends StackProps {
  /** The GitHub repository allowed to deploy, as "owner/name". */
  readonly githubRepo: string;
  /** The GitHub environment its deploy jobs run in. */
  readonly githubEnvironment?: string;
  readonly instanceType?: string;
  /**
   * An account has one GitHub OIDC provider. Pass false if it already exists,
   * and the stack will use it instead of failing to create a second.
   */
  readonly createGithubOidcProvider?: boolean;
}

export class CadenceStack extends Stack {
  constructor(scope: Construct, id: string, props: CadenceStackProps) {
    super(scope, id, props);

    if (!/^[\w.-]+\/[\w.-]+$/.test(props.githubRepo)) {
      throw new Error(`githubRepo must be "owner/name", got "${props.githubRepo}"`);
    }
    const githubEnvironment = props.githubEnvironment ?? "production";
    Tags.of(this).add("Project", "cadence");

    // --- Network: one public subnet, no NAT gateway ----------------------------
    // A NAT gateway would cost more than everything else here combined, and the
    // host needs a public address anyway: CloudFront reaches it by DNS name.
    const vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr("10.42.0.0/24"),
      maxAzs: 1,
      natGateways: 0,
      subnetConfiguration: [{ name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 26 }],
      restrictDefaultSecurityGroup: true,
    });

    const originFacing = new cr.AwsCustomResource(this, "CloudFrontOriginFacing", {
      onUpdate: {
        service: "EC2",
        action: "describeManagedPrefixLists",
        parameters: {
          Filters: [{ Name: "prefix-list-name", Values: [CLOUDFRONT_ORIGIN_FACING] }],
        },
        physicalResourceId: cr.PhysicalResourceId.of(CLOUDFRONT_ORIGIN_FACING),
        outputPaths: ["PrefixLists.0.PrefixListId"],
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE,
      }),
      installLatestAwsSdk: false,
    });

    const originSecurityGroup = new ec2.SecurityGroup(this, "OriginSecurityGroup", {
      vpc,
      description: "cadence origin: HTTP from CloudFront only; no SSH, shell access is SSM",
      allowAllOutbound: true,
    });
    originSecurityGroup.addIngressRule(
      ec2.Peer.prefixList(originFacing.getResponseField("PrefixLists.0.PrefixListId")),
      ec2.Port.tcp(80),
      "CloudFront origin-facing addresses",
    );

    // --- Secrets ----------------------------------------------------------------
    const generated = (id: string, description: string, length: number) =>
      new secretsmanager.Secret(this, id, {
        description,
        generateSecretString: { passwordLength: length, excludePunctuation: true },
        removalPolicy: RemovalPolicy.DESTROY,
      });
    const dbPassword = generated("DbPassword", "cadence: PostgreSQL password", 40);
    const jwtSecret = generated("JwtSecret", "cadence: JWT signing key", 64);
    const originVerify = generated(
      "OriginVerify",
      `cadence: value of ${ORIGIN_HEADER}, which only this stack's CloudFront adds`,
      48,
    );
    // Created with a placeholder so the stack never holds a real key. Until the
    // value starts with sk-ant-, release.sh leaves ANTHROPIC_API_KEY empty and
    // the API runs the scripted interviewer, which costs nothing per session.
    const anthropicKey = new secretsmanager.Secret(this, "AnthropicApiKey", {
      description:
        "cadence: Anthropic API key. 'unset' runs the scripted interviewer; put a real key here and redeploy to use the model.",
      secretStringValue: SecretValue.unsafePlainText("unset"),
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // --- Images and logs ---------------------------------------------------------
    const repository = (name: "api" | "web") =>
      new ecr.Repository(this, `${name === "api" ? "Api" : "Web"}Repository`, {
        repositoryName: `cadence/${name}`,
        imageScanOnPush: true,
        // Tags are git SHAs. Immutable means a tag always names the code it was built from.
        imageTagMutability: ecr.TagMutability.IMMUTABLE,
        lifecycleRules: [{ description: "keep the last ten releases", maxImageCount: 10 }],
        removalPolicy: RemovalPolicy.DESTROY,
        emptyOnDelete: true,
      });
    const apiRepository = repository("api");
    const webRepository = repository("web");

    const logGroup = new logs.LogGroup(this, "AppLogs", {
      logGroupName: "/cadence/app",
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // --- The host ----------------------------------------------------------------
    const hostRole = new iam.Role(this, "HostRole", {
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      description: "cadence host: SSM, pull its two images, read its own secrets, write its logs",
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName("AmazonSSMManagedInstanceCore")],
    });
    apiRepository.grantPull(hostRole);
    webRepository.grantPull(hostRole);
    for (const secret of [dbPassword, jwtSecret, originVerify, anthropicKey]) {
      secret.grantRead(hostRole);
    }
    logGroup.grantWrite(hostRole);

    const host = new ec2.Instance(this, "Host", {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      instanceType: new ec2.InstanceType(props.instanceType ?? "t4g.small"),
      machineImage: ec2.MachineImage.resolveSsmParameterAtLaunch(AL2023_ARM64, {
        os: ec2.OperatingSystemType.LINUX,
      }),
      securityGroup: originSecurityGroup,
      role: hostRole,
      userData: ec2.UserData.custom(BOOTSTRAP),
      // IMDSv2 only, and one network hop: the host can read its credentials,
      // the containers it runs cannot.
      httpTokens: ec2.HttpTokens.REQUIRED,
      httpPutResponseHopLimit: 1,
      // Standard credits: if the CPU runs hot for long, the instance slows down
      // instead of billing surplus credits, which the free trial does not cover.
      creditSpecification: ec2.CpuCredits.STANDARD,
      blockDevices: [
        {
          deviceName: "/dev/xvda",
          volume: ec2.BlockDeviceVolume.ebs(16, {
            encrypted: true,
            volumeType: ec2.EbsDeviceVolumeType.GP3,
          }),
        },
      ],
      propagateTagsToVolumeOnCreation: true,
      ssmSessionPermissions: false,
    });
    Validations.of(host).acknowledge({
      id: "CloudFormation-Validate::E1152",
      reason: "resolve:ssm: is a valid ImageId: EC2 resolves the parameter at launch",
    });

    const data = new ec2.Volume(this, "Data", {
      availabilityZone: vpc.publicSubnets[0].availabilityZone,
      size: Size.gibibytes(8),
      volumeType: ec2.EbsDeviceVolumeType.GP3,
      encrypted: true,
      // Deleting the stack leaves a final snapshot behind rather than the data.
      removalPolicy: RemovalPolicy.SNAPSHOT,
    });
    Tags.of(data).add(BACKUP_TAG.key, BACKUP_TAG.value);
    new ec2.CfnVolumeAttachment(this, "DataAttachment", {
      instanceId: host.instanceId,
      volumeId: data.volumeId,
      device: "/dev/sdf",
    });

    const snapshotRole = new iam.Role(this, "SnapshotRole", {
      assumedBy: new iam.ServicePrincipal("dlm.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName("service-role/AWSDataLifecycleManagerServiceRole"),
      ],
    });
    new dlm.CfnLifecyclePolicy(this, "DailySnapshots", {
      // The API only accepts letters, digits, spaces, "_" and "-" here.
      description: "cadence data volume daily snapshot kept for seven days",
      state: "ENABLED",
      executionRoleArn: snapshotRole.roleArn,
      policyDetails: {
        policyType: "EBS_SNAPSHOT_MANAGEMENT",
        resourceTypes: ["VOLUME"],
        targetTags: [{ key: BACKUP_TAG.key, value: BACKUP_TAG.value }],
        schedules: [
          {
            name: "daily",
            copyTags: true,
            createRule: { interval: 24, intervalUnit: "HOURS", times: ["03:00"] },
            retainRule: { count: 7 },
          },
        ],
      },
    });

    // --- The edge ----------------------------------------------------------------
    // A VPC origin: CloudFront reaches the host on its private address, through
    // an interface it places in this VPC. The hop from the edge to the host is
    // plain HTTP, and it never crosses the public internet.
    const origin = origins.VpcOrigin.withEc2Instance(host, {
      vpcOriginName: "cadence-host",
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      httpPort: 80,
      // The ceiling for one gap between bytes, not for a whole response: a
      // streamed question keeps the connection busy token by token.
      readTimeout: Duration.seconds(60),
      keepaliveTimeout: Duration.seconds(60),
      customHeaders: { [ORIGIN_HEADER]: originVerify.secretValue.unsafeUnwrap() },
    });
    const distribution = new cloudfront.Distribution(this, "Edge", {
      comment: "cadence",
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      defaultBehavior: {
        origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        // Pages and API responses are per user: never cached. Forwarding every
        // viewer header is what carries Authorization to the API, and with it
        // CloudFront-Viewer-Address, which the rate limiter keys on.
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_AND_CLOUDFRONT_2022,
        // Compressing at the edge would buffer server-sent events.
        compress: false,
      },
      additionalBehaviors: {
        // Content-hashed file names: safe to cache for as long as CloudFront likes.
        "/_next/static/*": {
          origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
          compress: true,
        },
      },
    });
    const publicUrl = `https://${distribution.distributionDomainName}`;

    const deployConfig = new ssm.StringParameter(this, "DeployConfig", {
      parameterName: DEPLOY_PARAMETER,
      description: "cadence: what the deploy workflow and the host need to find each other",
      stringValue: this.toJsonString({
        instanceId: host.instanceId,
        apiRepository: apiRepository.repositoryUri,
        webRepository: webRepository.repositoryUri,
        publicUrl,
        logGroup: logGroup.logGroupName,
        secrets: {
          dbPassword: dbPassword.secretArn,
          jwtSecret: jwtSecret.secretArn,
          originVerify: originVerify.secretArn,
          anthropicApiKey: anthropicKey.secretArn,
        },
      }),
    });
    // By name, not through deployConfig.grantRead: the parameter holds the
    // instance id, the instance waits for this policy, and a reference to the
    // parameter here would close that loop.
    hostRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:${this.partition}:ssm:${this.region}:${this.account}:parameter${DEPLOY_PARAMETER}`,
        ],
      }),
    );

    // --- Deploying from GitHub ---------------------------------------------------
    const github =
      props.createGithubOidcProvider === false
        ? iam.OidcProviderNative.fromOidcProviderArn(
            this,
            "GitHubOidc",
            `arn:${this.partition}:iam::${this.account}:oidc-provider/${GITHUB_OIDC}`,
          )
        : new iam.OidcProviderNative(this, "GitHubOidc", {
            url: `https://${GITHUB_OIDC}`,
            clientIds: ["sts.amazonaws.com"],
          });
    const deployRole = new iam.Role(this, "DeployRole", {
      description: `GitHub Actions, ${props.githubRepo} in its ${githubEnvironment} environment: push images, release on the host`,
      maxSessionDuration: Duration.hours(1),
      assumedBy: new iam.WebIdentityPrincipal(github.oidcProviderArn, {
        StringEquals: {
          [`${GITHUB_OIDC}:aud`]: "sts.amazonaws.com",
          // An environment, not a branch: a workflow on any branch can name a
          // branch, but only jobs the environment admits get this subject.
          [`${GITHUB_OIDC}:sub`]: `repo:${props.githubRepo}:environment:${githubEnvironment}`,
        },
      }),
    });
    apiRepository.grantPullPush(deployRole);
    webRepository.grantPullPush(deployRole);
    deployConfig.grantRead(deployRole);
    deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:SendCommand"],
        resources: [
          `arn:${this.partition}:ec2:${this.region}:${this.account}:instance/${host.instanceId}`,
          `arn:${this.partition}:ssm:${this.region}::document/AWS-RunShellScript`,
        ],
      }),
    );
    deployRole.addToPolicy(
      new iam.PolicyStatement({
        // Neither action supports resource-level permissions.
        actions: ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
        resources: ["*"],
      }),
    );

    new CfnOutput(this, "Url", { value: publicUrl, description: "cadence, live" });
    new CfnOutput(this, "DeployRoleArn", {
      value: deployRole.roleArn,
      description: "GitHub repository variable AWS_DEPLOY_ROLE_ARN",
    });
    new CfnOutput(this, "InstanceId", {
      value: host.instanceId,
      description: "aws ssm start-session --target <this>",
    });
    new CfnOutput(this, "AnthropicSecretName", {
      value: anthropicKey.secretName,
      description: "Put an Anthropic key here and redeploy to replace the scripted interviewer",
    });
  }
}

export interface CadenceBudgetStackProps extends StackProps {
  /** Where the alerts go. */
  readonly alertEmail: string;
  /** Monthly cost, before credits, that the alerts are measured against. */
  readonly monthlyBudgetUsd?: number;
}

/**
 * The cost alarm, in a stack of its own because AWS Budgets is a global
 * service whose CloudFormation resource only exists in us-east-1. It has no
 * assets, so it deploys without bootstrapping that region.
 */
export class CadenceBudgetStack extends Stack {
  constructor(scope: Construct, id: string, props: CadenceBudgetStackProps) {
    super(scope, id, { ...props, synthesizer: new BootstraplessSynthesizer() });

    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(props.alertEmail)) {
      throw new Error(`alertEmail is not an email address: "${props.alertEmail}"`);
    }
    Tags.of(this).add("Project", "cadence");

    new budgets.CfnBudget(this, "MonthlyBudget", {
      budget: {
        budgetName: "cadence-monthly",
        budgetType: "COST",
        timeUnit: "MONTHLY",
        budgetLimit: { amount: props.monthlyBudgetUsd ?? 20, unit: "USD" },
        // Gross, before credits. On the free plan the net cost is zero until the
        // credits run out -- the one moment an alert would no longer help.
        costTypes: { includeCredit: false, includeRefund: false },
      },
      notificationsWithSubscribers: (
        [
          [50, "ACTUAL"],
          [80, "ACTUAL"],
          [100, "FORECASTED"],
        ] as const
      ).map(([threshold, notificationType]) => ({
        notification: {
          notificationType,
          comparisonOperator: "GREATER_THAN",
          threshold,
          thresholdType: "PERCENTAGE",
        },
        subscribers: [{ subscriptionType: "EMAIL", address: props.alertEmail }],
      })),
    });
  }
}
