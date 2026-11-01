# Supabase on AWS — Project PRD

## Problem statement

Supabase's [official self-hosting approach](https://supabase.com/docs/guides/self-hosting/docker) uses Docker Compose. Organizations embedding Supabase in existing enterprise AWS infrastructure also need compatible RDS database operations and scalable container deployment with service-level controls. Running the Compose setup unchanged does not provide that integration.

This community-maintained project addresses those two core needs: RDS compatibility and ECS orchestration of the essential Supabase services. Supporting integrations such as SES serve those goals. Privacy, compliance, and enterprise infrastructure integration govern the solution; cost and deployment simplicity are optimized within those requirements.

## Core capabilities

| ID | User story |
| --- | --- |
| US-17 | As a platform owner, I want Supabase's database prerequisites, permissions, and migrations adapted to Amazon RDS PostgreSQL as the required database, so that I can rely on AWS-managed database operations and the project takes responsibility for RDS compatibility. |
| US-23 | As an operator, I want the selected Supabase container workloads deployed as independently managed ECS services with scaling, resource controls, health monitoring, and controlled updates, so that I can operate the backend according to AWS cloud deployment practices and adapt it to changing demand. |

## Ownership and enterprise use

| ID | User story |
| --- | --- |
| US-01 | As an enterprise platform owner, I want to operate Supabase using open-source components and AWS services in my organization's AWS account, so that the backend remains under our control without requiring Supabase Cloud or another managed platform vendor. |
| US-02 | As a compliance owner, I want the deployment to support our privacy, security, and compliance obligations, including HIPAA where applicable, through control over data location, access, protection, and retention, so that we can integrate it into our organization's compliance program. |
| US-03 | As an application owner, I want standard PostgreSQL and Supabase interfaces and access to my data, so that adopting this project preserves ownership and portability. |

## Deployment and integration

| ID | User story |
| --- | --- |
| US-04 | As an infrastructure engineer, I want the selected Supabase capabilities delivered as one self-contained CloudFormation stack, so that I can manage the backend as one deployable unit. |
| US-05 | As an operator, I want one command to deploy a ready-to-use instance after shared AWS prerequisites are available, so that provisioning and initialization require no separate manual deployment phases. |
| US-06 | As an infrastructure engineer, I want the stack designed primarily for embedding into existing enterprise infrastructure, using our company VPC and references to shared resources, so that it fits our established network, governance, and operational practices. |
| US-07 | As a release engineer, I want to invoke the same deployment from our existing infrastructure code and CI/CD pipeline, so that adopting the project fits our release process. |
| US-08 | As an infrastructure engineer, I want a minimal set of required parameters with sensible defaults, so that I provide organization-specific choices without configuring every internal component. |
| US-09 | As a maintainer, I want to copy a versioned release of the project's source into a consuming infrastructure repository, so that the organization can review, maintain, and deploy it independently. |
| US-10 | As an operator, I want to review proposed infrastructure changes and receive clear deployment success or failure, so that changes remain controlled and auditable. |
| US-41 | As an evaluator, I want an optional minimal standalone starter that supplies missing shared prerequisites and invokes the same Supabase deployment with one command, so that I can try the approach in a test AWS account before integrating it into existing infrastructure. |

## Enterprise flexibility (design preference)

Apply these goals selectively where they improve enterprise integration without disproportionate parameters, code, or maintenance effort. They guide future design rather than require universal configurability or a creation/reuse option for every resource. Required security protections and existing shared-infrastructure prerequisites remain in force.

| ID | User story |
| --- | --- |
| US-39 | As a platform owner, I want advanced enterprise features with significant security or cost implications to be configurable where useful, so that I can match the deployment to our governance, protection, and budget needs. |
| US-40 | As an infrastructure engineer, I want to reference resources shared across a product's infrastructure and, where appropriate to the stack's scope, have them created when those inputs are omitted, so that I can avoid duplication and unnecessary provisioning work. |

## Supabase capabilities and application access

| ID | User story |
| --- | --- |
| US-11 | As an application developer, I want an essential Supabase backend providing authentication, a database API, and object storage, so that I can build an application with a small operational footprint. |
| US-12 | As an application developer, I want a ready-to-use Supabase URL and anon key, with support for upstream-recommended client credentials, so that I can use standard Supabase clients for the supported capabilities. |
| US-13 | As an application team, I want to deploy our own ECS application alongside Supabase in the same VPC and connect privately, so that backend-to-backend traffic stays within our company network. |
| US-14 | As an operator, I want the stack to establish the private communication its Supabase services need and maintain it through scaling and replacement, so that the services continue working together. |
| US-38 | As a security owner, I want to choose between enforced encryption supplied by our enterprise VPC and TLS between service tasks, so that internal communication is protected using the approach that fits our infrastructure. |
| US-15 | As a security owner, I want private-only access to be supported and any client-facing exposure to be an explicit choice, so that the deployment follows our network access policy. |
| US-16 | As an administrator, I want to enable or disable Studio and its metadata administration capability together, with access restricted to approved private users, so that administration is available only where needed. |

