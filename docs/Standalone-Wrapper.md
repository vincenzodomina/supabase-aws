# Standalone wrapper

The optional [`wrapper/`](../wrapper/) supplies the shared resources needed to try Supabase on AWS without writing a surrounding infrastructure project. One command deploys a small foundation stack, prepares SES SMTP credentials, and invokes the unchanged [`src/deploy.sh`](../src/deploy.sh) for the Supabase stack.

This is a “hello world” deployment for a test AWS account. It demonstrates RDS compatibility and ECS hosting; it is not a complete cloud foundation for a new product, a production configuration, or evidence of compliance. The primary integration path remains [copying `src/` into existing infrastructure](Deployment.md#embedding-source).

## Before starting

- Use a test account and a deployment identity with the [core deployment permissions](Deployment.md#shared-resources-and-permissions), plus permission to manage VPC networking, ACM certificates, Private CA, SES identities, Route 53 records, and an IAM user/access key for SMTP. Account policies and service quotas must permit these resources.
- Provide an **existing public Route 53 hosted zone in the same account, already delegated from its registrar**, and an unused subdomain such as `supabase.example.com`. The wrapper creates certificates and SES/DNS records there. It does not register a domain, move DNS, or take over an existing SES identity. An undelegated zone leaves ACM validation waiting.
- Use `us-east-1` by default, or select a region supporting the pinned RDS version, Fargate, SES SMTP, and Private CA. The region must have at least two usable Availability Zones and enough Fargate capacity/quota for deployment overlap.
- Install the [deployment tools](../README.md#requirements) and obtain this repository. Docker is not needed for deployment.

A fresh AWS account still needs domain delegation and suitable permissions. [SES sandbox restrictions](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html) permit delivery only to verified recipients or the mailbox simulator; production sending approval is not necessary for a restricted demonstration.

## Deploy

From the repository root:

```sh
./wrapper/deploy.sh --domain supabase.example.com --hosted-zone-id Z0123456789EXAMPLE
```

Optional flags are `--name supabase-demo`, `--region us-east-1`, and `--profile PROFILE`. The default stack names are `supabase-demo-foundation` and `supabase-demo-supabase`. Rerun the same command for updates or retries; unchanged stacks succeed. A failure stops subsequent steps and leaves any created resources available for inspection and cleanup. SES verification can take time: if its waiter expires, check the DNS records and rerun once verified.

For browser-based setup, [open AWS CloudShell](https://console.aws.amazon.com/cloudshell/home?region=us-east-1), obtain a reviewed checkout, and run the same command. CloudShell uses the signed-in AWS identity. While this repository is private, cloning requires GitHub access; alternatively download its ZIP through GitHub and upload/extract it in CloudShell. Check the required tool versions before deployment.

The README button opens the shell; it does not execute code or bypass AWS setup. A literal [CloudFormation Launch Stack link](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-console-create-stacks-quick-create-links.html) requires a hosted template and prepared deployment artifacts. This starter uses the existing source packaging path and needs no public artifact host, build pipeline, or custom image.

## What it creates

| Foundation component | Purpose |
| --- | --- |
| VPC `10.42.0.0/16`, two public and two private subnets across two AZs | Public ALB placement; private ECS, Lambda, and RDS placement. Database and application subnets are shared for this demonstration. |
| Internet gateway, routing, one NAT gateway and Elastic IP | Outbound image pulls, AWS API access, and SES SMTP from private workloads. The single NAT is a deliberate availability tradeoff. |
| S3 gateway endpoint | Same-region S3 traffic without NAT processing; its default policy also permits CloudFormation callbacks, subject to IAM. |
| Private, encrypted artifact bucket with HTTPS-only policy | Existing deployer uploads its template, Lambda ZIP, and RDS CA bundle. |
| DNS-validated ACM API certificate | Trusted public HTTPS for the supplied hostname. |
| Short-lived Private CA, root certificate, and activation | Supplies the core stack's [Service Connect TLS](Networking.md#service-connect-tls) mode without weakening internal encryption. |
| SES domain identity and three DKIM records | Verifies the API subdomain for sending as `noreply@<API domain>`. SES supplies its default MAIL FROM domain. |
| Restricted SMTP IAM user/access key and Secrets Manager secret | Sends only from that address using that SES identity. The wrapper derives the regional SMTP password before deploying Auth. |

The existing Supabase stack creates RDS, ECS services, the public HTTPS ALB and API DNS alias, the Storage bucket, service secrets, and logs. Application containers, Studio, VPNs, custom images, registries, extra pipelines, and enterprise account governance are outside the wrapper.

All wrapper code lives in three files: `foundation.yaml`, `deploy.sh`, and `smtp-password.py`. The [AWS-documented SMTP derivation](https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html) uses Python's standard library. Credentials pass through permission-restricted temporary files, never command arguments or stack outputs. The original IAM secret may remain in Secrets Manager version history; treat every version as sensitive. Reruns reuse the converted current version.

## Demo defaults and cost

| Setting | Standalone choice |
| --- | --- |
| Access | Public HTTPS API; task IPs and database remain private; Studio and public signups remain disabled. |
| Capacity | One task per core service, maximum one; single-AZ `db.t4g.medium`. This profile does not demonstrate autoscaling or high availability. |
| Encryption | Service Connect TLS with managed proxies; verified database TLS and standard encrypted storage remain enabled. |
| Retention | One day of automated RDS backups; seven days of application/service logs; database deletion protection and snapshots remain enabled. |
| Telemetry | Container Insights disabled; ordinary service metrics, logs, and existing alarms remain. |
| Application URL | The API URL is also the default Auth redirect URL. No frontend or email-link landing page is deployed. |

**This is not a free-tier deployment.** RDS, Fargate, ALB, NAT, public IPv4 addresses, Private CA and certificate issuance, secrets, and storage/logging incur charges while active or retained. One task still includes TLS proxy capacity. Review regional pricing before launching, use synthetic data, and remove the deployment promptly after testing. [Short-lived CAs are AWS's recommended Service Connect choice](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect-tls.html); the CA remains a material cost even in a small demo.

For a real application, embed the source into an enterprise foundation and select capacity, private access, retention, outbound controls, budgets/alerts, recovery, and operational policies for that workload.

## Connect and verify

The command prints `ApiUrl` and `ApiSecretArn`. Retrieve only the publishable key for an application client:

```sh
aws secretsmanager get-secret-value --secret-id API_SECRET_ARN --region us-east-1 \
  --query SecretString --output text | jq -r .SUPABASE_PUBLISHABLE_KEY
```

Use that key and the HTTPS URL with a Supabase client. Keep privileged keys on trusted servers. New database tables still need grants and RLS; a demo deployment does not create application schemas or users. Verify a test recipient in SES before testing invitations or recovery emails. A real frontend needs its own `SiteUrl` and redirect allow list through the normal integration path.

Complete the separately approved [manual AWS acceptance](Deployment.md#manual-aws-acceptance), including TLS target health, private services, SES delivery, RDS privileges, and S3 access. Local script tests and template linting cannot establish live AWS behavior.

## Cleanup

Delete Supabase **before** its foundation: ECS needs the CA, networking, SMTP secret, and artifacts until its services have drained. Record both stacks' outputs first so retained resources remain discoverable.

Use the same AWS identity/profile as deployment. For the default names, this block removes database deletion protection and deletes the stacks in order, stopping if any command fails:

```sh
(
set -e
region=us-east-1
name=supabase-demo
aws cloudformation describe-stacks --stack-name "$name-foundation" --region "$region"
aws cloudformation describe-stacks --stack-name "$name-supabase" --region "$region"
db=$(aws cloudformation describe-stack-resource --stack-name "$name-supabase" \
  --logical-resource-id Database --region "$region" --query StackResourceDetail.PhysicalResourceId --output text)
aws rds modify-db-instance --db-instance-identifier "$db" --no-deletion-protection \
  --apply-immediately --region "$region"
aws cloudformation delete-stack --stack-name "$name-supabase" --region "$region"
aws cloudformation wait stack-delete-complete --stack-name "$name-supabase" --region "$region"
aws cloudformation delete-stack --stack-name "$name-foundation" --region "$region"
aws cloudformation wait stack-delete-complete --stack-name "$name-foundation" --region "$region"
)
```

If deletion fails, inspect the stack events before continuing; do not delete the foundation while services still depend on it. For partially created stacks, delete only the stacks/resources that exist and inspect any retained resources from rollback.

Snapshots, the versioned Storage bucket and its objects, the artifact bucket, and retained secrets/logs require separate review and deletion when no longer needed. Removing only the stacks does not remove all charges. The wrapper's IAM access key is revoked when its foundation is deleted; its SMTP secret is not retained. Review remaining DNS validation records in the shared zone. The existing hosted zone and registered domain remain owned by the account.
