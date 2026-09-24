import asyncio
import math
import httpx
import numpy as np
import sys

from collections.abc import Callable, Iterable
from uuid import uuid4


from scipy.integrate import cumulative_trapezoid

from .client import HTTPClient
from .config import GammaConfig, LoadConfig
from .models import RequestResult, RunCancelled, RunResult


ScalarFunction = Callable[[float], float]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run(
    client: HTTPClient,
    load_config: LoadConfig,
    dist_config: GammaConfig | None = None,
) -> RunResult:
    pattern = load_config.pattern

    if pattern == "constant":
        return await run_constant(
            client=client,
            duration_seconds=load_config.duration_seconds,
            requests_per_second=load_config.requests_per_second,
            max_in_flight=load_config.max_in_flight,
            timeout=load_config.timeout,
            dist_config=dist_config,
        )

    if pattern == "ramp":
        return await run_ramp(
            client=client,
            duration_seconds=load_config.duration_seconds,
            requests_per_second=load_config.requests_per_second,
            slope=load_config.slope,
            max_in_flight=load_config.max_in_flight,
            timeout=load_config.timeout,
            dist_config=dist_config,
        )

    if pattern == "burst":
        return await run_burst(
            client=client,
            duration_seconds=load_config.duration_seconds,
            requests_per_second=load_config.requests_per_second,
            max_in_flight=load_config.max_in_flight,
            peak_rps=load_config.peak_rps,
            center=load_config.center_seconds,
            width=load_config.width_seconds,
            timeout=load_config.timeout,
            dist_config=dist_config,
        )

    raise ValueError(f"Unsupported load pattern: {pattern}")


# ---------------------------------------------------------------------------
# Load patterns
# ---------------------------------------------------------------------------


async def run_constant(
    client: HTTPClient,
    duration_seconds: float,
    requests_per_second: float,
    max_in_flight: int,
    timeout: float,
    dist_config: GammaConfig | None = None,
) -> RunResult:
    schedule = build_arrivals(
        rate_fn=constant_rate(requests_per_second),
        duration_seconds=duration_seconds,
        dist_config=dist_config,
    )

    return await run_arrivals(
        client=client,
        arrivals=schedule,
        duration_seconds=duration_seconds,
        max_in_flight=max_in_flight,
        timeout=timeout,
    )


