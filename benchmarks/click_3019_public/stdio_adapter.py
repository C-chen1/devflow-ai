"""Public stdin/stdout adapter for Click #3019; contains no private test cases."""
import json
import sys

sys.path.insert(0, "/candidate/src")
import click
from click.testing import CliRunner

request = json.load(sys.stdin)


@click.command()
def command():
    if request["kind"] == "prompt":
        click.prompt(request["label"], prompt_suffix=request["suffix"])
    elif request["kind"] == "confirm":
        click.confirm(request["label"], prompt_suffix=request["suffix"])
    else:
        raise ValueError("Unknown command kind")


result = CliRunner().invoke(command, input=request["answer"] + "\n")
print(json.dumps({"exit_code": result.exit_code, "output": result.output}, ensure_ascii=False))
