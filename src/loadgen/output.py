import sys
from typing import TextIO
from collections import Counter
from collections.abc import Iterable

import numpy as np

from .models import RunResult, RunSummary, TimingSummary


def build_timing_summary(values_ms: Iterable[float]) -> TimingSummary:
    values = np.asarray(list(values_ms), dtype=float)

    if values.size == 0:
        return TimingSummary(
            samples=0,
            mean_ms=None,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
            max_ms=None,
        )

    p50, p95, p99 = np.percentile(values, [50, 95, 99])

    return TimingSummary(
        samples=int(values.size),
        mean_ms=float(values.mean()),
        p50_ms=float(p50),
        p95_ms=float(p95),
        p99_ms=float(p99),
        max_ms=float(values.max()),
    )


def build_summary(result: RunResult) -> RunSummary:
    records = result.requests
    counts = Counter(record.outcome for record in records)
    started = sum(record.dispatched_at is not None for record in records)

    errors = Counter(
        record.error_category or "unspecified"
        for record in records
        if record.outcome == "failed"
    )

    latency = build_timing_summary(
        (record.finished_at - record.dispatched_at) * 1000
        for record in records
        if record.outcome == "success"
        and record.dispatched_at is not None
        and record.finished_at is not None
    )

    scheduler_lag = build_timing_summary(
        (record.dispatched_at - record.scheduled_at) * 1000
        for record in records
        if record.dispatched_at is not None
    )

    return RunSummary(
        run_id=result.run_id,
        status=result.status,
        scheduled_duration_seconds=result.scheduled_duration_seconds,
        elapsed_seconds=result.elapsed_seconds,
        recorded_arrivals=len(records),
        started=started,
        successful=counts["success"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        cancelled=counts["cancelled"],
        success_rate=counts["success"] / started if started else None,
        successful_requests_per_second=(
            counts["success"] / result.elapsed_seconds
            if result.elapsed_seconds > 0
            else None
        ),
        latency=latency,
        scheduler_lag=scheduler_lag,
        failure_reasons=dict(errors),
    )



def print_summary(
    summary: RunSummary,
    writer: TextIO | None = None,
) -> None:
    if writer is None:
        writer = sys.stdout

    width = 64

    def row(label: str, value: str) -> None:
        print(f"  {label:<32} {value:>26}", file=writer)

    def section(title: str) -> None:
        print(f"\n{title}", file=writer)
        print("-" * width, file=writer)

    def number(value: float | None, unit: str = "") -> str:
        if value is None:
            return "N/A"
        return f"{value:,.2f}{unit}"

    print("=" * width, file=writer)
    print("LOAD TEST SUMMARY".center(width), file=writer)
    print("=" * width, file=writer)

    row("Run ID", summary.run_id)
    row("Status", summary.status)
    row("Configured load window", number(summary.scheduled_duration_seconds, " s"))
    row("Elapsed (including drain)", number(summary.elapsed_seconds, " s"))

    section("REQUESTS")
    row("Recorded arrivals", f"{summary.recorded_arrivals:,}")
    row("Started", f"{summary.started:,}")
    row("Successful", f"{summary.successful:,}")
    row("Failed", f"{summary.failed:,}")
    row("Skipped", f"{summary.skipped:,}")
    row("Cancelled", f"{summary.cancelled:,}")

    section("PERFORMANCE")
    row(
        "Success / started",
        (
            f"{summary.success_rate:.2%}"
            if summary.success_rate is not None
            else "N/A"
        ),
    )
    row(
        "Successes / total elapsed",
        number(summary.successful_requests_per_second, " req/s"),
    )

    section("TIMINGS")
    print(
        f"  {'Metric':<16}"
        f"{'Success latency':>21}"
        f"{'Scheduler lag':>21}",
        file=writer,
    )

    latency = summary.latency
    lag = summary.scheduler_lag

    print(
        f"  {'Samples':<16}"
        f"{latency.samples:>21,}"
        f"{lag.samples:>21,}",
        file=writer,
    )

    for label, attribute in (
        ("Mean", "mean_ms"),
        ("p50", "p50_ms"),
        ("p95", "p95_ms"),
        ("p99", "p99_ms"),
        ("Max", "max_ms"),
    ):
        print(
            f"  {label:<16}"
            f"{number(getattr(latency, attribute), ' ms'):>21}"
            f"{number(getattr(lag, attribute), ' ms'):>21}",
            file=writer,
        )

    if summary.failure_reasons:
        section("FAILURE REASONS")
        for reason, count in sorted(
            summary.failure_reasons.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            row(reason, f"{count:,}")

    print("\n" + "=" * width, file=writer)