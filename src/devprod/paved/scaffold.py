"""The paved road for a new service: `devprod new <name>` writes a Lambda module that builds, tests,
deploys and is owned from its first commit.

A Python service gets a handler with structured logs and a request id, a unit test and a sample event,
a SAM template (arm64, an alias, CodeDeploy canary with an alarm that rolls it back), its generated CI
workflow, and a CODEOWNERS line. A TypeScript package gets a vitest setup. Nothing is overwritten."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..ci import workflows

NAME = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")


class ScaffoldError(ValueError):
    pass


def render(
    name: str, language: str, deps: list[str], modules: dict[str, dict[str, Any]], owner: str = "platform"
) -> dict[str, str]:
    if not NAME.match(name):
        raise ScaffoldError(f"{name!r}: use 3-40 lowercase letters, digits and dashes, starting with a letter")
    if name in modules:
        raise ScaffoldError(f"{name} already exists at {modules[name]['path']}")
    missing = [d for d in deps if d not in modules]
    if missing:
        raise ScaffoldError(f"unknown dependencies: {', '.join(missing)}")
    if language not in ("python", "typescript"):
        raise ScaffoldError("language is python or typescript")
    base = f"services/{name}" if language == "python" else f"frontend/{name}"
    pkg = name.replace("-", "_")
    new = {**modules, name: {"name": name, "language": language, "deps": deps, "owner": owner, "path": base}}
    files: dict[str, str] = {}
    if language == "python":
        files[f"{base}/pyproject.toml"] = f'''[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [{", ".join(f'"{d}"' for d in deps)}]

[tool.uv.sources]
{chr(10).join(f"{d} = {{ workspace = true }}" for d in deps)}

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["uv_build>=0.8"]
build-backend = "uv_build"
'''
        files[f"{base}/src/{pkg}/__init__.py"] = ""
        files[
            f"{base}/src/{pkg}/handler.py"
        ] = f'''"""{name}: an AWS Lambda handler. Logs are one JSON object per line with the request id."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("{name}")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = getattr(context, "aws_request_id", "local")
    body = json.loads(event.get("body") or "{{}}")
    log.info(json.dumps({{"msg": "request", "request_id": request_id, "keys": sorted(body)}}))
    if "name" not in body:
        return {{"statusCode": 400, "body": json.dumps({{"error": "name is required"}})}}
    return {{"statusCode": 200, "body": json.dumps({{"hello": body["name"], "service": "{name}"}})}}
'''
        files[f"{base}/tests/test_handler.py"] = f'''import json
from pathlib import Path

from {pkg}.handler import handler

EVENT = json.loads((Path(__file__).parents[1] / "events" / "sample.json").read_text())


class Context:
    aws_request_id = "test"


def test_greets() -> None:
    out = handler(EVENT, Context())
    assert out["statusCode"] == 200
    assert json.loads(out["body"])["service"] == "{name}"


def test_rejects_missing_name() -> None:
    assert handler({{"body": "{{}}"}}, Context())["statusCode"] == 400
'''
        files[f"{base}/events/sample.json"] = (
            json.dumps(
                {"body": json.dumps({"name": "taktile"}), "requestContext": {"http": {"method": "POST"}}}, indent=2
            )
            + "\n"
        )
        files[f"{base}/template.yaml"] = sam_template(name, pkg)
    else:
        files[f"{base}/package.json"] = (
            json.dumps(
                {
                    "name": f"@org/{name}",
                    "private": True,
                    "type": "module",
                    "scripts": {"test": "vitest run", "build": "tsc -p ."},
                    "dependencies": {f"@org/{d}": "workspace:*" for d in deps},
                    "devDependencies": {"typescript": "^5.9.0", "vitest": "^3.2.0"},
                },
                indent=2,
            )
            + "\n"
        )
        files[f"{base}/tsconfig.json"] = (
            json.dumps(
                {
                    "compilerOptions": {
                        "target": "ES2023",
                        "module": "ESNext",
                        "moduleResolution": "Bundler",
                        "strict": True,
                        "outDir": "dist",
                        "declaration": True,
                    },
                    "include": ["src"],
                },
                indent=2,
            )
            + "\n"
        )
        files[f"{base}/src/index.ts"] = (
            f'export const name = "{name}";\n\nexport function greet(who: string): string {{\n  return `hello ${{who}} from ${{name}}`;\n}}\n'
        )
        files[f"{base}/src/index.test.ts"] = (
            'import { describe, expect, it } from "vitest";\nimport { greet } from "./index";\n\n'
            'describe("greet", () => {\n  it("names the package", () => {\n'
            f'    expect(greet("you")).toContain("{name}");\n  }});\n}});\n'
        )
    files[f"{base}/README.md"] = (
        f"# {name}\n\nOwned by @org/{owner}. Created with `devprod new`.\n\n"
        f"- Test: `devprod dev test {name}`\n"
        + (
            f"- Run locally: `devprod dev invoke {name} {base}/events/sample.json`\n"
            f"- Deploy to your dev account: `sam deploy --config-env dev` (from {base})\n"
            if language == "python"
            else ""
        )
    )
    files[f".github/workflows/{name}.yml"] = workflows.workflow_for(name, new)
    files[".github/CODEOWNERS"] = workflows.codeowners(new)
    return files


def sam_template(name: str, pkg: str) -> str:
    fn = "".join(p.capitalize() for p in name.split("-"))
    return f"""AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Description: {name} (created by devprod new)

Parameters:
  Stage:
    Type: String
    AllowedValues: [dev, staging, prod]
    Default: dev

Globals:
  Function:
    Runtime: python3.13
    Architectures: [arm64]
    MemorySize: 512
    Timeout: 10
    Tracing: Active
    Environment:
      Variables:
        LOG_LEVEL: INFO
        STAGE: !Ref Stage

Resources:
  {fn}Function:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
      Handler: {pkg}.handler.handler
      AutoPublishAlias: live
      DeploymentPreference:
        Type: Canary10Percent5Minutes
        Alarms:
          - !Ref {fn}ErrorsAlarm
      FunctionUrlConfig:
        AuthType: AWS_IAM

  {fn}ErrorsAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmDescription: Errors on the new version roll the deployment back
      Namespace: AWS/Lambda
      MetricName: Errors
      Dimensions:
        - Name: FunctionName
          Value: !Ref {fn}Function
      Statistic: Sum
      Period: 60
      EvaluationPeriods: 2
      Threshold: 1
      ComparisonOperator: GreaterThanOrEqualToThreshold
      TreatMissingData: notBreaching

Outputs:
  FunctionArn:
    Value: !GetAtt {fn}Function.Arn
"""


def write(root: Path, files: dict[str, str]) -> list[str]:
    """Writes new files; CODEOWNERS is replaced (it is generated). Refuses to overwrite anything else."""
    clash = [p for p in files if (root / p).exists() and not p.endswith("CODEOWNERS")]
    if clash:
        raise ScaffoldError(f"would overwrite {', '.join(clash)}")
    for p, text in files.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(text)
    return sorted(files)
