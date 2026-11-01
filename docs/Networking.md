# Networking

`InternalTrafficEncryption` selects one of two protected deployment modes. API clients use the existing HTTPS Supabase URL in either mode. RDS clients continue to verify database TLS; S3 and AWS APIs use HTTPS, and SES SMTP uses STARTTLS.

| Mode | Shared prerequisite | Stack behavior |
| --- | --- | --- |
| `vpc` (default) | Existing VPC Encryption Controls in `enforce` mode with state `available` | Bootstrap checks the selected VPC before database initialization and ECS deployment. Services use Cloud Map DNS without managed proxy containers. |
| `service-connect-tls` | Existing same-region AWS Private CA, with the tag `AmazonECSManaged=true` | ECS Service Connect adds managed TLS proxies and certificate rotation. Both ALBs use HTTPS targets and health checks. |

The surrounding infrastructure owns the VPC, CA, and any supplied KMS key. Neither mode provisions them or changes the VPC's encryption settings.

## VPC enforcement

The network owner must prepare and enable [VPC Encryption Controls](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-encryption-controls.html) before deployment. AWS migrates supported ALB/Fargate resources to encryption-capable infrastructure. Monitor mode alone is insufficient. Review region/AZ support, RDS instance compatibility, and the feature's shared-network charges.

The provider requires `ec2:DescribeVpcEncryptionControls` and HTTPS access to the regional EC2 API, through existing egress or an interface endpoint. Its custom resource checks mode, state, and VPC identity on creation and when its properties/package change. Missing, transitional, or unreadable enforcement fails closed. This does not continuously monitor the VPC or block a later administrator change. Other stack resources may already exist when the check runs; ECS services depend on its success.

The VPC owner remains responsible for exclusions, including the VPC-attached bootstrap Lambda and any NAT/internet gateways or peering paths. The stack's Lambda uses verified database TLS and HTTPS for its AWS calls. Review permitted paths outside the enforced boundary; security-group isolation alone does not supply encryption. Retain enforcement while this mode is deployed.

Read-only verification:

```sh
aws ec2 describe-vpc-encryption-controls --region "$region" \
  --vpc-ids "$vpc_id" --query 'VpcEncryptionControls[].{Vpc:VpcId,Mode:Mode,State:State,Exclusions:ResourceExclusions}'
```

## Service Connect TLS

Set `InternalTrafficEncryption=service-connect-tls` and supply `ServiceConnectPrivateCaArn`. Prefer a short-lived CA. Optionally supply `ServiceConnectKmsKeyArn` for an existing symmetric key whose policy permits the generated infrastructure role; otherwise ECS uses an AWS-owned key. The CloudFormation execution identity needs permission to pass that role to `ecs.amazonaws.com`.

[Service Connect](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect-tls.html) manages per-service certificates in Secrets Manager and rotates them automatically. Keep the CA, infrastructure role, key permissions, and managed secrets available until services finish draining. Do not manually delete or replicate the managed secrets, extend rotation beyond certificate validity, or grant applications access to their private keys. Monitor rotation failures through the organization's CloudTrail/CloudWatch tooling.