## AWS services and data protection

| ID | User story |
| --- | --- |
| US-18 | As an application developer, I want Supabase Storage to use Amazon S3 by default while preserving Supabase access controls, so that application files remain in our AWS environment. |
| US-19 | As an operator, I want authentication and account emails delivered through Amazon SES by default, so that email uses our organization's AWS email service. |
| US-20 | As a security administrator, I want credentials managed through AWS Secrets Manager and made available only to authorized workloads and operators, so that deployments and applications can connect without exposing privileged secrets. |
| US-21 | As an application owner, I want authentication and data-access permissions to remain effective on RDS, so that private database records and stored objects are accessible only as authorized. |
| US-22 | As a data owner, I want database and object data protected against unintended deletion or replacement and recoverable through backups, so that infrastructure changes and operational failures do not cause avoidable data loss. |

## Operations and maintainability

| ID | User story |
| --- | --- |
| US-24 | As an operator, I want logs, service health, and operational signals available through Amazon CloudWatch, so that I can investigate failures and capacity issues within our AWS tooling. |
| US-25 | As an application owner, I want routine deployments, retries, and service replacements to preserve data, credentials, and valid sessions, so that infrastructure maintenance does not unexpectedly disrupt users. |
| US-26 | As a maintainer, I want a minimal, reusable project with a clear structure, few moving parts, and the fewest distinct technologies and external dependencies necessary to meet its requirements, so that I can understand, review, and maintain it easily. |
| US-34 | As a platform owner, I want to minimize AWS resource usage and cost, especially for costly services, only after privacy, compliance, enterprise infrastructure integration, security, maintainability, and the core RDS and scalable ECS capabilities are satisfied, so that cost savings never weaken these higher-priority requirements. |
| US-35 | As a maintainer, I want additional registries, build or CI/CD pipelines, custom container images, and deployment steps introduced only when their need is justified against requirements and existing capabilities, so that self-hosting requires the least necessary machinery and operational ceremony. |
| US-36 | As a maintainer, I want substantial scripts used by infrastructure templates kept in separate source files wherever practical, so that I can review and test them independently while keeping templates concise and understandable. |

## Upstream compatibility and verification

| ID | User story |
| --- | --- |
| US-27 | As a maintainer, I want reproducible deployments using a coordinated, pinned Supabase release and controlled adoption of current upstream recommendations and security fixes, so that components remain compatible and maintained. |
| US-28 | As a maintainer, I want to trace our adaptations to upstream sources and identify relevant new, changed, or removed upstream behavior, so that subsequent updates can be applied cleanly and reviewed efficiently. |
| US-29 | As an operator, I want database compatibility changes applied safely and repeatably while preserving application data and Supabase-managed schema history, so that both fresh installations and upgrades remain reliable. |
| US-30 | As a release owner, I want upgrade compatibility and recovery requirements understood before rollout, so that changes with irreversible database effects are introduced deliberately. |
| US-31 | As a maintainer, I want automated checks focused on meaningful risks in scripts, service interoperability, credentials, access controls, storage, and AWS deployment limits, so that testing detects important regressions with proportionate effort. |
| US-32 | As a release owner, I want verification results to identify tested versions, distinguish fresh installations from upgrades, and explain the limits of local RDS compatibility checks, so that compatibility claims reflect the available evidence. |
| US-33 | As a maintainer, I want routine automated verification to run locally using existing development capabilities wherever practical, so that feedback is accessible and testing remains lightweight. |
| US-37 | As a maintainer, I want to avoid external dependencies introduced solely for testing unless their risk coverage clearly justifies their dependency, security, and maintenance costs, so that testing adds only necessary complexity. |

## Non-goals

| ID | User story |
| --- | --- |
| NG-01 | As a platform owner, I want Realtime, Edge Functions, Supavisor, analytics services, image transformation, and a separate email-template server outside the initial scope, so that the project stays focused on essential application backend needs. |
| NG-02 | As an application team, I want deployment of our custom application and its business-specific schemas to remain responsibilities of the consuming project, so that this stack stays reusable across applications. |
| NG-03 | As an operator, I want real AWS deployment and acceptance testing performed manually in a separate scratch AWS environment, with automation of those cloud tests outside this project's scope, so that infrastructure validation remains an explicit operational responsibility. |
