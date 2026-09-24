from .config import Config
from .models import RunResult
from .client import HTTPClient
from .engine import run

async def run_scenario(config: Config) -> RunResult:
    async with HTTPClient(config.target, config.request) as client:
        result = await run(client, config.load)
        return result