async def run_ramp(
    client: HTTPClient,
    duration_seconds: float,
    requests_per_second: float,
    slope: float,
    max_in_flight: int,
    timeout: float,
    dist_config: GammaConfig | None = None,
) -> RunResult:
    schedule = build_arrivals(
        rate_fn=ramp_rate(
            start_rps=requests_per_second,
            slope=slope,
        ),
        duration_seconds=duration_seconds,
        dist_config=dist_config,
    )

    return await run_arrivals(
        client=client,
        arrivals=schedule,
        duration_seconds=duration_seconds,
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
    timeout: float,
    dist_config: GammaConfig | None = None,
) -> RunResult:
    schedule = build_arrivals(
        rate_fn=burst_rate(
            baseline_rps=requests_per_second,
            peak_rps=peak_rps,
            center_seconds=center,
            width_seconds=width,
        ),
        duration_seconds=duration_seconds,
        dist_config=dist_config,
    )

    return await run_arrivals(
        client=client,
        arrivals=schedule,
        duration_seconds=duration_seconds,
        max_in_flight=max_in_flight,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Arrival generation
# ---------------------------------------------------------------------------


def build_arrivals(
    rate_fn: ScalarFunction,
    duration_seconds: float,
    dist_config: GammaConfig | None = None,
) -> Iterable[float]:
    inverse_rate, total_volume = inverse_cumulative_rate(
        rate_fn=rate_fn,
        duration=duration_seconds,
    )

    if dist_config is None:
        return deterministic_arrivals(
            inv_rate_fn=inverse_rate,
            total_volume=total_volume,
        )

    return gamma_arrivals(
        inv_rate_fn=inverse_rate,
        total_volume=total_volume,
        shape=dist_config.shape,
        seed=dist_config.seed,
    )


def deterministic_arrivals(
    inv_rate_fn: ScalarFunction,
    total_volume: float,
) -> Iterable[float]:
    total_volume = normalize_volume(total_volume)

    for position in range(math.ceil(total_volume)):
        yield inv_rate_fn(float(position))


def gamma_arrivals(
    inv_rate_fn: ScalarFunction,
    total_volume: float,
    *,
    shape: float = 1.0,
    seed: int | None = None,
) -> Iterable[float]:
    if not math.isfinite(shape) or shape <= 0:
        raise ValueError("shape must be positive and finite")
    if not math.isfinite(total_volume) or total_volume < 0:
        raise ValueError("total_volume must be nonnegative and finite")

    rng = np.random.default_rng(seed)
    position = 0.0

    # Unit mean gaps preserve the rate curve's cumulative-volume scale.
    gamma_scale = 1.0 / shape

    while position < total_volume:
        gap = float(
            rng.gamma(
                shape=shape,
                scale=gamma_scale,
            )
        )

        next_position = position + gap

        if not math.isfinite(gap) or next_position <= position:
            raise ValueError(
                "Sampled gap cannot advance the schedule"
            )

        if next_position >= total_volume:
            return

        yield inv_rate_fn(next_position)

        position = next_position


def normalize_volume(total_volume: float) -> float:
    nearest_integer = round(total_volume)

    if (
        nearest_integer >= 0
        and np.isclose(
            total_volume,
            nearest_integer,
            rtol=0.0,
            atol=1e-9,
        )
    ):
        return float(nearest_integer)

    return total_volume


# ---------------------------------------------------------------------------
# Request execution
# ---------------------------------------------------------------------------


async def execute_request(
    client: HTTPClient,
    record: RequestResult,
    start: float,
    timeout: float,
) -> None:
    """Update an engine-owned record, even when this task is cancelled."""
    loop = asyncio.get_running_loop()
    record.dispatched_at = loop.time() - start

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
    except Exception as exc:
        record.error_category = type(exc).__name__
        raise
    finally:
        record.finished_at = loop.time() - start


async def report_progress(
    pending: set,
    result: RunResult,
    start: float,
) -> None:
    loop = asyncio.get_running_loop()

    while True:
        elapsed = loop.time() - start
        finished = sum(
            record.finished_at is not None
            for record in result.requests
        )

        print(
            f"[{elapsed:.0f}s] "
            f"in-flight={len(pending)} "
            f"finished={finished}",
            file=sys.stderr,
            flush=True,
        )

        await asyncio.sleep(5)


async def run_arrivals(
    client: HTTPClient,
    arrivals: Iterable[float],
    max_in_flight: int,
    timeout: float,
    *,
    duration_seconds: float,
) -> RunResult:
    if max_in_flight < 1:
        raise ValueError("max_in_flight must be positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive and finite")

    loop = asyncio.get_running_loop()
    start = loop.time()
    result = RunResult(
        run_id=str(uuid4()),
        elapsed_seconds=0.0,
        scheduled_duration_seconds=duration_seconds,
    )
    pending: set[asyncio.Task[None]] = set()

    def request_done(task: asyncio.Task[None], record: RequestResult) -> None:
        pending.discard(task)
        # A task cancelled before its first execution never enters its finally block.
        if task.cancelled() and record.finished_at is None:
            record.outcome = "cancelled"
            record.finished_at = loop.time() - start


    progress_task = asyncio.create_task(
        report_progress(pending, result, start)
    )
    try:
        async with asyncio.TaskGroup() as group:
            previous_offset = 0.0
            for index, scheduled_at in enumerate(arrivals):
                if (
                    not math.isfinite(scheduled_at)
                    or scheduled_at < previous_offset
                    or not 0 <= scheduled_at < duration_seconds
                ):
                    raise ValueError("Arrivals must be ordered and within the test window")
                previous_offset = scheduled_at
                await sleep_until(loop, start, scheduled_at)

                pending.difference_update(task for task in tuple(pending) if task.done())
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

                # Register before starting the task so cancellation cannot lose the record.
                record = RequestResult(
                    request_id=str(index),
                    outcome="failed",
                    scheduled_at=scheduled_at,
                )
                result.requests.append(record)
                task = group.create_task(execute_request(client, record, start, timeout))
                pending.add(task)
                task.add_done_callback(lambda task, record=record: request_done(task, record))

            # Empty or sparse schedules still observe the entire configured window.
            # TaskGroup then drains remaining requests, bounded by their deadlines.
            await sleep_until(loop, start, duration_seconds)
    except asyncio.CancelledError as exc:
        result.status = "cancelled"
        raise RunCancelled(result) from exc
    except Exception:
        result.status = "failed"
        raise
    finally:
        result.elapsed_seconds = loop.time() - start
        # Ensure pre-start cancellations are accounted for before exposing partial results.
        for record in result.requests:
            if record.outcome != "skipped" and record.finished_at is None:
                record.finished_at = result.elapsed_seconds
                if result.status == "cancelled":
                    record.outcome = "cancelled"
                else:
                    record.error_category = "run_aborted"
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)

    return result


