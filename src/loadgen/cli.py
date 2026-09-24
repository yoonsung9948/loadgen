import typer
import asyncio

from pathlib import Path
from .config import load_yaml
from .runner import run_scenario
from .output import print_summary, build_summary

app = typer.Typer()


@app.callback()
def main():
    """Inference load generator."""


@app.command()
def run(
    path: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    )
):
    """Run a load test."""
    config = load_yaml(path)
    result = asyncio.run(run_scenario(config))
    result = build_summary(result)
    print_summary(result)


if __name__ == "__main__":
    app()

