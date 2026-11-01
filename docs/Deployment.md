# Deployment and integration

The [README quick start](../README.md#deploy) deploys the stack in one command. This guide covers permissions, embedding the source in other infrastructure, optional administration, and manual AWS acceptance.

## Shared resources and permissions

- An AWS account and permission to deploy CloudFormation, IAM, ECS, RDS, S3, Secrets Manager, Cloud Map, Lambda, CloudWatch, and load balancers. Optional DNS and Service Connect TLS configuration also require the corresponding Route 53, Private CA, and KMS permissions.
- An existing ACM certificate in the deployment region covering the API hostname and, if enabled, the Studio hostname.
- An existing private S3 bucket in the deployment region for templates, Lambda ZIPs, and the RDS CA bundle. The deployer needs `s3:PutObject`/`s3:GetObject`; certificate readers receive access to only their CA object. Uploads use SSE-S3 encryption. Retain artifacts while referenced by current or rollback deployments; task replacements still download the CA bundle.
- An SES-verified sender identity with production sending access, plus a Secrets Manager JSON secret containing `username` and `password` for **SES SMTP credentials**. These are not ordinary AWS API credentials. SES identity verification, sandbox removal, quotas, and SMTP credential rotation remain account-level responsibilities.
- If that secret uses a customer-managed KMS key, supply `SesSmtpKmsKeyArn`. The key policy must allow the generated `AuthExecutionRole` to decrypt, directly or through IAM delegation; cross-account secrets also need a secret resource policy. The stack grants decryption only through Secrets Manager for that secret, and creates no key. Leave the parameter empty for the AWS-managed Secrets Manager key.

See [networking](Networking.md) for subnets, private clients, DNS, outbound access, and encryption prerequisites.

## Execution and packaging

Run commands from the repository root with the tools listed in the [README](../README.md#requirements). Packaging downloads hash-verified Linux wheels; Docker is only required for local integration tests.

The deploy command packages and uploads the template, Lambda ZIP, and CA bundle, then creates and executes a CloudFormation change set. Repeated deployments use current inputs; an unchanged stack succeeds. There is no built-in preview or saved-change-set execution mode. Use the consuming pipeline's native approval flow if required.

Optionally pass `--role-arn ARN` to use an existing CloudFormation execution role that trusts `cloudformation.amazonaws.com` and has permission to manage the stack resources and read its artifacts. The caller needs `iam:PassRole` for that role, plus artifact-upload and CloudFormation permissions, including stack-policy access. [CloudFormation keeps the role associated with the stack](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-iam-servicerole.html) for later operations even when this flag is omitted; scope both role and stack access carefully.

CloudFormation provisions RDS, invokes the bootstrap Lambda, and starts ECS services only after initialization succeeds. The Lambda connects to RDS with verified TLS, initializes database prerequisites, and populates service credentials. Changes to the bootstrap package or resource properties, including `CredentialRevision`, rerun initialization while preserving existing signing keys. Deleting the bootstrap resource never alters the database. The bootstrap initializes only the roles, schemas, JWT helpers, and extensions needed by the selected services; Auth and Storage apply their own release-specific migrations. Unsupported Supabase extensions and logical replication slots are not installed.

The Bash deployer uploads artifacts under content-addressed S3 keys and passes `ArtifactsBucket`, `BootstrapKey`, and `CertificateKey` to the template. Keep those parameters out of your parameter file. `./src/deploy.sh --package /tmp/bootstrap.zip` produces the Lambda package without AWS credentials; Python, SQL, dependencies, and the CA bundle remain separate files under `src/`. Other pipelines can package/upload these artifacts and deploy the same template.

The deployer installs a CloudFormation stack policy blocking database replacement/removal, preserving existing restrictions. It applies the policy before updates and immediately after successful first creation. Stack policies are an API setting, not a template resource; database deletion protection and snapshot policies are declared in the YAML. Each deployment creates and executes a fresh change set from the current files. Review changes before deployment; an ECS rollback does not reverse database migrations.

Envoy uses its pinned official image. `src/deploy.sh` embeds `src/gateway-entrypoint.sh` into the template before upload; the `@include` marker is local packaging syntax, not a CloudFormation feature. Use `./src/deploy.sh --template > rendered.yaml` to produce the deployable template for review or another pipeline (with its S3 artifact parameters), without AWS credentials. Gateway configuration lives in `src/supabase.yaml`, and secrets are injected by ECS at task launch.

## Embedding configuration

Stack outputs include `ApiSecurityGroupId`, `GatewaySecurityGroupId`, `DatabaseSecurityGroupId`, and `DatabasePort`. `ServiceConnectNamespaceArn` is available only in Service Connect TLS mode. Outputs create no CloudFormation exports or additional access rules; the consuming infrastructure chooses how to pass them to clients. For application stacks deployed later, see [consumer-managed API ingress](Networking.md#api-access-and-dns). Joining the Service Connect namespace also requires compatible client service configuration and explicit network access; ordinary VPC clients should use the HTTPS API URL.

`StorageTenantId` defaults to the stack name when empty. Supply the original tenant ID when preserving an existing Storage object layout: S3 object keys include this prefix. Keep the bucket, tenant ID, and database Storage metadata together. Changing the parameter does not migrate objects and can make existing files unreachable.

## Optional administration

Set `EnableStudio=true`, provide `StudioDomainName`, and narrow `StudioClientCidr` to your private/VPN clients. Create the Studio DNS record pointing at `StudioLoadBalancerDnsName`; it must resolve for those clients. The certificate must cover this hostname.

Studio has privileged database access. Network access is its access-control boundary in this version: there is no public administration endpoint or built-in per-user Studio login. Only trusted administrators should have access to the allowed network. postgres-meta listens within the shared task and has no load balancer or public gateway route. Disable both components together with `EnableStudio=false`.

Studio snippets are ephemeral across task replacement. Store durable schema changes in your own version-controlled migrations rather than relying on Studio's local files.

Fresh deployments with Studio disabled create no administration resources. Disabling an existing installation removes its administration resources from the stack but retains `MetaSecret`; re-enabling generates a new meta encryption key. Review retained secrets separately before removing them when no longer needed.

## Manual AWS acceptance

Install the [test dependencies](../CONTRIBUTION.md#validation) and run commands from the repository root.

Manual AWS acceptance is separate from the local/CI suite. Before production use, deploy an approved disposable AWS stack and verify: database bootstrap; all ECS services stable; actual signup/invitation and recovery emails through SES; token refresh and MFA; authenticated/anonymous RLS behavior; private S3 upload/download/delete and signed URLs; task replacement under traffic; Studio unreachable outside its allowed network; and snapshot restoration.

From a host with private API access and the test dependencies installed, use `.venv/bin/python tests/acceptance.py --stack-name NAME --region REGION --profile PROFILE` for a Storage API round trip. It creates and deletes test data, requires access to the API secret, and leaves S3 version history subject to the bucket retention policy.

Also complete [RDS privilege and migration acceptance](RDS%20Compatibility.md#manual-rds-acceptance), mode-specific [networking acceptance](Networking.md#capacity-and-rollout), and [workload sizing](Compute.md#rds-workload-sizing). Local tests do not establish RDS restrictions, managed networking, or compliance.

## Removal

To remove a deployment, first disable database deletion protection explicitly. RDS snapshots, S3 data, secrets, and logs are retained by policy; review and remove retained resources separately when they are no longer needed.
