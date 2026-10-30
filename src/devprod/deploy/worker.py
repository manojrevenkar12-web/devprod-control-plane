"""Runs rollouts in production: every minute, load each active rollout's snapshot from DynamoDB,
`advance` it, and save it back with a conditional write on its version, so two workers never advance
the same rollout twice and a crashed worker loses nothing (the next tick resumes from the snapshot)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .engine import Account, Executor, Metrics, Rollout


def _plain(x: Any) -> Any:
    return json.loads(json.dumps(x, default=lambda d: float(d) if isinstance(d, Decimal) else str(d)))


@dataclass
class RolloutTable:
    name: str
    region: str = "eu-central-1"

    def __post_init__(self) -> None:
        self.t = boto3.resource("dynamodb", region_name=self.region).Table(self.name)

    def put_new(self, ro: Rollout, wake_at: float, accounts: list[Account]) -> None:
        self.t.put_item(
            Item={
                "release": ro.release,
                "version": 0,
                "state": ro.state,
                "wake_at": Decimal(str(wake_at)),
                "snapshot": json.dumps(ro.snapshot()),
                "accounts": json.dumps([a.__dict__ for a in accounts]),
            },
            ConditionExpression="attribute_not_exists(#r)",
            ExpressionAttributeNames={"#r": "release"},
        )

    def due(self, now: float) -> list[dict[str, Any]]:
        items = self.t.scan()["Items"]  # a handful of active rollouts; a GSI on state when there are more
        return [i for i in items if i["state"] in ("pending", "rolling") and float(i["wake_at"]) <= now]

    def save(self, item: dict[str, Any], ro: Rollout, wake_at: float | None) -> bool:
        try:
            self.t.put_item(
                Item={
                    "release": ro.release,
                    "version": int(item["version"]) + 1,
                    "state": ro.state,
                    "wake_at": Decimal(str(wake_at if wake_at is not None else -1)),
                    "snapshot": json.dumps(ro.snapshot()),
                    "accounts": item["accounts"],
                },
                ConditionExpression="#v = :v",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":v": item["version"]},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False  # another worker advanced it first
            raise


def tick(table: RolloutTable, now: float, ex: Executor, metrics: Metrics, epoch: datetime) -> list[dict[str, Any]]:
    out = []
    for item in table.due(now):
        ro = Rollout.from_snapshot(_plain(json.loads(item["snapshot"])))
        accounts = {a["account_id"]: Account(**a) for a in json.loads(item["accounts"])}
        nxt = ro.advance(now, accounts, ex, metrics, epoch)
        saved = table.save(item, ro, nxt)
        out.append({"release": ro.release, "state": ro.state, "wave": ro.wave, "next": nxt, "saved": saved})
    return out
