# Retention

Choose retention under the organization's audit, recovery, and deletion policy. Shortening a period can permanently remove existing history; a CloudFormation rollback cannot recover expired data. Review a change set before applying a retention change.

## Service logs

`LogRetentionDays` controls the application/Lambda log group. `ServiceLogRetentionDays` separately controls both RDS export groups (`postgresql`, `upgrade`) and the cluster's Container Insights `performance` group. Its default, `0`, leaves service-log policies unchanged. A positive value sets their retention in days, including for existing groups.

The stack creates missing RDS groups and, when Insights is enabled, its performance group. When Insights is disabled, an existing performance group still receives the selected retention, but an absent group is not created. Existing groups are updated in place, preserving their encryption settings and other configuration. Events older than the selected retention become eligible for expiry.

Setting the parameter back to `0`, or deleting the stack, leaves groups and their last retention policies in place. Zero stops management; it does not restore indefinite retention. To keep future events indefinitely after disabling management, an authorized administrator can run:

```sh
aws logs delete-retention-policy --log-group-name "$LOG_GROUP" --region "$AWS_REGION"
```

Verify each exact group in CloudWatch after deployment, or use:

```sh
aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$AWS_REGION" \
  --query 'logGroups[].{Name:logGroupName,Days:retentionInDays}'
```

Names use the actual RDS instance identifier and ECS cluster name from the stack's Resources tab. Omitted `retentionInDays` means no expiry. Review existing external retention automation before enabling stack management. The Lambda's private subnets must reach the CloudWatch Logs API through the supplied network's NAT or Logs endpoint.

Retention is reconciled on creation and relevant stack updates, not continuously. Deleting a managed group outside CloudFormation can therefore remove its policy until another relevant update; restoring a shorter policy later can expire accumulated history. A failed operation can leave some groups updated; inspect all three before retrying or disabling management. After retaining resources on stack deletion, their existing expiry policies continue to apply.

AWS documents [RDS export groups](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_LogAccess.Concepts.PostgreSQL.html), [Insights groups](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-reference-performance-logs-ECS.html), and [retention expiry](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html). Expiry is asynchronous, typically taking up to 72 hours after eligibility and occasionally longer; verify deletion when required by policy. Real delivery, permissions, and expiry behavior require manual AWS acceptance.

## Storage history

S3 versioning preserves bytes after ordinary overwrites and deletes. A Supabase Storage API deletion removes the application's access and metadata, but S3 can retain older versions behind a delete marker. API deletion is not permanent erasure; the manual acceptance script also leaves version history subject to this policy.

`StorageNoncurrentVersionRetentionDays=0` keeps the existing indefinite-history default. A positive value adds noncurrent-version expiry across the storage bucket. Days are counted from when a version becomes noncurrent, not from the deployment date. Existing older versions can become eligible immediately. Current objects are not expired, delete markers remain, and the seven-day incomplete-upload cleanup is independent.

Expiration permanently removes recoverable bytes. Use a duration compatible with the RDS/S3 recovery window and approved audit/erasure policy. Returning to zero removes the expiry rule but cannot restore deleted versions or guarantee cancellation of already scheduled deletion. S3 lifecycle processing is asynchronous and is not a timed erasure guarantee. [AWS version-deletion semantics](https://docs.aws.amazon.com/AmazonS3/latest/userguide/DeletingObjectVersions.html).

### Administrator purge

Use this only for an approved, precisely scoped erasure. The application task has no `s3:DeleteObjectVersion` permission. An administrator needs `s3:ListBucketVersions` on the bucket and `s3:DeleteObjectVersion` on the intended keys.

1. Stop writes and replacements for the affected objects. Identify all physical S3 keys, including historical Supabase object-version keys; do not assume an API path equals a single S3 key. Check retention obligations and any externally applied Object Lock or legal hold.
2. Remove the objects through the Supabase Storage API so application metadata and access are reconciled. Do not delete rows directly from the storage schema. Keep writers stopped through verification.
3. Review all S3 data versions and delete markers for each exact key using the console's **Show versions**, or the following read-only command. A prefix can also match sibling keys; select only the intended exact key. Keep CLI pagination enabled.

```sh
aws s3api list-object-versions --bucket "$BUCKET" --prefix "$OBJECT_KEY" --region "$AWS_REGION" --output json
```

4. Permanently delete each approved data version by its exact key and version ID, including a `null` version if present. Keep delete markers until all data versions for that key are gone, preventing an older version from becoming visible again. Then remove its markers using their version IDs.

```sh
aws s3api delete-object --bucket "$BUCKET" --key "$OBJECT_KEY" --version-id "$VERSION_ID" --region "$AWS_REGION"
```

5. Repeat the version listing and confirm no versions or markers remain for each exact key and the application object is unavailable. Record verification before resuming writers. A concurrent write can invalidate an earlier listing; deletion is not an atomic operation across versions.

S3 erasure does not erase RDS snapshots, replicas, backups, exported logs, or other copies. Handle those under their own retention policies. A retained bucket keeps its lifecycle rules after stack deletion; resource retention does not suspend data expiry. [AWS CLI version listing](https://docs.aws.amazon.com/cli/latest/reference/s3api/list-object-versions.html) and [version deletion](https://docs.aws.amazon.com/cli/latest/reference/s3api/delete-object.html).
