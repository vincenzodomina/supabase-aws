# Credential rotation

`CredentialRevision` is a non-secret change identifier. Changing it reruns the existing bootstrap, then rolls every enabled ECS service in dependency order. Bootstrap applies the three service-role passwords and rebuilds their connection URIs; it preserves a complete API key bundle. The revision does not generate new passwords, reset the RDS master password, or revoke user sessions.

[ECS reads injected secrets at task launch](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html). The stack uses the native [deployment nonce](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-ecs-service-forcenewdeployment.html) to replace tasks without changing images. [Changing a secret alone does not update an RDS dynamic reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references-secretsmanager.html).

## Prepare and review

Install this release with unchanged credentials first. Rotate one credential class at a time in a planned maintenance window; pause application traffic, background writers, and concurrent deployments/secret writers. Single-password transitions and mixed task generations can interrupt reconnects. Use the same reviewed source/artifacts, with no pending migrations or unrelated infrastructure changes.

Run from the repository root in a protected administrator environment. Use the existing Python environment with `src/requirements.txt` installed for key generation and PostgreSQL verification. Database checks need an approved private network path; do not open public access. The operator needs the relevant secret read/write, deployment, and (for master rotation) RDS modification permissions.

Start a dedicated Bash session. Keep secret files private, outside the checkout and shared logs; do not enable shell tracing or AWS debug logging. Retain recovery copies in the organization's approved secret store until acceptance completes.

```bash
set -euo pipefail
set +x
umask 077
export AWS_REGION=us-east-1 AWS_PAGER=''
stack=supabase
parameters=/secure/config/parameters.json
artifacts_bucket=existing-artifacts-bucket
rotation_dir=$(mktemp -d)
resource() {
  aws cloudformation describe-stack-resource --stack-name "$stack" \
    --logical-resource-id "$1" --query StackResourceDetail.PhysicalResourceId --output text
}
db=$(resource Database)
aws rds describe-db-instances --db-instance-identifier "$db" > "$rotation_dir/database.json"
jq --arg revision "$(date -u +%Y%m%dT%H%M%SZ)" '.CredentialRevision=$revision' \
  "$parameters" > "$rotation_dir/parameters.json"
./src/deploy.sh --template > "$rotation_dir/template.yaml"
```

Review the rendered template and parameter changes before publishing credentials; keep the checkout, artifacts, and parameter file unchanged through deployment. Rendering does not calculate AWS resource changes. A credential refresh should update bootstrap and enabled ECS services without replacing the database. Secret contents are fetched at execution/task launch; freeze other secret writers throughout the operation. Supply the existing profile/execution role as required by the deployment.

Select one secret below, then save its current complete JSON object:

| Rotation | Set `secret` to | Result |
| --- | --- | --- |
| RDS master | `$(resource DatabaseSecret)` | Update RDS first, then the secret; bootstrap and optional Studio/meta need the new password. |
| Service role | `$(resource AuthSecret)`, `$(resource RestSecret)`, or `$(resource StorageSecret)` | Publish the new password; bootstrap applies it and repairs the URI before tasks roll. |
| SES SMTP | Existing `SesSmtpSecretArn` | Publish credentials for the configured SES region; refresh tasks, then retire the old SMTP credentials. |
| API keys/signing | `$(resource ApiSecret)` | Choose opaque keys only or a deliberate complete signing-bundle cutover. |

```bash
secret=$(resource AuthSecret)  # Select the intended row before continuing.
secret_region=$(printf '%s' "$secret" | cut -d: -f4)
aws secretsmanager get-secret-value --region "$secret_region" --secret-id "$secret" \
  --query SecretString --output text > "$rotation_dir/previous.json"
```

## Database passwords

For either master or service-role rotation, generate a new password while preserving the other fields:

```bash
aws secretsmanager get-random-password --password-length 48 --exclude-punctuation \
  > "$rotation_dir/random.json"
jq --slurpfile random "$rotation_dir/random.json" '.password=$random[0].RandomPassword' \
  "$rotation_dir/previous.json" > "$rotation_dir/next.json"
```

For a **service role**, continue to publish and refresh below. Leave its username unchanged; bootstrap overwrites the old URI. Verify a new connection using the new password after refresh, and confirm the old password is rejected. Password changes do not terminate existing database sessions.

For the **master password**, first inspect `PendingModifiedValues` in the captured RDS configuration. Resolve unrelated pending changes before proceeding: `ApplyImmediately` can apply those too. The following explicit RDS operation is necessary because changing the secret/revision does not change the template's master-password reference:

```bash
jq --arg db "$db" '{DBInstanceIdentifier:$db, MasterUserPassword:.password, ApplyImmediately:true}' \
  "$rotation_dir/next.json" > "$rotation_dir/rds-update.json"
aws rds modify-db-instance --cli-input-json "file://$rotation_dir/rds-update.json" > /dev/null
aws rds wait db-instance-available --db-instance-identifier "$db"
```

Confirm the pending password change has cleared and authenticate with the new password before publishing it. Instance availability alone does not establish password propagation. This check also applies to service-role passwords after refresh:

```bash
.venv/bin/python - "$rotation_dir" <<'PY'
import json, sys
from pathlib import Path
import psycopg
root = Path(sys.argv[1])
db = json.loads((root/'database.json').read_text())['DBInstances'][0]
credential = json.loads((root/'next.json').read_text())
with psycopg.connect(host=db['Endpoint']['Address'], port=db['Endpoint']['Port'],
                     dbname='postgres', user=credential['username'], password=credential['password'],
                     sslmode='verify-full', sslrootcert='src/rds-ca.pem', connect_timeout=15) as connection:
    connection.execute('SELECT 1')
print('New database credential accepted with verified TLS.')
PY
```

