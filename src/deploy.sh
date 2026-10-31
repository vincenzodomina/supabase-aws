#!/usr/bin/env bash
#
# ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
# ┃                     Supabase Deployment Script                     ┃
# ┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
# ┃  One CloudFormation stack; no custom container builds.             ┃
# ┃  Default: create a change set. Use --apply to execute.              ┃
# ┃  --change-set ARN --apply executes an existing reviewed plan.       ┃
# ┃                                                                    ┃
# ┃  1. Parse Options and Check Required Tools                          ┃
# ┃  2. Load Parameters and Embed the Gateway Script                    ┃
# ┃  3. Inspect the Existing Stack                                     ┃
# ┃  4. Package and Upload Versioned Lambda and Certificate Artifacts   ┃
# ┃  5. Protect the Existing Database                                  ┃
# ┃  6. Deploy the Stack and Protect a Newly Created Database           ┃
# ┃                                                                    ┃
# ┃  CloudFormation runs database initialization before services.      ┃
# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
#
########## 1. Parse Options and Check Required Tools ##########

set -euo pipefail
project_dir=$(cd "$(dirname "$0")/.." && pwd)

usage() {
  echo "Usage: $0 --stack-name NAME --region REGION --parameters FILE --artifacts-bucket BUCKET [--profile PROFILE] [--role-arn ARN] [--apply]"
  echo "       $0 --stack-name NAME --region REGION --change-set ARN --apply [--profile PROFILE]"
  echo "       $0 --template > rendered.yaml"
  echo "       $0 --package bootstrap.zip"
}

render_template() (
  cd "$project_dir"
  awk '
    /^ *- "@include src\/gateway-entrypoint.sh"$/ {
      sub(/-.*/, "")
      print $0 "- |"
      while ((result = getline line < "src/gateway-entrypoint.sh") > 0) print $0 "  " line
      if (result < 0) exit 1
      close("src/gateway-entrypoint.sh")
      included++
      next
    }
    { print }
    END { if (included != 1) exit 1 }
  ' src/supabase.yaml
)

package_lambda() (
  destination=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
  package_dir=$(mktemp -d)
  trap 'rm -rf "$package_dir"' EXIT
  # Download Lambda-compatible wheels on either macOS or Linux; never build native code locally.
  python3 -m pip install --disable-pip-version-check --no-compile --require-hashes --only-binary=:all: \
    --platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
    --implementation cp --python-version 3.13 --target "$package_dir/files" -r "$project_dir/src/requirements.txt"
  cp "$project_dir"/src/{bootstrap.py,rds-ca.pem} "$package_dir/files/"
  cp -R "$project_dir/src/migrations" "$package_dir/files/"
  cd "$package_dir/files"
  # Stable metadata makes unchanged inputs reuse the same content-addressed S3 object.
  find . -type d -exec chmod 755 {} +
  find . -type f -exec chmod 644 {} +
  TZ=UTC find . -exec touch -t 198001010000 {} +
  find . -type f | LC_ALL=C sort | zip -X -q "$package_dir/bootstrap.zip" -@
  mv "$package_dir/bootstrap.zip" "$destination"
)

if [[ $# -eq 1 && "$1" == --template ]]; then
  render_template
  exit 0
fi

if [[ $# -eq 2 && "$1" == --package ]]; then
  package_lambda "$2"
  exit 0
fi

apply=false
stack_name='' stack_id='' region='' parameters='' artifacts_bucket='' role_arn='' change_set=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) stack_name="${2:?Missing stack name}"; shift 2 ;;
    --region) region="${2:?Missing region}"; shift 2 ;;
    --parameters) parameters="${2:?Missing parameter file}"; shift 2 ;;
    --artifacts-bucket) artifacts_bucket="${2:?Missing artifact bucket}"; shift 2 ;;
    --profile) export AWS_PROFILE="${2:?Missing profile}"; shift 2 ;;
    --role-arn) role_arn="${2:?Missing CloudFormation role ARN}"; shift 2 ;;
    --change-set) change_set="${2:?Missing change-set ARN}"; shift 2 ;;
    --apply) apply=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done
[[ -n "$stack_name" && -n "$region" ]] || { usage >&2; exit 1; }
required_tools=(aws jq)
if [[ -n "$change_set" ]]; then
  if ! "$apply" || [[ -n "$parameters" || -n "$artifacts_bucket" || -n "$role_arn" ]]; then
    echo '--change-set requires --apply and cannot be combined with parameters, artifacts, or a role override.' >&2
    exit 1
  fi
  [[ "$change_set" =~ ^arn:[^:]+:cloudformation:[^:]+:[0-9]{12}:changeSet/[^/]+/[^/]+$ ]] || {
    echo 'Use the full reviewed change-set ARN, not a reusable name.' >&2; exit 1;
  }
else
  [[ -n "$parameters" && -n "$artifacts_bucket" ]] || { usage >&2; exit 1; }
  required_tools+=(openssl python3 zip)
fi
[[ "$stack_name" =~ ^[a-z][a-z0-9-]{0,39}$ ]] || { echo 'Use a lowercase stack name of at most 40 characters.' >&2; exit 1; }
export AWS_REGION="$region" AWS_DEFAULT_REGION="$region" AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off
for tool in "${required_tools[@]}"; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done

