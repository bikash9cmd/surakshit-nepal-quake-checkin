"""The Lambda handlers against mocked AWS (moto): real boto3 calls, real DynamoDB conditional
writes and stream-shaped records, no account needed."""

import json

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from surakshit.aws import handlers

T0 = 1_429_942_285_950  # 2015-04-25 06:11:25.950 UTC


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.client("dynamodb")
        for name in ("CheckIns", "Meta"):
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                        {"AttributeName": "sk", "KeyType": "RANGE"}],
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                                   {"AttributeName": "sk", "AttributeType": "S"}])
        boto3.client("ssm").put_parameter(Name="/surakshit/pepper", Value="test-pepper", Type="SecureString")
        url = boto3.client("sqs").create_queue(QueueName="notify")["QueueUrl"]
        sns = boto3.client("sns")
        page = sns.create_topic(Name="page")["TopicArn"]
        email = sns.create_topic(Name="email")["TopicArn"]
        for k, v in {"CHECKINS_TABLE": "CheckIns", "META_TABLE": "Meta", "PEPPER_PARAM": "/surakshit/pepper",
                     "NOTIFY_QUEUE_URL": url, "PAGE_TOPIC_ARN": page, "EMAIL_TOPIC_ARN": email,
                     "SMS_PROVIDER": "dryrun", "SMS_BUDGET_PER_EVENT": "3"}.items():
            monkeypatch.setenv(k, v)
        handlers._cache.clear()
        yield {"notify_url": url}
        handlers._cache.clear()


def sqs_record(body: str, mid: str, source: str = "web", at: int = T0 + 60_000) -> dict:
    return {"messageId": mid, "body": body, "attributes": {"SentTimestamp": str(at + 5)},
            "messageAttributes": {"source": {"stringValue": source, "dataType": "String"},
                                  "requestId": {"stringValue": f"req-{mid}", "dataType": "String"},
                                  "requestTime": {"stringValue": str(at), "dataType": "String"}}}


def gorkha_feed() -> dict:
    return {"features": [{"id": "us20002926", "geometry": {"coordinates": [84.7314, 28.2305, 8.22]},
                          "properties": {"mag": 7.8, "time": T0, "place": "67 km NNE of Bharatpur, Nepal",
                                         "type": "earthquake", "ids": ",us20002926,"}}]}


def stream_records(store_items: list[tuple[dict | None, dict | None]]) -> dict:
    ser = TypeSerializer()

    def img(d):
        return {k: ser.serialize(v) for k, v in d.items()} if d else None

    recs = []
    for old, new in store_items:
        ddb = {}
        if old:
            ddb["OldImage"] = img(old)
        if new:
            ddb["NewImage"] = img(new)
        recs.append({"eventName": "MODIFY" if old else "INSERT", "dynamodb": ddb})
    return {"Records": recs}