The conditional infrastructure role uses [AWS's managed TLS policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonECSInfrastructureRolePolicyForServiceConnectTransportLayerSecurity.html): it permits operations on ECS-owned `ecs-sc!` secrets and tagged CAs. Its scope is broader than a single stack's CA; restrict who can pass this role and tag eligible CAs. An optional KMS grant is limited to the supplied key. Application task roles gain no certificate-read permission.

All five ECS services, including optional administration, use the same HTTP namespace and existing client names. No Route 53 private discovery zone is created in this mode. Application HTTP URLs address task-local proxies; traffic between proxies is TLS. Studio/meta and readiness checks stay within their task network namespace. Only named Service Connect endpoints are covered; arbitrary IP connections and other external destinations are not automatically encrypted. Existing security groups retain their restricted paths.

Both ALBs use HTTPS on the existing task ports, with TLS 1.3 listener policies and matching HTTPS health checks. No proxy-bypass port is enabled. [ALB does not validate target certificates](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html); backend encryption must not be described as mutual TLS or ALB validation of server identity.

Service Connect proxy logs use the existing log group. Per-request and idle proxy timeouts are disabled to avoid introducing a 15-second transfer limit; existing gateway/application/ALB limits still apply. Backend services start before their clients so the endpoint names are available to new tasks. [AWS's Service Connect deployment guide](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-connect-concepts-deploy.html) describes endpoint discovery and proxy sizing.

## Capacity and rollout

With default parameters, effective per-task allocations are:

| Workload | VPC mode: CPU / memory | TLS mode: CPU / memory |
| --- | --- | --- |
| Gateway, Auth, REST | 256 units / 512 MiB | 512 units / 1,024 MiB |
| Storage | 256 units / 1,024 MiB | 512 units / 2,048 MiB |
| Optional Studio/meta | 1,024 units / 2,048 MiB | 2,048 units / 4,096 MiB |

TLS raises only baseline CPU/memory choices; larger operator values remain unchanged and must include proxy headroom. AWS recommends at least 256 additional CPU units and 64 MiB for a proxy, with more for high request rates. Check effective task allocations for the selected parameters and validate the Fargate CPU/memory combination. The default eight core tasks therefore allocate 4 vCPU/10 GiB in TLS mode versus 2 vCPU/5 GiB in VPC mode. Include proxy capacity, certificate issuance, secrets, and optional KMS charges in the comparison.

Changing modes replaces ALB target groups and rolls services. Mixed HTTP/TLS generations can fail during the transition: use a planned maintenance window, quiesce application traffic, and verify every backend before reopening access. Review the source and parameters for resource replacements; preserve database and object resources. Existing tasks need replacement to learn new Service Connect endpoints. Rollback also needs the previous network prerequisites to remain available.

Manual AWS acceptance must cover private client access, allowed and denied service paths, HTTPS target health, plaintext rejection in TLS mode, task replacement, scaling, certificate rotation, and VPC enforcement/exclusions where applicable. Local checks verify configuration, prerequisite failures, and Envoy host-file aliases; Docker/Moto does not reproduce the managed Service Connect dataplane or establish a compliance certification.

## Shared network prerequisites

The existing VPC needs DNS support/hostnames, two private application subnets, and private database subnets spanning at least two Availability Zones. Application and database subnets may overlap. The stack creates no NAT gateways or VPC endpoints.

| Caller / destination | Port | Shared network and policy requirements |
| --- | --- | --- |
| ECS image pulls → Docker Hub and `public.ecr.aws` | TCP 443 | Public registry, authentication, and layer-download hosts need [outbound routing](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/networking-outbound.html). Private ECR endpoints do not provide access to these registries. |
| ECS secret injection and bootstrap → Secrets Manager | TCP 443 | Reach the region containing each secret; an interface endpoint needs private DNS and a policy permitting `GetSecretValue`, plus bootstrap's `PutSecretValue` on generated service secrets. |
| Network prerequisite check → EC2 API | TCP 443 | VPC encryption mode requires regional `DescribeVpcEncryptionControls` access through existing egress or an interface endpoint; see [VPC enforcement](#vpc-enforcement). |
| ECS logging → CloudWatch Logs | TCP 443 | Regional Logs endpoint or shared egress; permit execution-role `CreateLogStream` and `PutLogEvents` on the stack log group. Optional service-log retention also needs the bootstrap role's `CreateLogGroup`/`PutRetentionPolicy` access to its named service groups. |
| Storage and certificate helpers → S3 | TCP 443 | Permit the Storage role's bucket/object and multipart actions on the stack bucket, and task-role `GetObject` on the artifact bucket's CA object. |
| Bootstrap → CloudFormation's presigned S3 response URL | TCP 443 | S3 endpoint policies must permit the callback's signed `PutObject` to `cloudformation-custom-resource-response-<region-without-dashes>/*`. Do not restrict this solely to project buckets or the Lambda role. A CloudFormation API endpoint alone is insufficient. [AWS callback requirements](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/vpc-interface-endpoints.html). |
| Auth → `email-smtp.<SesRegion>.amazonaws.com` | TCP 587, STARTTLS | Shared egress or an [SES SMTP interface endpoint](https://docs.aws.amazon.com/ses/latest/dg/send-email-set-up-vpc-endpoints.html) in the selected region; allow Auth's security group and verify regional/AZ availability. |

Reuse an [S3 gateway endpoint](https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html) on the application-subnet route tables for same-region Storage, certificate downloads, and callbacks. It avoids NAT data processing for that traffic and has no additional endpoint charge. Keep ownership in the surrounding infrastructure. Interface endpoints have separate costs; use existing enterprise endpoints where required. Enable their private DNS and allow the callers on the listed ports in endpoint security groups and network ACLs, including return traffic. Endpoint policies restrict access in addition to IAM and bucket/secret policies; they do not grant it.

## API access and DNS

The API defaults to `ApiExposure=internal`, using the private application subnets. Set `ApiClientSecurityGroupId` to the existing security group of the consuming ECS application or other approved clients; those clients also need outbound HTTPS access. Browsers and Auth email-link users must have private network access to this endpoint. For public clients, explicitly choose `ApiExposure=internet-facing` and provide `PublicSubnetIds`.

If `HostedZoneId` is supplied, the stack creates the API alias in that existing zone: use a private zone associated with the VPC for internal access, or a public zone for internet-facing access. Otherwise manage the corresponding DNS yourself, pointing `ApiDomainName` at `ApiLoadBalancerDnsName`. DNS must match the supplied certificate. The API is HTTPS-only.

Existing public deployments must explicitly set `ApiExposure=internet-facing` to preserve their exposure. Changing the scheme replaces the ALB and updates its DNS target; review the ALB replacement and plan a client/DNS cutover before switching an existing deployment.
