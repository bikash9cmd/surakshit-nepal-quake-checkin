# Security and privacy

A check-in service holds, for one day, the location and condition of a large share of a country's population. The security design starts from "what is the worst use of this data?" and works backwards.

## Threat model

| Threat | Example | Mitigation | Residual risk |
|---|---|---|---|
| **Data breach** | A copy of the CheckIns table leaks | No phone numbers stored: the key is HMAC-SHA256 with a pepper in SSM Parameter Store (SecureString). Notes are 140 characters. Items expire after 180 days. Encrypted at rest (DynamoDB, SQS, SNS). | With the pepper as well, an attacker can test any number. The pepper is only readable by three Lambda roles. |
| **Number enumeration** | A script looks up thousands of numbers to find who "needs help" in which district | Lookup reveals status, district and time only, never the note or contacts. WAF: IP reputation list, strict rate limit outside Nepal. | Inside Nepal the limit is loose on purpose (carrier-grade NAT); a determined local attacker can enumerate slowly. |
| **Spoofed check-in (web)** | Someone marks another person "safe" so their family stops looking | Every check-in is an update, not a replacement: the person can correct it, the latest wins, and the SMS channel is authenticated by the network. Contacts are notified, so a false "safe" reaches the family who can call. | **Real and accepted** for v1 (ADR-008). OTP would cost millions of SMS and fail when networks are jammed. The targeted fix: require OTP only to move someone from "need help" to "safe". |
| **Spoofed check-in (SMS)** | Forged webhook calls inject check-ins for any number | Lambda token authorizer on `POST /sms/inbound`; the token lives in SSM; compared in constant time. | A leaked token. Rotate in SSM; the authorizer's 5-minute cache bounds the window. |
| **SMS spam / cost attack** | Check-ins listing 3 contacts each, from a script | Contacts capped at 3; own number and duplicates dropped; per-person update cap (20 per event); per-event SMS budget with an alarm. | Up to the budget, spam SMS can be sent to arbitrary Nepali numbers. The budget is the bound. |
| **PII in logs** | Lookup URLs (`/status?phone=98...`) in access logs | API Gateway access logs record `routeKey`, not path or query string. WAF sampled requests off. CloudFront standard logs off. Lambda logs mask numbers (`+977 98XXXXX567`). | CloudFront real-time metrics only; if logs are ever needed, a field list without the query string. |
| **Denial of service** | Flood the API | CloudFront + AWS Shield Standard; WAF; API stage throttle set just under the account quota so one route can't starve the account; the queue protects the table. | A large, distributed flood from inside Nepal looks like the real surge. |
| **Insider / operator** | Someone with console access browses check-ins | Least-privilege roles per function; no human role needs table read in normal operation; CloudTrail data events can be enabled for the table during events. | An administrator can always read the data; the HMAC means they see hashes, not numbers. |

## Data minimisation, field by field

| Field | Stored? | Why |
|---|---|---|
| Phone number | **No**, HMAC only | Needed only to find the record again |
| Contacts' numbers | **No**, only inside the queued SMS job (4-day retention) | Needed only to send the SMS |
| Status | Yes | The point of the service |
| District | Yes, optional | Responders need where; the person decides whether to give it |
| Note | Yes, 140 chars, never shown by lookup | Goes to the contacts the person chose |
| Request time, request id, channel | Yes | Ordering, idempotency, debugging |
| IP address, user agent, location | **No** | Not needed, and the most sensitive |

## IAM

Each Lambda has its own role, generated from SAM policy templates scoped to one table or queue:

| Function | Can | Can't |
|---|---|---|
| Writer | Read/write CheckIns, read Meta, send to the notify queue, read the pepper | Read the SMS token, publish to SNS |
| Read API | Read CheckIns and Meta, read the pepper | Write anything |
| Aggregator | Read the CheckIns stream, write Meta | Read CheckIns items directly, read the pepper |
| Notifier | Write Meta (SMS counter), read the SMS token | Read CheckIns, read the pepper |
| Activator | Write Meta, publish to the two SNS topics | Read CheckIns |
| SMS authorizer | Read the SMS token | Anything else |

API Gateway's role can `sqs:SendMessage` to the check-in queue and nothing else.

## Legal

Nepal's Individual Privacy Act, 2075 (2018) applies to personal data processed about people in Nepal. A real deployment needs a data controller in Nepal (a partner organisation, see ARCHITECTURE.md "Going live"), an agreed retention period, and a published privacy notice in Nepali. The 180-day TTL and the lookup's limited fields are my defaults, not legal advice.
