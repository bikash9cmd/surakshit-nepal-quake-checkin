#!/usr/bin/env bash
# Prints the stream ARN of the standby replica of surakshit-checkins (a stack parameter for the
# standby deploy). Each replica of a global table has its own stream in its own region.
set -euo pipefail
aws dynamodb describe-table --region "${STANDBY:-ap-southeast-1}" --table-name surakshit-checkins \
  --query Table.LatestStreamArn --output text
