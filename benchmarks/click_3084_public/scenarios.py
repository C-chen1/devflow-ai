"""Public behaviour checks for pallets/click#3084, derived from the issue and the docs.

They use only the public API through CliRunner. No implementation detail is asserted.
"""
import click
from click.testing import CliRunner


def _make_command():
    @click.command()
    @click.option("--name", is_flag=False, flag_value="Flag", default="Default")
    def hello(name):
        click.echo(f"Hello, {name}!")

    return hello


def test_flag_value_used_when_the_flag_is_given_alone():
    """The documented behaviour from issue #3084: `--name` alone must use `flag_value`."""
    result = CliRunner().invoke(_make_command(), ["--name"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Hello, Flag!"


def test_default_used_when_the_option_is_absent():
    result = CliRunner().invoke(_make_command(), [])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Hello, Default!"


def test_explicit_value_still_wins():
    result = CliRunner().invoke(_make_command(), ["--name", "Ada"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Hello, Ada!"
