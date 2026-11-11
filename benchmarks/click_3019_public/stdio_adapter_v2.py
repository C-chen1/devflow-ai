"""Public Click adapter supporting both explicit and omitted prompt suffixes."""
import json
import sys

sys.path.insert(0, "/candidate/src")
import click
from click.testing import CliRunner

request = json.load(sys.stdin)
options = {} if request.get("use_default") else {"prompt_suffix": request["suffix"]}


@click.command()
def command():
    if request["kind"] == "prompt":
        click.prompt(request["label"], **options)
    elif request["kind"] == "confirm":
        click.confirm(request["label"], **options)
    else:
        raise ValueError("Unknown command kind")


result = CliRunner().invoke(command, input=request["answer"] + "\n")
print(json.dumps({"exit_code": result.exit_code, "output": result.output}, ensure_ascii=False))
