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

## RDS workload sizing

`DatabaseInstanceClass` defaults to `db.t4g.medium`; that is a starting configuration, not production capacity evidence. [AWS recommends T classes for development/test and other non-production workloads](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-metrics.html). Evaluate a supported fixed-performance class for sustained enterprise workloads before production acceptance. Preserve Multi-AZ availability, encryption, recovery, and connection headroom when comparing cost.

Collect representative normal load, peaks, background jobs, and deployment/failover overlap. A short idle sample cannot justify downsizing or establish that burst credits replenish. Record the region, engine/version, class, Multi-AZ setting, storage/IOPS/throughput, and service replica/pool limits alongside the observation window.

Use the existing `AWS/RDS` metrics with the exact `DBInstanceIdentifier` dimension:

| Signal | Statistics | Interpretation |
| --- | --- | --- |
| `CPUUtilization` | Average, Maximum | Sustained demand and peaks; correlate with application throughput and p95/p99 latency. |
| `CPUCreditUsage` | Sum | Credits spent during the window, measured in vCPU-minutes. |
| `CPUCreditBalance` | Minimum | Lowest remaining earned-credit balance; a gauge, not a quantity to sum. |
| `CPUSurplusCreditBalance` | Maximum | Borrowed-credit debt that may be repaid; not necessarily a charge. |
| `CPUSurplusCreditsCharged` | Sum | Credits charged during the window; not a currency amount or the complete RDS bill. |
| `FreeableMemory`, `SwapUsage` | Minimum, Maximum respectively | Memory pressure; correlate with connection growth and latency. |
| `DatabaseConnections` | Maximum | Network connections; compare with the database's usable connection budget and deployment overlap. |
| `ReadLatency`, `WriteLatency`, `ReadIOPS`, `WriteIOPS`, `DiskQueueDepth` | Average/Maximum | Storage pressure can limit performance independently of CPU. |

Credit metrics have five-minute resolution. An empty result can mean an unsupported class, wrong region/dimension, or missing observations; never treat it as zero. Read the [RDS metric definitions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-metrics.html) before interpreting balances and charges.

The following Bash example reads an existing deployment and the last complete 24-hour window at five-minute boundaries. It writes local evidence only, using the existing AWS CLI and Python standard library. Run it manually with approved read access; no collection is performed by this project automatically.

```bash
set -euo pipefail
umask 077
export AWS_REGION=us-east-1 AWS_PAGER=''
stack=supabase
evidence=$(mktemp -d)
db=$(aws cloudformation describe-stack-resource --stack-name "$stack" \
  --logical-resource-id Database --query StackResourceDetail.PhysicalResourceId --output text)
aws rds describe-db-instances --db-instance-identifier "$db" \
  --query 'DBInstances[0].{Id:DBInstanceIdentifier,Engine:Engine,Version:EngineVersion,Class:DBInstanceClass,MultiAZ:MultiAZ,StorageType:StorageType,GiB:AllocatedStorage,IOPS:Iops,Throughput:StorageThroughput,Pending:PendingModifiedValues}' \
  --output json > "$evidence/instance.json"
read -r start end < <(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
end = now.replace(minute=now.minute//5*5, second=0, microsecond=0)
print((end-timedelta(days=1)).isoformat(), end.isoformat())
PY
)
printf 'region=%s\nstart=%s\nend=%s\nperiod=300\n' "$AWS_REGION" "$start" "$end" > "$evidence/window.txt"
while read -r metric statistics; do
  read -r -a stats <<< "$statistics"
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name "$metric" \
    --dimensions "Name=DBInstanceIdentifier,Value=$db" --start-time "$start" --end-time "$end" \
    --period 300 --statistics "${stats[@]}" --query 'sort_by(Datapoints,&Timestamp)' \
    --output json > "$evidence/$metric.json"
done <<'METRICS'
CPUUtilization Average Maximum
CPUCreditUsage Sum
CPUCreditBalance Minimum
CPUSurplusCreditBalance Maximum
CPUSurplusCreditsCharged Sum
FreeableMemory Minimum
SwapUsage Maximum
DatabaseConnections Maximum
ReadLatency Average Maximum
WriteLatency Average Maximum
ReadIOPS Average Maximum
WriteIOPS Average Maximum
DiskQueueDepth Average Maximum
METRICS
printf 'Evidence directory: %s\n' "$evidence"
```

Allow for metric publication delay and verify window completeness. Repeat across representative days, including a full sustained-load cycle; sum charged credits across non-overlapping windows only. For larger or older windows, adjust the period to [CloudWatch's retention and 1,440-point request limit](https://docs.aws.amazon.com/cli/latest/reference/cloudwatch/get-metric-statistics.html). Keep operational evidence in the consuming organization's approved location, outside this repository.

Compare candidate classes available for the same engine/version and region, using equivalent data, traffic, storage, availability, and connection headroom. Include observed CPU-credit charges, instance hours, storage/IOPS, backup, and monitoring costs using [current regional RDS pricing](https://aws.amazon.com/rds/postgresql/pricing/) and actual billing; a credit metric alone is not a cost estimate. Record latency/error acceptance, throughput, peak resource use, and recovery behavior before selecting a class.

Apply an approved choice through `DatabaseInstanceClass` and the normal deployment command. Review the source and parameters, scheduling, pending RDS modifications, restart/failover disruption, and resource replacement before deployment; remeasure afterward. Retain existing protection and capacity controls.