Keep RDS and `DatabaseSecret` synchronized for future stack updates. Do not enable independent automatic rotation without coordinating these consumers and the bootstrap connection.

## SES SMTP

Obtain a second valid credential pair through the organization's existing SES/IAM process, retaining the old pair until all consumers migrate. [SMTP credentials are region-specific](https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html); the SMTP password is not the raw IAM secret access key. Prepare `next.json` as a complete JSON object containing `username` and `password`, preserving any other fields used by shared consumers. Publish and refresh, then verify real invitation/recovery mail through Auth before retiring the old access key. Do not retire a shared key while other applications still use it.

## API keys and signing material

[Self-hosted Supabase distinguishes opaque API keys from JWT signing keys](https://supabase.com/docs/guides/self-hosting/self-hosted-auth-keys). Use `opaque` for routine publishable/secret API-key rotation: signing material, legacy keys, and user access tokens remain valid. The gateway accepts one opaque key per role, so coordinate application configuration changes with the rollout.

Use `signing` only for a planned replacement of the entire bundle, including the symmetric secret, EC key pair, legacy/asymmetric role JWTs, and opaque keys. This invalidates old access tokens after every verifier rolls and external verifier caches refresh; affected clients must refresh or authenticate again. Existing refresh sessions remain usable unless separately revoked. Follow the incident-response policy for session revocation and compromised keys. Stored data is not re-encrypted by this operation; test existing Storage signed URLs and regenerate affected links.

Generate a consistent bundle through the existing generator, never by editing only `JWT_SECRET` or a single JWK field:

```bash
rotation_kind=opaque  # Use signing only for the deliberate complete cutover described above.
.venv/bin/python - "$rotation_dir" "$rotation_kind" <<'PY'
import json, secrets, sys
from pathlib import Path
sys.path.insert(0, 'src')
from bootstrap import generate_keys
root, kind = Path(sys.argv[1]), sys.argv[2]
assert kind in ('opaque', 'signing')
previous = json.loads((root/'previous.json').read_text())
generated = generate_keys(secrets.token_urlsafe(36) if kind == 'signing' else previous['JWT_SECRET'])
names = generated.keys() if kind == 'signing' else ['SUPABASE_PUBLISHABLE_KEY', 'SUPABASE_SECRET_KEY']
previous.update({name: generated[name] for name in names})
(root/'next.json').write_text(json.dumps(previous))
PY
```

Keep the complete bundle privileged: `JWT_JWKS` also contains the legacy symmetric key. Distribute only public/anon credentials to browsers. `MetaSecret` contains encryption material rather than a login password; changing it requires separate upstream data-recovery analysis.

## Publish, refresh, and verify

After the selected preparation and any master-password verification, publish the secret and deploy using the same source and parameter file:

```bash
aws secretsmanager put-secret-value --region "$secret_region" --secret-id "$secret" \
  --secret-string "file://$rotation_dir/next.json" --query VersionId --output text
./src/deploy.sh --stack-name "$stack" --region "$AWS_REGION" \
  --parameters "$rotation_dir/parameters.json" --artifacts-bucket "$artifacts_bucket"
```

Wait for successful stack completion and confirm every enabled service has completed its new deployment, old tasks have drained, and replica minimums are restored. Verify affected database logins, Auth login/refresh, private RLS access, an S3 upload/download/delete, and optional Studio/meta access. For opaque rotation, check that old opaque keys fail while existing sessions and legacy keys still work. For signing rotation, check old access tokens fail and new tokens work. Verify SMTP delivery separately. These AWS checks remain manual acceptance; local Docker/Moto tests cannot establish RDS propagation, ECS rollout ordering, or SES delivery.

Update consuming applications before reopening traffic. Persist the revision in the canonical parameter file. Once verification and the recovery-retention period finish, remove temporary secret files through the organization's approved process; unlinking files alone is not guaranteed secure erasure.

## Recovery

CloudFormation rollback does not undo secret writes or a committed database password change. The bootstrap reads current secret values during rollback too; resetting the revision alone does not restore old credentials.

| Failure | Recovery |
| --- | --- |
| Master changed, secret publication failed | Keep traffic paused. Publish the new matching master secret, or explicitly restore RDS to the saved old password before restoring its secret. Verify authentication, then refresh. |
| Service-role bootstrap or task rollout failed | Inspect stack events and sanitized bootstrap logs. Retry with the intended complete secret and a fresh revision, or republish the saved full object and refresh to reapply the old password/URI. |
| SMTP rollout or delivery failed | Keep the old SMTP credential active. Restore the previous secret and refresh; verify delivery before retiring either key. |
| API-key cutover failed | Restore the complete previous bundle and refresh only if those keys remain trusted. Update clients/verifier caches consistently; tokens minted with the rejected new key will fail. Prefer forward recovery for compromised keys. |

Use the same file-based `put-secret-value` command with `previous.json` to restore a trusted object. Keep recovery files until resolved; never patch only the URI or signing subset. Wait for stack rollback to settle (resolve `UPDATE_ROLLBACK_FAILED` first), review the source and parameters with a fresh revision, redeploy, and repeat verification before restoring traffic.
