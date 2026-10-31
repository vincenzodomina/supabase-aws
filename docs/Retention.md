# Retention

Choose retention under the organization's audit, recovery, and deletion policy. Shortening a period can permanently remove existing history; a CloudFormation rollback cannot recover expired data. Review a change set before applying a retention change.

## Service logs

`LogRetentionDays` controls the application/Lambda log group. `ServiceLogRetentionDays` separately controls both RDS export groups (`postgresql`, `upgrade`) and the cluster's Container Insights `performance` group. Its default, `0`, leaves service-log policies unchanged. A positive value sets their retention in days, including for existing groups.

The stack creates missing RDS groups and, when Insights is enabled, its performance group. When Insights is disabled, an existing performance group still receives the selected retention, but an absent group is not created. Events, encryption settings, and other group configuration are preserved when adopting existing groups. Only retention is changed.

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
