"""The rollout engine against real AWS: one role per customer account, a CloudFormation stack per
account, CodeDeploy for the Lambda traffic shift inside it, and CloudWatch for the gate's counts.

`Rollout.advance` speaks in minutes since an epoch; these adapters translate to wall-clock time. They
are tested against moto (`tests/test_aws.py`); in production they run in a Step Functions task or a
worker that persists `Rollout.snapshot()` between steps."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError, WaiterError

CANARY = {
    "Canary10Percent5Minutes": "CodeDeployDefault.LambdaCanary10Percent5Minutes",
    "Linear10PercentEvery1Minute": "CodeDeployDefault.LambdaLinear10PercentEvery1Minute",
    "AllAtOnce": "CodeDeployDefault.LambdaAllAtOnce",
}


@dataclass
class Accounts:
    """Sessions in customer accounts through a deploy role each account trusts."""

    role_name: str = "devprod-deployer"
    region: str = "eu-central-1"
    external_id: str | None = None
    _cache: dict[str, tuple[boto3.Session, float]] = field(default_factory=dict)

    def session(self, account_id: str) -> boto3.Session:
        hit = self._cache.get(account_id)
        if hit and hit[1] > time.time() + 300:
            return hit[0]
        sts = boto3.client("sts", region_name=self.region)
        kwargs: dict[str, Any] = {
            "RoleArn": f"arn:aws:iam::{account_id}:role/{self.role_name}",
            "RoleSessionName": "devprod-rollout",
            "DurationSeconds": 3600,
        }
        if self.external_id:
            kwargs["ExternalId"] = self.external_id
        c = sts.assume_role(**kwargs)["Credentials"]
        s = boto3.Session(
            aws_access_key_id=c["AccessKeyId"],
            aws_secret_access_key=c["SecretAccessKey"],
            aws_session_token=c["SessionToken"],
            region_name=self.region,
        )
        exp = c["Expiration"].timestamp() if hasattr(c["Expiration"], "timestamp") else time.time() + 3600
        self._cache[account_id] = (s, exp)
        return s


@dataclass
class StackExecutor:
    """Deploys a release as a CloudFormation stack update in each account (an `Executor`)."""

    accounts: Accounts
    stack_name: str
    template: str | Callable[[int], str]  # the template, or where to read a release's template
    epoch: datetime
    account_ids: dict[str, str]  # our account name -> AWS account id
    parameters: dict[str, str] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    previous: dict[str, int] = field(default_factory=dict)

    def _minutes(self) -> float:
        return (datetime.now(UTC) - self.epoch).total_seconds() / 60

    def _apply(self, account: str, release: int) -> str:
        cf = self.accounts.session(self.account_ids[account]).client("cloudformation")
        params = [
            {"ParameterKey": k, "ParameterValue": v} for k, v in {**self.parameters, "Release": str(release)}.items()
        ]
        body = self.template(release) if callable(self.template) else self.template
        try:
            cf.describe_stacks(StackName=self.stack_name)
            exists = True
        except ClientError:
            exists = False
        try:
            if exists:
                cf.update_stack(
                    StackName=self.stack_name,
                    TemplateBody=body,
                    Parameters=params,
                    Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
                )
                waiter = "stack_update_complete"
            else:
                cf.create_stack(
                    StackName=self.stack_name,
                    TemplateBody=body,
                    Parameters=params,
                    Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
                )
                waiter = "stack_create_complete"
        except ClientError as e:
            if "No updates are to be performed" in str(e):
                return "unchanged"
            raise
        try:
            cf.get_waiter(waiter).wait(StackName=self.stack_name, WaiterConfig={"Delay": 5, "MaxAttempts": 180})
        except WaiterError as e:
            raise RuntimeError(f"{account}: stack {self.stack_name} did not settle: {e}") from e
        return "applied"

    def deploy(self, account: str, release: int, at: float) -> float:
        before = self.current_release(account)
        status = self._apply(account, release)
        if before is not None:
            self.previous[account] = before
        self.log.append({"account": account, "release": release, "action": "deploy", "status": status})
        return self._minutes()

    def rollback(self, account: str, release: int, at: float) -> float:
        prev = self.previous.get(account)
        if prev is None:
            raise RuntimeError(f"{account}: no previous release to roll back to")
        self._apply(account, prev)
        self.log.append({"account": account, "release": prev, "action": "rollback", "from": release})
        return self._minutes()

    def current_release(self, account: str) -> int | None:
        cf = self.accounts.session(self.account_ids[account]).client("cloudformation")
        try:
            stack = cf.describe_stacks(StackName=self.stack_name)["Stacks"][0]
        except ClientError:
            return None
        for p in stack.get("Parameters", []):
            if p["ParameterKey"] == "Release":
                return int(p["ParameterValue"])
        return None


@dataclass
class CloudWatchMetrics:
    """Decision and error counts per account from a CloudWatch namespace (a `Metrics`)."""

    accounts: Accounts
    epoch: datetime
    account_ids: dict[str, str]
    namespace: str = "Decisions"
    requests_metric: str = "Requests"
    errors_metric: str = "Errors"

    def _sum(self, account: str, metric: str, t0: float, t1: float) -> int:
        cw = self.accounts.session(self.account_ids[account]).client("cloudwatch")
        start, end = self.epoch + timedelta(minutes=t0), self.epoch + timedelta(minutes=t1)
        period = max(60, int((end - start).total_seconds() // 60 * 60) or 60)
        r = cw.get_metric_statistics(
            Namespace=self.namespace, MetricName=metric, StartTime=start, EndTime=end, Period=period, Statistics=["Sum"]
        )
        return int(sum(dp["Sum"] for dp in r["Datapoints"]))

    def counts(self, accounts: list[str], t0: float, t1: float) -> tuple[int, int]:
        n = sum(self._sum(a, self.requests_metric, t0, t1) for a in accounts)
        e = sum(self._sum(a, self.errors_metric, t0, t1) for a in accounts)
        return n, e


def lambda_shift(
    session: boto3.Session,
    app: str,
    group: str,
    function: str,
    alias: str,
    to_version: str,
    config: str = "Canary10Percent5Minutes",
) -> str:
    """Shifts a Lambda alias to a new version through CodeDeploy, with automatic rollback on the
    deployment group's alarms. Returns the deployment id."""
    lam = session.client("lambda")
    current = lam.get_alias(FunctionName=function, Name=alias)["FunctionVersion"]
    appspec = {
        "version": 0.0,
        "Resources": [
            {
                function: {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Name": function,
                        "Alias": alias,
                        "CurrentVersion": current,
                        "TargetVersion": to_version,
                    },
                }
            }
        ],
    }
    cd = session.client("codedeploy")
    r = cd.create_deployment(
        applicationName=app,
        deploymentGroupName=group,
        deploymentConfigName=CANARY[config],
        revision={"revisionType": "AppSpecContent", "appSpecContent": {"content": json.dumps(appspec)}},
        autoRollbackConfiguration={"enabled": True, "events": ["DEPLOYMENT_FAILURE", "DEPLOYMENT_STOP_ON_ALARM"]},
    )
    return str(r["deploymentId"])
