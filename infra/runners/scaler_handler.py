"""Lambda: scales the ephemeral runner pools. Triggered by SQS (workflow_job webhooks, verified at the
Function URL by devprod.service) and by a one-minute schedule. The decision is devprod.ci.scaler.decide;
this file only reads GitHub and the Auto Scaling group and applies it."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import boto3

from devprod.ci.scaler import Pool, decide

ASG = boto3.client("autoscaling")
CFG = json.loads(os.environ["SCALER_CONFIG"])  # fleet section of config/ci.yaml, rendered by Terraform
POOLS = json.loads(os.environ["POOLS"])  # {"<asg name>": ["self-hosted", "linux", "arm64", "ci-python", "medium"]}
TZ = ZoneInfo(os.environ.get("WORK_TZ", "Europe/Berlin"))


def working_hours(now: datetime) -> bool:
    local = now.astimezone(TZ)
    return local.weekday() < 5 and 8 <= local.hour < 19


def queued_by_labels(records: list[dict]) -> dict[tuple[str, ...], int]:
    out: dict[tuple[str, ...], int] = {}
    for r in records:
        job = json.loads(r["body"]).get("workflow_job", {})
        if job.get("status") == "queued":
            key = tuple(sorted(job.get("labels", [])))
            out[key] = out.get(key, 0) + 1
    return out


def handler(event: dict, context: object) -> dict:
    queued = queued_by_labels(event.get("Records", []))
    now = datetime.now(UTC)
    out = []
    for asg_name, labels in POOLS.items():
        g = ASG.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"][0]
        tags = {i["InstanceId"]: i for i in g["Instances"]}
        busy = sum(1 for i in tags.values() if i.get("LifecycleState") == "InService" and i.get("ProtectedFromScaleIn"))
        idle = sum(1 for i in tags.values() if i.get("LifecycleState") == "InService" and not i.get("ProtectedFromScaleIn"))
        booting = sum(1 for i in tags.values() if i.get("LifecycleState", "").startswith("Pending"))
        d = decide(Pool(tuple(labels), queued.get(tuple(sorted(labels)), 0), busy, idle, booting), CFG,
                   working_hours(now))
        if d["desired"] != g["DesiredCapacity"]:
            ASG.set_desired_capacity(AutoScalingGroupName=asg_name, DesiredCapacity=d["desired"], HonorCooldown=False)
        out.append({"asg": asg_name, **d})
    return {"pools": out}