def test_end_to_end_activation_writer_aggregator_api(aws):
    out = handlers.activator_handler({"feed": gorkha_feed()})
    assert out["opened"] == ["2015-04-25T0611Z-us20002926"]
    assert handlers.activator_handler({"feed": gorkha_feed()})["opened"] == []  # idempotent

    records = [
        sqs_record(json.dumps({"phone": "9841234567", "status": "need_help", "district": "Gorkha",
                               "contacts": ["9851111111"]}), "m1"),
        sqs_record(json.dumps({"from": "+9779861234567", "text": "THIK kathmandu", "id": "sms-1"}), "m2", "sms"),
        sqs_record("{broken", "m3"),
    ]
    resp = handlers.writer_handler({"Records": records})
    assert resp == {"batchItemFailures": []}
    # Redelivery of m1: no failure, no second write.
    assert handlers.writer_handler({"Records": records[:1]}) == {"batchItemFailures": []}

    notify = boto3.client("sqs").receive_message(QueueUrl=aws["notify_url"], MaxNumberOfMessages=10)["Messages"]
    kinds = sorted(json.loads(m["Body"])["kind"] for m in notify)
    assert kinds == ["notify", "notify", "reply"]  # the duplicate re-sends the notify, by design

    # The stream, as DynamoDB would deliver it.
    store = handlers._store()
    items = [i for i in boto3.resource("dynamodb").Table("CheckIns").scan()["Items"]]
    assert len(items) == 2 and all("phone" not in i for i in items)
    handlers.aggregator_handler(stream_records([(None, dict(i)) for i in items]))
    s = store.summary("2015-04-25T0611Z-us20002926")
    assert s["Gorkha"]["need_help"] == 1 and s["Kathmandu"]["safe"] == 1

    r = handlers.api_handler({"routeKey": "GET /status", "queryStringParameters": {"phone": "९८४१२३४५६७"}})
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["status"] == "need_help" and "note" not in body
    assert r["headers"]["cache-control"] == "public, max-age=30"
    r = handlers.api_handler({"routeKey": "GET /summary/{event_id}",
                              "pathParameters": {"event_id": "2015-04-25T0611Z-us20002926"}})
    assert json.loads(r["body"])["totals"] == {"safe": 1, "need_help": 1, "injured": 0}
    assert handlers.api_handler({"routeKey": "GET /status", "queryStringParameters": {"phone": "1"}})[
        "statusCode"] == 400


def test_out_of_order_and_cap_on_real_conditional_writes(aws):
    handlers.activator_handler({"feed": gorkha_feed()})
    later = sqs_record(json.dumps({"phone": "9841234567", "status": "injured"}), "late", at=T0 + 900_000)
    earlier = sqs_record(json.dumps({"phone": "9841234567", "status": "safe", "district": "Gorkha"}), "early",
                         at=T0 + 100_000)
    handlers.writer_handler({"Records": [later, earlier]})
    item = boto3.resource("dynamodb").Table("CheckIns").scan()["Items"][0]
    assert item["status"] == "injured" and int(item["updates"]) == 1


def test_ttl_deletes_do_not_decrement_counters(aws):
    old = {"pk": "k", "sk": "e1", "status": "safe", "district": "Gorkha"}
    handlers.aggregator_handler(stream_records([(None, old)]))
    ttl = stream_records([(old, None)])
    ttl["Records"][0].update(eventName="REMOVE", userIdentity={"type": "Service",
                                                               "principalId": "dynamodb.amazonaws.com"})
    handlers.aggregator_handler(ttl)
    assert handlers._store().summary("e1")["Gorkha"]["safe"] == 1


def test_notifier_budget_breaker_stops_notifications_but_not_replies(aws):
    sent = []

    class Recorder:
        def send(self, to, text):
            sent.append(text)

    handlers._cache["sms"] = Recorder()

    def rec(i, kind):
        return {"messageId": f"n{i}", "body": json.dumps({"to": "+9779851111111", "text": f"{kind}-{i}",
                                                          "kind": kind, "event_id": "e1"})}

    handlers.notifier_handler({"Records": [rec(i, "notify") for i in range(3)]})
    # SMS_BUDGET_PER_EVENT is 3 in the fixture: the next notification is skipped, the reply still goes.
    handlers.notifier_handler({"Records": [rec(10, "notify"), rec(11, "reply")]})
    assert sent == ["notify-0", "notify-1", "notify-2", "reply-11"]


def test_writer_returns_throttled_messages_for_retry(aws, monkeypatch):
    from botocore.exceptions import ClientError

    def throttled(self, item):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
                          "UpdateItem")

    monkeypatch.setattr(handlers.DynamoStore, "put_checkin", throttled)
    resp = handlers.writer_handler({"Records": [sqs_record(json.dumps({"phone": "9841234567", "status": "safe"}),
                                                           "t1")]})
    assert resp == {"batchItemFailures": [{"itemIdentifier": "t1"}]}
