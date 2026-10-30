"""The AWS adapters and the production workers against moto (no AWS account, no network)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from devprod import settings
from devprod.deploy.aws import Accounts, CloudWatchMetrics, StackExecutor
from devprod.deploy.engine import Account, Rollout, plan
from devprod.deploy.worker import RolloutTable, tick

REGION = "eu-central-1"
TEMPLATE = """AWSTemplateFormatVersion: "2010-09-09"
Parameters:
  Release:
    Type: String
Resources:
  Marker:
    Type: AWS::SSM::Parameter
    Properties:
      Name: /decisions/release
      Type: String
      Value: !Ref Release
"""


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield


def epoch() -> datetime:
    return datetime.now(UTC) - timedelta(hours=2)


def test_assume_role_session_is_cached():
    a = Accounts(region=REGION, external_id="x" * 16)
    s1 = a.session("111111111111")
    assert a.session("111111111111") is s1
    assert s1.client("sts").get_caller_identity()["Account"]


def test_stack_executor_deploys_and_rolls_back_to_the_previous_release():
    ex = StackExecutor(Accounts(region=REGION), "decisions-runtime", TEMPLATE, epoch(), {"cust-1": "222222222222"})
    ex.deploy("cust-1", 1, 0)
    assert ex.current_release("cust-1") == 1
    ex.deploy("cust-1", 2, 0)
    assert ex.current_release("cust-1") == 2
    ex.rollback("cust-1", 2, 0)
    assert ex.current_release("cust-1") == 1
    assert [x["action"] for x in ex.log] == ["deploy", "deploy", "rollback"]


def test_stack_executor_reads_each_releases_template():
    seen = []
    ex = StackExecutor(
        Accounts(region=REGION),
        "decisions-runtime",
        lambda r: seen.append(r) or TEMPLATE,
        epoch(),
        {"c": "333333333333"},
    )
    ex.deploy("c", 7, 0)
    assert seen == [7]


def test_cannot_roll_back_without_a_previous_release():
    ex = StackExecutor(Accounts(region=REGION), "s", TEMPLATE, epoch(), {"c": "444444444444"})
    with pytest.raises(RuntimeError):
        ex.rollback("c", 1, 0)


def test_cloudwatch_counts_feed_the_gate():
    e = epoch()
    accounts = Accounts(region=REGION)
    cw = accounts.session("555555555555").client("cloudwatch")
    for minute in range(10, 40):
        ts = e + timedelta(minutes=minute)
        cw.put_metric_data(
            Namespace="Decisions",
            MetricData=[
                {"MetricName": "Requests", "Timestamp": ts, "Value": 100},
                {"MetricName": "Errors", "Timestamp": ts, "Value": 1 if minute < 25 else 9},
            ],
        )
    m = CloudWatchMetrics(accounts, e, {"c": "555555555555"})
    n_before, e_before = m.counts(["c"], 10, 25)
    n_after, e_after = m.counts(["c"], 25, 40)
    assert n_before == n_after == 1500 and e_before == 15 and e_after == 135


def make_table(name="rollouts"):
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "release", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "release", "AttributeType": "N"}],
    )
    return RolloutTable(name, REGION)


class Fake:
    def __init__(self) -> None:
        self.deploys: list[tuple[str, int]] = []

    def deploy(self, account, release, at):
        self.deploys.append((account, release))
        return at + 1

    def rollback(self, account, release, at):
        return at + 1

    def counts(self, ids, t0, t1):
        return (10_000, 20)


def test_worker_resumes_rollouts_from_dynamodb_and_never_advances_twice():
    table = make_table()
    accs = [
        Account("internal-dev", "internal", "UTC", "any", 30),
        Account("internal-staging", "internal", "UTC", "any", 30),
    ]
    accs += [Account(f"c{k}", ["starter", "growth", "enterprise"][k % 3], "UTC", "any", 10) for k in range(12)]
    cfg = settings.load("deploy")["control-plane"]
    ro = Rollout(5, plan(accs, cfg["waves"]), cfg["gate"], False)
    table.put_new(ro, 0.0, accs)
    with pytest.raises(Exception):  # noqa: B017 - a second rollout of the same release is refused
        table.put_new(ro, 0.0, accs)
    fake = Fake()
    t, states = 0.0, []
    for _ in range(200):
        out = tick(table, t, fake, fake, epoch())
        states += [o["state"] for o in out]
        if out and out[0]["state"] == "completed":
            break
        t += 5
    assert states[-1] == "completed"
    assert sorted(a for a, _ in fake.deploys) == sorted(a.account_id for a in accs)
    stale = table.t.get_item(Key={"release": 5})["Item"]
    stale["version"] = 0  # a worker holding an old copy
    assert table.save(stale, ro, None) is False


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_lambda_verifies_dedupes_and_routes(monkeypatch):
    from devprod.service import lambda_webhook as lw

    sm = boto3.client("secretsmanager", region_name=REGION)
    arn = sm.create_secret(Name="wh", SecretString="s3cret")["ARN"]
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="deliveries",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
    )
    sqs = boto3.client("sqs", region_name=REGION)
    runners = sqs.create_queue(QueueName="runners")["QueueUrl"]
    triage = sqs.create_queue(QueueName="triage")["QueueUrl"]
    for k, v in {
        "WEBHOOK_SECRET_ARN": arn,
        "DELIVERIES_TABLE": "deliveries",
        "RUNNER_QUEUE_URL": runners,
        "TRIAGE_QUEUE_URL": triage,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lw, "_secret", None)

    def call(event_name, payload, delivery, secret="s3cret", b64=False):
        body = json.dumps(payload).encode()
        return lw.handler(
            {
                "body": base64.b64encode(body).decode() if b64 else body.decode(),
                "isBase64Encoded": b64,
                "headers": {
                    "X-Hub-Signature-256": sign(secret, body),
                    "X-GitHub-Event": event_name,
                    "X-GitHub-Delivery": delivery,
                },
            },
            None,
        )

    assert call("workflow_job", {"action": "queued"}, "d1", secret="wrong")["statusCode"] == 401
    assert call("workflow_job", {"action": "queued", "workflow_job": {"status": "queued"}}, "d1")["statusCode"] == 202
    assert json.loads(call("workflow_job", {"action": "queued"}, "d1")["body"]) == {"duplicate": True}
    run = {
        "action": "completed",
        "workflow_run": {"id": 9, "conclusion": "failure", "pull_requests": [{"number": 4}]},
        "repository": {"full_name": "org/mono"},
    }
    assert call("workflow_run", run, "d2", b64=True)["statusCode"] == 202
    msg = json.loads(sqs.receive_message(QueueUrl=triage)["Messages"][0]["Body"])
    assert msg == {"run_id": 9, "repo": "org/mono", "prs": [4]}
    assert len(sqs.receive_message(QueueUrl=runners, MaxNumberOfMessages=10)["Messages"]) == 1
    assert json.loads(call("push", {"ref": "main"}, "d3")["body"])["ignored"] == "push/None"


def test_rollout_worker_lambda_deploys_each_releases_template(monkeypatch):
    from devprod.deploy import lambda_worker

    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="releases", CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_object(Bucket="releases", Key="releases/3/template.yaml", Body=TEMPLATE.encode())
    table = make_table("rollouts-live")
    accs = [Account("777777777777", "growth", "UTC", "any", 10)]
    waves = [{"name": "all", "share": 1.0, "bake_minutes": 0, "account_ids": ["777777777777"]}]
    table.put_new(Rollout(3, waves, None, False), 0.0, accs)
    for k, v in {
        "ROLLOUTS_TABLE": "rollouts-live",
        "DEPLOY_ROLE_NAME": "devprod-deployer",
        "RELEASES_BUCKET": "releases",
        "STACK_NAME": "decisions-runtime",
    }.items():
        monkeypatch.setenv(k, v)
    out = lambda_worker.handler({}, None)["advanced"]
    assert out and out[0]["release"] == 3
    ex = StackExecutor(Accounts(region=REGION), "decisions-runtime", TEMPLATE, epoch(), {"a": "777777777777"})
    assert ex.current_release("a") == 3


def test_runner_scaler_lambda_sets_desired_capacity_from_queued_jobs(monkeypatch):
    import importlib
    import sys

    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    subnet = ec2.describe_subnets()["Subnets"][0]["SubnetId"]
    asg = boto3.client("autoscaling", region_name=REGION)
    asg.create_launch_configuration(LaunchConfigurationName="lc", ImageId=ami, InstanceType="t4g.small")
    asg.create_auto_scaling_group(
        AutoScalingGroupName="devprod-runners-python-medium",
        LaunchConfigurationName="lc",
        MinSize=0,
        MaxSize=64,
        DesiredCapacity=0,
        VPCZoneIdentifier=subnet,
    )
    labels = ["self-hosted", "linux", "arm64", "ci-python", "medium"]
    monkeypatch.setenv("SCALER_CONFIG", json.dumps(settings.load("ci")["fleet"]))
    monkeypatch.setenv("POOLS", json.dumps({"devprod-runners-python-medium": labels}))
    monkeypatch.syspath_prepend(str(settings.ROOT / "infra" / "runners"))
    sys.modules.pop("scaler_handler", None)
    sh = importlib.import_module("scaler_handler")
    monkeypatch.setattr(sh, "working_hours", lambda now: True)
    records = [
        {"body": json.dumps({"action": "queued", "workflow_job": {"status": "queued", "labels": labels}})}
        for _ in range(9)
    ]
    out = sh.handler({"Records": records}, None)["pools"][0]
    assert out["desired"] == 9 and out["launch"] == 9
    g = asg.describe_auto_scaling_groups(AutoScalingGroupNames=["devprod-runners-python-medium"])
    assert g["AutoScalingGroups"][0]["DesiredCapacity"] == 9
