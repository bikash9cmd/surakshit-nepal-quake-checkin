"""Lambda authorizer for POST /sms/inbound: the aggregator sends a shared token in a header.

API Gateway caches the answer for 5 minutes per token value, so this runs a handful of times
per event, not once per SMS. Without it, anyone who found the URL could inject check-ins for
any phone number, which is the one thing the SMS door must not allow.
"""

from __future__ import annotations

import hmac
import os

import boto3

_token: dict[str, str] = {}


def handler(event, context=None):
    if "v" not in _token:
        ssm = boto3.client("ssm")
        _token["v"] = ssm.get_parameter(Name=os.environ["SMS_TOKEN_PARAM"], WithDecryption=True)["Parameter"]["Value"]
    given = (event.get("headers") or {}).get("x-surakshit-token", "")
    return {"isAuthorized": bool(given) and hmac.compare_digest(given, _token["v"])}
