from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .observability import percentile


def run_progressive_load(
    operation: Callable[[str, str], int],
    steps: tuple[int, ...] = (1, 10, 25, 50, 100),
    max_workers: int = 10,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = run_id or f"p4-load-{uuid.uuid4()}"
    reports = []
    for terminals in steps:
        started = time.monotonic()
        def invoke(index: int) -> tuple[int, float]:
            terminal_id = f"terminal-p4-{index:03d}"
            operation_id = f"{run_id}-t{terminals}-n{index}"
            operation_started = time.monotonic()
            try:
                status = operation(terminal_id, operation_id)
            except Exception:
                status = 599
            return status, (time.monotonic() - operation_started) * 1000
        with ThreadPoolExecutor(max_workers=min(max_workers, terminals)) as executor:
            results = list(executor.map(invoke, range(1, terminals + 1)))
        elapsed = max(time.monotonic() - started, 0.000001)
        statuses = [item[0] for item in results]
        latencies = [item[1] for item in results]
        reports.append({
            "terminals": terminals,
            "requests": len(results),
            "throughput_per_second": round(len(results) / elapsed, 3),
            "elapsed_seconds": round(elapsed, 6),
            "p50_ms": percentile(latencies, 50),
            "p95_ms": percentile(latencies, 95),
            "p99_ms": percentile(latencies, 99),
            "errors": sum(status >= 400 for status in statuses),
            "4xx": sum(400 <= status < 500 for status in statuses),
            "5xx": sum(status >= 500 for status in statuses),
            "retries": 0,
            "status_counts": {str(status): statuses.count(status) for status in sorted(set(statuses))},
        })
    return {
        "run_id": run_id,
        "steps": reports,
        "total_requests": sum(item["requests"] for item in reports),
        "total_errors": sum(item["errors"] for item in reports),
        "autoscaling_behavior": "FIXED_SINGLE_TASK_STAGING_NO_AUTOSCALING_CONFIGURED",
        "monetary_operations": False,
        "simulation": True,
    }
