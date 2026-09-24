from dataclasses import dataclass, field
from typing import Literal

@dataclass
class RequestResult:
    request_id: str
    outcome: Literal["success", "failed", "skipped", "cancelled"]

    scheduled_at: float
    dispatched_at: float | None = None
    first_content_at: float | None = None
    finished_at: float | None = None

    error_category: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

@dataclass
class RunResult:
    run_id: str
    elapsed_seconds: float
    requests: list[RequestResult] = field(default_factory=list)


@dataclass
class TimingSummary:
    samples: int
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None

@dataclass
class RunSummary:
    run_id: str
    elapsed_seconds: float

    recorded_arrivals: int
    started: int
    successful: int
    failed: int
    skipped: int
    cancelled: int

    success_rate: float | None
    successful_requests_per_second: float | None

    latency: TimingSummary
    scheduler_lag: TimingSummary
    failure_reasons: dict[str, int]