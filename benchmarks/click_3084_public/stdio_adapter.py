"""Public stdin/stdout adapter for Click #3084; contains no private test cases.

The trusted control plane runs this inside the evaluation container with the candidate
checkout mounted read-only at ``/candidate`` (or wherever ``DEVFLOW_CANDIDATE_ROOT``
points). One JSON request goes in on stdin, one JSON result comes out on stdout, so the
hidden cases stay outside the candidate mount: they are only ever the stdin/stdout pairs
the controller supplies.

Request: ``{"flag_value": str, "default": str, "argv": [str, ...]}``
Result:  ``{"exit_code": int, "output": str}``
"""
import json
import os
import sys

CANDIDATE_ROOT = os.environ.get("DEVFLOW_CANDIDATE_ROOT", "/candidate")
sys.path.insert(0, os.path.join(CANDIDATE_ROOT, "src"))
import click  # noqa: E402  (import must follow the path insertion)
from click.testing import CliRunner  # noqa: E402

request = json.load(sys.stdin)


@click.command()
@click.option("--name", is_flag=False, flag_value=request["flag_value"],
              default=request["default"])
def command(name):
    click.echo(f"Hello, {name}!")


result = CliRunner().invoke(command, request.get("argv", []))
print(json.dumps({"exit_code": result.exit_code, "output": result.output}, ensure_ascii=False))
