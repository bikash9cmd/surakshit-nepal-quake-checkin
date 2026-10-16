#!/usr/bin/env bash
# Creates the two SecureString parameters in BOTH regions, before the first deploy.
#
# The pepper must be byte-for-byte identical in both regions. If it differs, the standby hashes
# every phone number to a different key, and after a failover every lookup says "no check-in"
# for people who are marked safe. (CloudFormation can't create SecureStrings, hence a script.)
set -euo pipefail
PRIMARY=${PRIMARY:-ap-south-1}
STANDBY=${STANDBY:-ap-southeast-1}
PEPPER=$(openssl rand -base64 48)
SMS_TOKEN=${SMS_TOKEN:-$(openssl rand -hex 24)}
for region in "$PRIMARY" "$STANDBY"; do
  aws ssm put-parameter --region "$region" --name /surakshit/phone-pepper --type SecureString \
    --value "$PEPPER" --no-overwrite
  aws ssm put-parameter --region "$region" --name /surakshit/sms-token --type SecureString \
    --value "$SMS_TOKEN" --no-overwrite
done
echo "Parameters created in $PRIMARY and $STANDBY. Give the SMS token to the aggregator for the inbound webhook header."
