import math
import numpy as np
import asyncio
import httpx

from uuid import uuid4
from collections.abc import Iterable, Callable
from scipy.integrate import cumulative_trapezoid

from .client import HTTPClient
from .models import RequestResult, RunResult
from .config import LoadConfig


ScalarFunction = Callable[[float], float]


async def run(client: HTTPClient, load_config: LoadConfig) -> RunResult:
    pattern = load_config.pattern

    if pattern == "constant":
        return await run_constant(
            client=client,
            duration_seconds=load_config.duration_seconds,
            requests_per_second=load_config.requests_per_second,
            max_in_flight=load_config.max_in_flight,
            timeout=load_config.timeout
        )
    elif pattern == "ramp":
        return await run_ramp(
            client=client,
            duration_seconds=load_config.duration_seconds,
            slope=load_config.slope,
            requests_per_second=load_config.requests_per_second,
            max_in_flight=load_config.max_in_flight,
            timeout=load_config.timeout
        )
    elif pattern == "burst":
        return await run_burst(
            duration_seconds=load_config.duration_seconds,
            requests_per_second=load_config.requests_per_second,
            max_in_flight=load_config.max_in_flight,
            peak_rps=load_config.peak_rps,
            center=load_config.center_seconds,
            width=load_config.width_seconds,
            timeout=load_config.timeout
        )


async def run_constant(
    client: HTTPClient,
    duration_seconds: float,
    requests_per_second: float,
    max_in_flight: int,
    timeout: int,
) -> RunResult:
    inv_rate_fn, total_volume = inverse_cumulative_rate(
        rate_fn=constant_rate(requests_per_second),
        duration=duration_seconds,
    )
    return await run_arrivals(
        client=client,
        arrivals=arrival_iterator(inv_rate_fn, total_volume),
        max_in_flight=max_in_flight,
        timeout=timeout,
    )

async def run_ramp(
    client: HTTPClient,
    duration_seconds: float,
    slope: float,
    requests_per_second: float,
    max_in_flight: int,
    timeout: int,
) -> RunResult:
    inv_rate_fn, total_volume = inverse_cumulative_rate(
        rate_fn=ramp_rate(requests_per_second, slope),
        duration=duration_seconds,
    )
    return await run_arrivals(
        client=client,
        arrivals=arrival_iterator(inv_rate_fn, total_volume),
        max_in_flight=max_in_flight,
        timeout=timeout,
    )

async def run_burst(
    client: HTTPClient,
    duration_seconds: float,
    requests_per_second: float,
    max_in_flight: int,
    peak_rps: float,
    center: float,
    width: float,
    timeout: int,
) -> RunResult:
    inv_rate_fn, total_volume = inverse_cumulative_rate(
        rate_fn=burst_rate(
            baseline_rps=requests_per_second,
            peak_rps=peak_rps,
            center_seconds=center,
            width_seconds=width
        ),
        duration=duration_seconds,
    )
    return await run_arrivals(
        client=client,
        arrivals=arrival_iterator(inv_rate_fn, total_volume),
        max_in_flight=max_in_flight,
        timeout=timeout,
    )


def arrival_iterator(
    inv_rate_fn: ScalarFunction,
    total_volume: float,
) -> Iterable[float]:
    # integration noise
    nearest_integer = round(total_volume)
    if nearest_integer > 0 and math.isclose(
        total_volume, nearest_integer, rel_tol=0.0, abs_tol=1e-9
    ):
        total_volume = float(nearest_integer)

    for i in range(math.ceil(total_volume)):
        yield inv_rate_fn(i)


async def execute_request(
    client: HTTPClient,
    request_id: str,
    scheduled_at: float,
    start: float,
    timeout: int,
) -> RequestResult:
    loop = asyncio.get_running_loop()

    record = RequestResult(
        request_id=request_id,
        outcome="failed",
        scheduled_at=scheduled_at,
        dispatched_at=loop.time() - start,
    )

    try:
        async with asyncio.timeout(timeout):
            response = await client.send()
            response.raise_for_status()
        
        record.outcome = "success"

    except TimeoutError:
        record.error_category = "request_deadline"

    except httpx.HTTPError as exc:
        record.error_category = type(exc).__name__

    except asyncio.CancelledError:
        record.outcome = "cancelled"
        raise

    finally:
        record.finished_at = loop.time() - start

    return record


async def run_arrivals(
    client: HTTPClient,
    arrivals: Iterable[float],
    max_in_flight: int,
    timeout: int,
) -> RunResult:
    if max_in_flight < 1:
        raise ValueError("max_in_flight must be positive")

    loop = asyncio.get_running_loop()
    start = loop.time()

    result = RunResult(
        run_id=str(uuid4()),
        elapsed_seconds=0.0,
    )

    pending: set[asyncio.Task[RequestResult]] = set()
    tasks: list[asyncio.Task[RequestResult]] = []

    async with asyncio.TaskGroup() as group:
        for index, scheduled_at in enumerate(arrivals):
            await asyncio.sleep(
                max(0.0, start + scheduled_at - loop.time())
            )

            if len(pending) >= max_in_flight:
                result.requests.append(
                    RequestResult(
                        request_id=str(index),
                        outcome="skipped",
                        scheduled_at=scheduled_at,
                        error_category="capacity",
                    )
                )
                continue

            task = group.create_task(
                execute_request(
                    client=client,
                    request_id=str(index),
                    scheduled_at=scheduled_at,
                    start=start,
                    timeout=timeout,
                )
            )

            pending.add(task)
            tasks.append(task)
            task.add_done_callback(pending.discard)

    result.requests.extend(
        task.result()
        for task in tasks
    )

    result.elapsed_seconds = loop.time() - start
    return result


def constant_rate(rps: float) -> ScalarFunction:
    return lambda _: rps

def ramp_rate(start_rps: float, slope: float) -> ScalarFunction:
    return lambda t: t * slope + start_rps

def burst_rate(
    baseline_rps: float,
    peak_rps: float,
    center_seconds: float,
    width_seconds: float,
) -> ScalarFunction:
    return lambda t: float(
        baseline_rps
        + (peak_rps - baseline_rps)
        * np.exp(-0.5 * ((t - center_seconds) / width_seconds) ** 2)
    )

def inverse_cumulative_rate(
    rate_fn: ScalarFunction,
    duration: float,
    *,
    samples: int = 10_001,
) -> tuple[ScalarFunction, float]:
    """Return the approximate inverse and total integrated request volume."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Duration must be positive and finite")

    if samples < 2:
        raise ValueError("At least two samples are required")

    times = np.linspace(0.0, duration, samples)
    rates = np.array([rate_fn(float(t)) for t in times])

    if not np.all(np.isfinite(rates)) or np.any(rates < 0):
        raise ValueError("Sampled rates must be finite and nonnegative")

    cumulative = cumulative_trapezoid(rates, times, initial=0.0)

    if not np.all(np.isfinite(cumulative)):
        raise ValueError("Cumulative volume overflowed")

    total = float(cumulative[-1])

    def inverse(position: float) -> float:
        if not math.isfinite(position) or not 0 <= position <= total:
            raise ValueError(f"Position must be between 0 and {total}")

        # Binary search for the earliest time
        right = int(np.searchsorted(cumulative, position, side="left"))

        if right == 0:
            return 0.0

        left = right - 1
        fraction = (
            (position - cumulative[left])
            / (cumulative[right] - cumulative[left])
        )

        return float(
            times[left] + fraction * (times[right] - times[left])
        )

    return inverse, total