async def sleep_until(
    loop: asyncio.AbstractEventLoop,
    start: float,
    scheduled_at: float,
) -> None:
    delay = (
        start
        + scheduled_at
        - loop.time()
    )

    await asyncio.sleep(
        max(0.0, delay)
    )


# ---------------------------------------------------------------------------
# Rate functions
# ---------------------------------------------------------------------------


def constant_rate(
    rps: float,
) -> ScalarFunction:
    if rps < 0:
        raise ValueError(
            "rps must be nonnegative"
        )

    return lambda _: rps


def ramp_rate(
    start_rps: float,
    slope: float,
) -> ScalarFunction:
    return lambda t: (
        start_rps + t * slope
    )


def burst_rate(
    baseline_rps: float,
    peak_rps: float,
    center_seconds: float,
    width_seconds: float,
) -> ScalarFunction:
    if width_seconds <= 0:
        raise ValueError(
            "width_seconds must be positive"
        )

    return lambda t: float(
        baseline_rps
        + (
            peak_rps
            - baseline_rps
        )
        * np.exp(
            -0.5
            * (
                (
                    t
                    - center_seconds
                )
                / width_seconds
            )
            ** 2
        )
    )


# ---------------------------------------------------------------------------
# Cumulative rate inversion
# ---------------------------------------------------------------------------


def inverse_cumulative_rate(
    rate_fn: ScalarFunction,
    duration: float,
    *,
    samples: int = 10_001,
) -> tuple[ScalarFunction, float]:
    if (
        not math.isfinite(duration)
        or duration <= 0
    ):
        raise ValueError(
            "Duration must be positive and finite"
        )

    if samples < 2:
        raise ValueError(
            "At least two samples are required"
        )

    times = np.linspace(
        0.0,
        duration,
        samples,
    )

    rates = np.array(
        [
            rate_fn(float(t))
            for t in times
        ],
        dtype=float,
    )

    if (
        not np.all(np.isfinite(rates))
        or np.any(rates < 0)
    ):
        raise ValueError(
            "Sampled rates must be finite and nonnegative"
        )

    cumulative = cumulative_trapezoid(
        rates,
        times,
        initial=0.0,
    )

    if not np.all(
        np.isfinite(cumulative)
    ):
        raise ValueError(
            "Cumulative volume overflowed"
        )

    total = float(cumulative[-1])

    def inverse(position: float) -> float:
        if (
            not math.isfinite(position)
            or position < 0
            or position > total
        ):
            raise ValueError(
                f"Position must be between 0 and {total}"
            )

        if position == 0:
            return 0.0

        right = int(
            np.searchsorted(
                cumulative,
                position,
                side="left",
            )
        )

        # position == total can put us at the final
        # sample, which is valid.
        right = min(
            right,
            len(cumulative) - 1,
        )

        # Move left across a flat cumulative region.
        #
        # We need two samples with increasing cumulative
        # volume in order to interpolate safely.
        left = right - 1

        while (
            left >= 0
            and cumulative[left]
            == cumulative[right]
        ):
            left -= 1

        if left < 0:
            return float(times[right])

        volume_delta = (
            cumulative[right]
            - cumulative[left]
        )

        if volume_delta <= 0:
            return float(times[right])

        fraction = (
            position
            - cumulative[left]
        ) / volume_delta

        return float(
            times[left]
            + fraction
            * (
                times[right]
                - times[left]
            )
        )

    return inverse, total