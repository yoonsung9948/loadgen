import typer
import asyncio

from pathlib import Path
from .models import RunCancelled
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
    ),
):
    """Run a load test."""
    config = load_yaml(path)
    async def execute():
        # Handle cancellation inside the loop, before asyncio.run translates Ctrl+C.
        try:
            return await run_scenario(config)
        except RunCancelled as exc:
            return exc.result

    result = asyncio.run(execute())
    print_summary(build_summary(result))
    if result.status == "cancelled":
        raise typer.Exit(code=130)


if __name__ == "__main__":
    app()

