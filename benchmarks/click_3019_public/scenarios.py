"""Public behavior checks derived from Click issue 3019; not hidden evaluation tests."""
import click
import os
import pytest
from pathlib import Path
from click.testing import CliRunner


@pytest.fixture(scope="session", autouse=True)
def check_target_source():
    """Ensure pytest is testing the selected checkout rather than an installed Click."""
    source_root = os.environ.get("DEVFLOW_CLICK_SOURCE") or str(Path.cwd() / "src")
    if not Path(source_root).is_dir():
        pytest.fail("Select the checkout's src directory or run from the checkout root")
    if not Path(click.__file__).resolve().is_relative_to(Path(source_root).resolve()):
        pytest.fail("The imported Click does not belong to the selected checkout")


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [("", "testfoo\n"), (" ", "test foo\n")],
    ids=("empty-suffix", "space-suffix"),
)
def test_prompt_suffix(suffix, expected):
    @click.command()
    def command():
        click.prompt("test", prompt_suffix=suffix)

    result = CliRunner().invoke(command, input="foo\n")
    assert result.exit_code == 0
    assert result.output == expected


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [("", "test [y/N]y\n"), (" ", "test [y/N] y\n")],
    ids=("empty-suffix", "space-suffix"),
)
def test_confirm_suffix(suffix, expected):
    @click.command()
    def command():
        click.confirm("test", prompt_suffix=suffix)

    result = CliRunner().invoke(command, input="y\n")
    assert result.exit_code == 0
    assert result.output == expected
