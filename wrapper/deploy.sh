#!/usr/bin/env bash
# Standalone demo: 1. Check inputs  2. Create foundation  3. Prepare SMTP  4. Deploy Supabase

set -euo pipefail
umask 077
wrapper_dir=$(cd "$(dirname "$0")" && pwd)

usage() {
  echo "Usage: $0 --domain supabase.example.com --hosted-zone-id ZONE [--name supabase-demo] [--region us-east-1] [--profile PROFILE]"
  echo 'Deploys paid test resources and a public HTTPS API. Requires an already delegated Route 53 zone.'
}

########## 1. Check Inputs ##########

name=supabase-demo region=us-east-1 domain='' zone_id=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) domain="${2:?Missing API domain}"; shift 2 ;;
    --hosted-zone-id) zone_id="${2:?Missing hosted zone ID}"; shift 2 ;;
    --name) name="${2:?Missing name}"; shift 2 ;;
    --region) region="${2:?Missing region}"; shift 2 ;;
    --profile) export AWS_PROFILE="${2:?Missing profile}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n "$domain" && -n "$zone_id" ]] || { usage >&2; exit 1; }
[[ "$name" =~ ^[a-z][a-z0-9-]{0,28}$ ]] || { echo 'Use a lowercase name of at most 29 characters.' >&2; exit 1; }
[[ "$domain" =~ ^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$ ]] || { echo 'Use a lowercase DNS hostname.' >&2; exit 1; }
[[ "$zone_id" =~ ^Z[A-Z0-9]+$ ]] || { echo 'Use a Route 53 zone ID, without /hostedzone/.' >&2; exit 1; }
export AWS_REGION="$region" AWS_DEFAULT_REGION="$region" AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off
for tool in aws jq openssl python3 zip; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done
python3 -m pip --version >/dev/null
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
zone=$(aws route53 get-hosted-zone --id "$zone_id" --output json)
zone_name=$(jq -er '.HostedZone.Name | rtrimstr(".")' <<<"$zone")
if [[ $(jq -r '.HostedZone.Config.PrivateZone' <<<"$zone") != false || "$domain" != *."$zone_name" ]]; then
  echo 'Choose an unused subdomain inside the supplied public hosted zone.' >&2
  exit 1
fi
account=$(aws sts get-caller-identity --query Account --output text)
echo "Deploying $name in account $account / $region. This creates paid resources and a public HTTPS API."

########## 2. Create the Foundation ##########

aws cloudformation deploy --stack-name "$name-foundation" --template-file "$wrapper_dir/foundation.yaml" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "ApiDomainName=$domain" "HostedZoneId=$zone_id"
aws cloudformation describe-stacks --stack-name "$name-foundation" --output json \
  | jq -e '.Stacks[0].Outputs | map({key: .OutputKey, value: .OutputValue}) | from_entries' > "$scratch/foundation.json"

########## 3. Prepare Regional SES SMTP Credentials ##########

smtp_secret=$(jq -er '.SesSmtpSecretArn' "$scratch/foundation.json")
aws secretsmanager get-secret-value --secret-id "$smtp_secret" --query SecretString --output text > "$scratch/smtp.json"
if jq -e 'has("secretAccessKey")' "$scratch/smtp.json" >/dev/null; then
  python3 "$wrapper_dir/smtp-password.py" "$region" < "$scratch/smtp.json" > "$scratch/converted.json"
  aws secretsmanager put-secret-value --secret-id "$smtp_secret" --secret-string "file://$scratch/converted.json" >/dev/null
  mv "$scratch/converted.json" "$scratch/smtp.json"
fi
jq -e '.username | type == "string" and length > 0' "$scratch/smtp.json" >/dev/null
jq -e '.password | type == "string" and length > 0' "$scratch/smtp.json" >/dev/null
smtp_user=$(jq -r '.username' "$scratch/smtp.json")
rm "$scratch/smtp.json"
if ! aws ses wait identity-exists --identities "$domain"; then
  echo 'SES verification is still pending or unreadable. Check DNS and permissions, then rerun this command; the foundation remains deployed.' >&2
  exit 1
fi

########## 4. Deploy the Existing Supabase Stack ##########

jq --arg domain "$domain" --arg zone "$zone_id" --arg region "$region" --arg revision "$smtp_user" '
  {
    VpcId, PublicSubnetIds, PrivateSubnetIds, CertificateArn, ServiceConnectPrivateCaArn, SesSmtpSecretArn,
    DatabaseSubnetIds: .PrivateSubnetIds,
    ApiDomainName: $domain, HostedZoneId: $zone, SiteUrl: ("https://" + $domain),
    ApiExposure: "internet-facing", InternalTrafficEncryption: "service-connect-tls",
    SesRegion: $region, SesSenderEmail: ("noreply@" + $domain), CredentialRevision: $revision,
    DatabaseMultiAZ: "false", DatabaseBackupRetentionDays: 1,
    GatewayMinTasks: 1, GatewayMaxTasks: 1, AuthMinTasks: 1, AuthMaxTasks: 1,
    RestMinTasks: 1, RestMaxTasks: 1, StorageMinTasks: 1, StorageMaxTasks: 1,
    ContainerInsights: "disabled", LogRetentionDays: 7, ServiceLogRetentionDays: 7
  }' "$scratch/foundation.json" > "$scratch/parameters.json"
"$wrapper_dir/../src/deploy.sh" --stack-name "$name-supabase" --region "$region" \
  --parameters "$scratch/parameters.json" --artifacts-bucket "$(jq -er '.ArtifactsBucket' "$scratch/foundation.json")"
aws cloudformation describe-stacks --stack-name "$name-supabase" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl' || OutputKey=='ApiSecretArn']" --output table
echo 'Demo deployed. See docs/Standalone-Wrapper.md for keys, SES sandbox limits, and cleanup.'
