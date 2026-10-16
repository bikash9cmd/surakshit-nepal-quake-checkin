# Deploying Surakshit

Three stacks, in this order. Needs the AWS SAM CLI and credentials for an account you're happy to put a WAF and two regions in (about $12/month idle, see [docs/COST.md](../docs/COST.md)).

```text
1. infra/waf.yaml        us-east-1        WAF web ACL for CloudFront (CloudFront-scoped ACLs only exist there)
2. template.yaml          ap-south-1       Role=primary: global tables (with the Singapore replica), CloudFront, everything else
3. template.yaml          ap-southeast-1   Role=standby, Active=false: API, queues and workers on the replica
```

## Steps

```bash
# 0. Secrets in both regions (same pepper in both - see the script for why)
scripts/bootstrap_params.sh

# 1. WAF
aws cloudformation deploy --region us-east-1 --stack-name surakshit-waf --template-file infra/waf.yaml
WAF_ARN=$(aws cloudformation describe-stacks --region us-east-1 --stack-name surakshit-waf \
  --query "Stacks[0].Outputs[?OutputKey=='WebAclArn'].OutputValue" --output text)

# 2. Primary (creates the global tables and their Singapore replicas)
make bundle
cd infra && sam build && sam deploy --region ap-south-1 --stack-name surakshit-primary --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides Role=primary Active=true AlertEmail=you@example.com OnCallEmail=pager@example.com \
                        WebAclArn=$WAF_ARN

# 3. Standby (needs the replica's stream ARN)
STREAM=$(STANDBY=ap-southeast-1 ../scripts/standby_stream_arn.sh)
sam deploy --region ap-southeast-1 --stack-name surakshit-standby --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides Role=standby Active=false CheckInsStreamArn=$STREAM \
                        AlertEmail=you@example.com OnCallEmail=pager@example.com

# 4. Tell the primary's CloudFront about the standby API (enables read failover)
STANDBY_API=$(aws cloudformation describe-stacks --region ap-southeast-1 --stack-name surakshit-standby \
  --query "Stacks[0].Outputs[?OutputKey=='ApiDomain'].OutputValue" --output text)
sam deploy --region ap-south-1 --stack-name surakshit-primary --resolve-s3 --capabilities CAPABILITY_IAM \
  --parameter-overrides Role=primary Active=true StandbyApiDomain=$STANDBY_API WebAclArn=$WAF_ARN \
                        AlertEmail=you@example.com OnCallEmail=pager@example.com

# 5. The page
BUCKET=$(aws cloudformation describe-stacks --region ap-south-1 --stack-name surakshit-primary \
  --query "Stacks[0].Outputs[?OutputKey=='SiteBucketName'].OutputValue" --output text)
aws s3 cp ../web/index.html s3://$BUCKET/index.html --content-type "text/html; charset=utf-8" \
  --cache-control "public, max-age=300"
```

Then work through the readiness checklist in [docs/RUNBOOK.md](../docs/RUNBOOK.md): quota increases, the SMS aggregator, a drill.

## Parameters worth knowing

| Parameter | Default | Notes |
|---|---|---|
| `Role` | primary | Which region owns the global tables and CloudFront |
| `Active` | true | Which region runs the activator and counters. Exactly one. |
| `SmsProvider` | dryrun | `aggregator` or `aws` to actually send SMS |
| `SmsBudgetPerEvent` | 500000 | Contact notifications stop after this many SMS per event |
| `WarmWriteUnits` | 20000 | DynamoDB warm throughput for CheckIns |
| `MaxWriteUnits` | 40000 | On-demand ceiling: a cost guardrail |
| `ApiThrottleRate` | 9000 | Keep just under the account's API Gateway quota |

## Tearing down

The tables have `DeletionPolicy: Retain` and deletion protection: deleting the stack leaves the data. To remove everything, turn deletion protection off on both replicas and delete the tables by hand. That's deliberate: nobody should be able to delete an event's check-ins with one `sam delete`.

## Validating without deploying

```bash
make validate-infra   # cfn-lint on both templates (also runs in CI)
```
