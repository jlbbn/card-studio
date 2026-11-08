from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered or not 0 <= quantile <= 100:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


@dataclass
class RequestMetrics:
    started: float = field(default_factory=time.monotonic)
    latencies_ms: list[float] = field(default_factory=list)
    status_codes: list[int] = field(default_factory=list)
    retries: int = 0
    counters: dict[str, int] = field(default_factory=dict)

    def record(self, status_code: int, latency_ms: float) -> None:
        self.status_codes.append(status_code)
        self.latencies_ms.append(latency_ms)

    def increment(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(time.monotonic() - self.started, 0.000001)
        count = len(self.status_codes)
        return {
            "requests": count,
            "request_rate_per_second": round(count / elapsed, 3),
            "latency_ms": {
                "p50": percentile(self.latencies_ms, 50),
                "p95": percentile(self.latencies_ms, 95),
                "p99": percentile(self.latencies_ms, 99),
            },
            "4xx": sum(400 <= status < 500 for status in self.status_codes),
            "5xx": sum(500 <= status < 600 for status in self.status_codes),
            "errors": sum(status >= 400 for status in self.status_codes),
            "retries": self.retries,
            **self.counters,
        }


@dataclass(frozen=True)
class Alarm:
    alarm_id: str
    metric: str
    threshold: float
    comparison: str = "GREATER_THAN"
    severity: str = "HIGH"


REQUIRED_ALARMS = (
    Alarm("service-unavailable", "service_unavailable", 0, severity="CRITICAL"),
    Alarm("alb-5xx", "http_5xx", 0),
    Alarm("target-unhealthy", "unhealthy_targets", 0, severity="CRITICAL"),
    Alarm("high-latency", "latency_p95_ms", 2000),
    Alarm("ecs-cpu", "ecs_cpu_percent", 80),
    Alarm("ecs-memory", "ecs_memory_percent", 85),
    Alarm("rds-cpu", "db_cpu_percent", 80),
    Alarm("rds-free-storage", "db_free_storage_mb", 1024, comparison="LESS_THAN"),
    Alarm("db-connections-abnormal", "db_connections", 80),
    Alarm("reconciliation-backlog", "reconciliation_lag", 10),
    Alarm("repeated-authentication-failures", "authentication_failures", 5),
)


class AlarmEvaluator:
    def __init__(self, alarms: Iterable[Alarm] = REQUIRED_ALARMS):
        self.alarms = {alarm.alarm_id: alarm for alarm in alarms}

    def evaluate(self, alarm_id: str, value: float | None) -> dict[str, Any]:
        alarm = self.alarms[alarm_id]
        if value is None:
            state = "INSUFFICIENT_DATA"
        elif alarm.comparison == "GREATER_THAN":
            state = "ALARM" if value > alarm.threshold else "OK"
        elif alarm.comparison == "LESS_THAN":
            state = "ALARM" if value < alarm.threshold else "OK"
        else:
            raise ValueError("Comparación de alarma inválida.")
        return {
            "alarm_id": alarm_id,
            "metric": alarm.metric,
            "threshold": alarm.threshold,
            "comparison": alarm.comparison,
            "severity": alarm.severity,
            "value": value,
            "state": state,
        }

    def snapshot(self, metrics: Mapping[str, float | None]) -> list[dict[str, Any]]:
        return [self.evaluate(alarm_id, metrics.get(alarm.metric)) for alarm_id, alarm in self.alarms.items()]
