from __future__ import annotations

from typing import Any

from .model import AssertionResult


class AssertionRecorder:
    def __init__(self):
        self.items: list[AssertionResult] = []

    def check(self, name: str, condition: bool, detail: str) -> None:
        self.items.append(AssertionResult(name, "PASS" if condition else "FAIL", detail))

    def equal(self, name: str, actual: Any, expected: Any) -> None:
        self.check(name, actual == expected, f"expected={expected}; actual={actual}")

    def extend(self, values: list[AssertionResult]) -> None:
        self.items.extend(values)

    @property
    def passed(self) -> bool:
        return bool(self.items) and all(item.status == "PASS" for item in self.items)


def assert_terminal_transition(transitions: list[dict[str, Any]], state: str) -> AssertionResult:
    found = any(item.get("new_state") == state for item in transitions)
    return AssertionResult(
        "terminal_transition",
        "PASS" if found else "FAIL",
        f"required={state}; present={found}",
    )
