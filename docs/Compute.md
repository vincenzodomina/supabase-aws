# Compute choices

## ECS architecture

`EcsCpuArchitecture` selects `X86_64` (default) or `ARM64` for all ECS tasks, including Studio/meta and helper containers. The pinned image indexes contain both variants; no image rebuild or registry is required. Lambda remains x86-64, matching its packaged wheels. RDS instance architecture is an independent choice.

Set `"EcsCpuArchitecture": "ARM64"` in the deployment parameters to evaluate ARM. [AWS requires Linux Fargate platform 1.4.0 or later](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-arm64.html); check support for every supplied Availability Zone. In particular, `use1-az3` does not support Fargate ARM64. Changing architecture registers new task definitions and rolls all services, including administration. Preserve deployment headroom and verify availability before admitting traffic.

Run the existing integration suite with the same package on a Docker engine capable of running both architectures:

```sh
./src/deploy.sh --package /tmp/bootstrap.zip
ECS_CPU_ARCHITECTURE=X86_64 python3 tests/integration.py /tmp/bootstrap.zip
ECS_CPU_ARCHITECTURE=ARM64 python3 tests/integration.py /tmp/bootstrap.zip
```

The selected architecture applies to application and helper containers. Database, Moto, and Lambda fixtures remain x86-64. The suite includes Studio/meta sharing a network namespace, database metadata access, and Studio readiness; it does not validate every administration workflow. Routine CI uses x86-64. Recheck both variants when updating pinned images.

Local compatibility does not establish Fargate capacity or performance. Before choosing ARM in production, compare representative throughput, p95/p99 latency, errors, CPU, memory, and total task cost in the same region with equivalent resource allocations and replicas. Include rollout/surge headroom and optional administration. Use [current regional Fargate pricing](https://aws.amazon.com/fargate/pricing/); lower unit prices alone do not prove lower workload cost. Revert the parameter to x86-64 if acceptance fails; changing architecture does not reverse database migrations.
