"""The GitHub webhook as a Lambda Function URL. It verifies the signature before anything else, records
the delivery id once (DynamoDB, conditional put with a TTL), and routes: `workflow_job` events to the
runner scaler's queue, failed `workflow_run` events to the triage queue. Nothing slow happens here, so
GitHub always gets its answer within its ten-second limit."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .app import signature_ok

_secret: str | None = None


def secret() -> str:
    global _secret
    if _secret is None:
        _secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["WEBHOOK_SECRET_ARN"])[
            "SecretString"
        ]
    return _secret


def _resp(code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def first_time(delivery: str, ttl_days: int = 3) -> bool:
    table = boto3.resource("dynamodb").Table(os.environ["DELIVERIES_TABLE"])
    try:
        table.put_item(
            Item={"id": delivery, "expires": int(time.time()) + ttl_days * 86400},
            ConditionExpression="attribute_not_exists(id)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    h = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not signature_ok(secret(), body, h.get("x-hub-signature-256")):
        return _resp(401, {"error": "bad signature"})
    delivery, kind = h.get("x-github-delivery"), h.get("x-github-event", "")
    if not delivery:
        return _resp(400, {"error": "missing X-GitHub-Delivery"})
    if not first_time(delivery):
        return _resp(200, {"duplicate": True})
    try:
        return route(kind, body)
    except Exception:
        boto3.resource("dynamodb").Table(os.environ["DELIVERIES_TABLE"]).delete_item(Key={"id": delivery})
        raise  # 5xx: GitHub's redelivery will try again


def route(kind: str, body: bytes) -> dict[str, Any]:
    payload = json.loads(body)
    sqs = boto3.client("sqs")
    if kind == "workflow_job":
        sqs.send_message(QueueUrl=os.environ["RUNNER_QUEUE_URL"], MessageBody=body.decode())
        return _resp(202, {"queued": "runners"})
    if (
        kind == "workflow_run"
        and payload.get("action") == "completed"
        and payload.get("workflow_run", {}).get("conclusion") == "failure"
    ):
        sqs.send_message(
            QueueUrl=os.environ["TRIAGE_QUEUE_URL"],
            MessageBody=json.dumps(
                {
                    "run_id": payload["workflow_run"]["id"],
                    "repo": payload["repository"]["full_name"],
                    "prs": [p["number"] for p in payload["workflow_run"].get("pull_requests", [])],
                }
            ),
        )
        return _resp(202, {"queued": "triage"})
    return _resp(200, {"ignored": f"{kind}/{payload.get('action')}"})
