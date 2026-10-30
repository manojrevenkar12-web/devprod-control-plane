"""Mints a just-in-time runner config on the instance: GitHub App JWT -> installation token ->
POST /orgs/{org}/actions/runners/generate-jitconfig. Prints the encoded config for run.sh --jitconfig.
The App key never leaves Secrets Manager except into this process."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

import boto3
import jwt

API = "https://api.github.com"


def call(method: str, path: str, token: str, body: dict | None = None, kind: str = "Bearer") -> dict:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"{kind} {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main() -> None:
    p = argparse.ArgumentParser()
    for a in ("--secret", "--org", "--name", "--labels", "--region"):
        p.add_argument(a, required=True)
    args = p.parse_args()
    s = json.loads(boto3.client("secretsmanager", region_name=args.region)
                   .get_secret_value(SecretId=args.secret)["SecretString"])
    now = int(time.time())
    app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(s["app_id"])}, s["private_key"], "RS256")
    inst = call("GET", f"/orgs/{args.org}/installation", app_jwt)
    token = call("POST", f"/app/installations/{inst['id']}/access_tokens", app_jwt)["token"]
    cfg = call("POST", f"/orgs/{args.org}/actions/runners/generate-jitconfig", token,
               {"name": args.name, "runner_group_id": 1, "labels": args.labels.split(","), "work_folder": "_work"})
    print(cfg["encoded_jit_config"])


if __name__ == "__main__":
    main()