########## 2. Load Parameters and Embed the Gateway Script ##########

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
if [[ -z "$change_set" ]]; then
  if ! jq -e 'type == "object" and (has("ArtifactsBucket") | not) and (has("BootstrapKey") | not) and (has("CertificateKey") | not)
    and all(.[]; (type == "string" or type == "number" or type == "boolean") and (tostring | test("[\\r\\n]") | not))' "$parameters" >/dev/null; then
    echo 'Parameters must be a JSON object of single-line values. ArtifactsBucket, BootstrapKey, and CertificateKey are managed by deploy.sh.' >&2
    exit 1
  fi
  overrides=()
  while IFS= read -r value; do overrides+=("$value"); done < <(jq -r 'to_entries[] | "\(.key)=\(.value)"' "$parameters")
  cd "$project_dir"
  render_template > "$scratch/supabase.yaml"
fi

########## 3. Inspect the Existing Stack ##########

if stack=$(aws cloudformation describe-stacks --stack-name "$stack_name" --output json 2>"$scratch/error"); then
  status=$(jq -r '.Stacks[0].StackStatus' <<<"$stack")
elif [[ $(<"$scratch/error") == *'does not exist'* ]]; then
  status=NEW
else
  cat "$scratch/error" >&2
  exit 1
fi
if [[ -n "$change_set" ]]; then
  [[ "$status" != NEW ]] || { echo 'The reviewed stack no longer exists.' >&2; exit 1; }
  stack_id=$(jq -er '.Stacks[0].StackId' <<<"$stack")
  reviewed=$(aws cloudformation describe-change-set --change-set-name "$change_set" --stack-name "$stack_id" --output json)
  if ! jq -e --arg stack "$stack_id" --arg arn "$change_set" \
    '.StackId == $stack and .ChangeSetId == $arn and .Status == "CREATE_COMPLETE" and .ExecutionStatus == "AVAILABLE"' <<<"$reviewed" >/dev/null; then
    echo 'The change set must belong to this stack and be available for execution.' >&2
    exit 1
  fi
fi

########## 4. Package and Upload Versioned Lambda and Certificate Artifacts ##########

if [[ -z "$change_set" ]]; then
  package_lambda "$scratch/bootstrap.zip"
  bootstrap_key="supabase-aws/bootstrap/$(openssl dgst -sha256 -r "$scratch/bootstrap.zip" | cut -d' ' -f1).zip"
  certificate_key="supabase-aws/certificates/$(openssl dgst -sha256 -r src/rds-ca.pem | cut -d' ' -f1).pem"

  upload_artifact() {
    if aws s3api put-object --bucket "$artifacts_bucket" --key "$2" --body "$1" \
      --server-side-encryption AES256 --if-none-match '*' > /dev/null 2>"$scratch/error"; then
      echo "Published artifact: $2"
    elif [[ $(<"$scratch/error") == *PreconditionFailed* ]]; then
      echo "Reusing artifact: $2"
    else
      cat "$scratch/error" >&2
      return 1
    fi
  }
  upload_artifact "$scratch/bootstrap.zip" "$bootstrap_key"
  upload_artifact src/rds-ca.pem "$certificate_key"
  overrides+=("ArtifactsBucket=$artifacts_bucket" "BootstrapKey=$bootstrap_key" "CertificateKey=$certificate_key")
fi

########## 5. Protect the Existing Database ##########

protect_database() {
  local policy
  # Preserve any additional restrictions while blocking destructive database updates.
  policy=$(aws cloudformation get-stack-policy --stack-name "${stack_id:-$stack_name}" --output json)
  policy=$(jq -c '(.StackPolicyBody | if . then fromjson else
    {Statement: [{Effect:"Allow", Action:"Update:*", Principal:"*", Resource:"*"}]} end)
    | .Statement += [{Effect:"Deny", Action:["Update:Replace","Update:Delete"], Principal:"*", Resource:"LogicalResourceId/Database"}]
    | .Statement |= unique' <<<"$policy")
  aws cloudformation set-stack-policy --stack-name "${stack_id:-$stack_name}" --stack-policy-body "$policy"
}
new_stack=false
if [[ "$status" == NEW || "$status" == REVIEW_IN_PROGRESS ]]; then new_stack=true; fi
if "$apply" && ! "$new_stack"; then protect_database; fi

########## 6. Deploy the Stack and Protect a Newly Created Database ##########

if [[ -n "$change_set" ]]; then
  aws cloudformation execute-change-set --change-set-name "$change_set" --stack-name "$stack_id"
  waiter='stack-update-complete'
  if "$new_stack"; then waiter='stack-create-complete'; fi
  aws cloudformation wait "$waiter" --stack-name "$stack_id"
else
  options=(--no-fail-on-empty-changeset)
  if [[ -n "$role_arn" ]]; then options+=(--role-arn "$role_arn"); fi
  if ! "$apply"; then options+=(--no-execute-changeset); fi
  aws cloudformation deploy --stack-name "$stack_name" --template-file "$scratch/supabase.yaml" \
    --s3-bucket "$artifacts_bucket" --s3-prefix "supabase-aws/$stack_name" \
    --capabilities CAPABILITY_IAM --parameter-overrides "${overrides[@]}" "${options[@]}"
fi
if "$apply"; then
  if "$new_stack"; then protect_database; fi
  echo 'Stack deployment complete. Run the README acceptance checks before serving users.'
fi
