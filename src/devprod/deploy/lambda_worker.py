"""Lambda: the rollout worker's minute tick (infra/control-plane/template.yaml)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from .aws import Accounts, CloudWatchMetrics, StackExecutor
from .worker import RolloutTable, tick

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)  # rollout times are minutes since this instant


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = (datetime.now(UTC) - EPOCH).total_seconds() / 60
    table = RolloutTable(os.environ["ROLLOUTS_TABLE"], os.environ.get("AWS_REGION", "eu-central-1"))
    accounts = Accounts(
        os.environ["DEPLOY_ROLE_NAME"],
        os.environ.get("AWS_REGION", "eu-central-1"),
        os.environ.get("DEPLOY_EXTERNAL_ID"),
    )
    ids = {i["account_id"]: i["account_id"] for item in table.due(now) for i in json.loads(item["accounts"])}
    ex = StackExecutor(
        accounts, os.environ.get("STACK_NAME", "decisions-runtime"), os.environ.get("TEMPLATE_BODY", ""), EPOCH, ids
    )
    metrics = CloudWatchMetrics(accounts, EPOCH, ids)
    return {"advanced": tick(table, now, ex, metrics, EPOCH)}
