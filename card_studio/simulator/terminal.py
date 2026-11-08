from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from card_studio import __version__
from card_studio.application import CardStudioApplication
from card_studio.emv.unpredictable import UnpredictableNumberError
from card_studio.host.mock import MockAuthorizationHost
from card_studio.kernels.base import KernelAdapter
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.internal import internal_profile
from card_studio.money import Money
from card_studio.profiles.model import CardProfile
from card_studio.transactions.model import TransactionRequest

from .icc import SyntheticIccReader


@dataclass(frozen=True)
class TerminalStep:
    action: str
    value: Any = None

    def __post_init__(self) -> None:
        if self.action not in {
            "EXECUTE", "EXECUTE_NEW", "FAULT", "RESET", "RESTART", "QUERY", "PUBLISH_LATE",
            "CANCEL", "ASSERT_SEQUENCE", "ASSERT_HOST_REFERENCE",
        }:
            raise ValueError(f"Unsupported terminal step: {self.action}")


@dataclass(frozen=True)
class TerminalScenario:
    scenario_id: str
    interface: str
    kernel_scenario: str = "approved"
    schema_version: int = 1
    steps: tuple[TerminalStep, ...] = (TerminalStep("EXECUTE"),)
    expected_state: str = "SUCCEEDED"
    expected_error: str | None = None
    expected_instructions: tuple[int, ...] = ()
    expected_directory: str | None = None
    unpredictable_number: str | None = None
    host_timeout: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Terminal scenarios require schema_version=1.")
        if self.interface not in {"CONTACT", "CONTACTLESS"}:
            raise ValueError("Terminal scenarios require CONTACT or CONTACTLESS.")
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "expected_instructions", tuple(self.expected_instructions))

    def digest(self) -> str:
        import hashlib

        value = {
            "scenario_id": self.scenario_id,
            "schema_version": self.schema_version,
            "interface": self.interface,
            "kernel_scenario": self.kernel_scenario,
            "steps": [{"action": step.action, "value": step.value} for step in self.steps],
            "expected_state": self.expected_state,
            "expected_error": self.expected_error,
            "expected_instructions": list(self.expected_instructions),
            "unpredictable_number": self.unpredictable_number,
            "host_timeout": self.host_timeout,
            "metadata": dict(self.metadata),
        }
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass
class TerminalRun:
    scenario_id: str
    status: str
    result: dict[str, Any]
    icc: dict[str, Any]
    assertions: list[dict[str, Any]]
    scenario_sha256: str
    terminal_version: str = __version__
    mode: str = "SIMULATED"
    authority: str = "EXTERNAL_UNVERIFIED"
    physical_validation: str = "NOT_EXECUTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "result": self.result,
            "icc": self.icc,
            "assertions": self.assertions,
            "scenario_sha256": self.scenario_sha256,
            "terminal_version": self.terminal_version,
            "simulation": True,
            "mode": self.mode,
            "laboratory_mode": "LAB_ONLY",
            "evidence_level": "SIMULATED",
            "authority": self.authority,
            "certification": "NOT COMPLETED",
            "physical_validation": self.physical_validation,
        }


class TerminalSimulator:
    def __init__(
        self,
        directory: Path,
        kernel_factory: Callable[..., KernelAdapter] = InternalEmvKernel,
    ):
        self.directory = directory
        self.kernel_factory = kernel_factory

    def _profile(self, kernel: KernelAdapter, scenario: TerminalScenario) -> CardProfile:
        profile = internal_profile(kernel, scenario.scenario_id, scenario.interface, scenario.kernel_scenario)
        if scenario.unpredictable_number is None:
            return profile
        value = profile.to_dict()
        execution = dict(value["scheme_parameters"]["emv_execution"])
        execution["pdol"] = [*execution["pdol"], {"tag": "9F37", "length": 4, "data_type": "BINARY"}]
        execution["unpredictable_number"] = {"required": True}
        value["scheme_parameters"] = {**value["scheme_parameters"], "emv_execution": execution}
        value["profile_digest"] = ""
        return CardProfile.from_dict(value)

    def _un_source(self, scenario: TerminalScenario) -> tuple[Callable[[], bytes], bytes]:
        mode = scenario.unpredictable_number
        strong = bytes.fromhex("01020304")
        if mode == "weak":
            return lambda: bytes(4), strong
        if mode == "repeated":
            return lambda: strong, strong
        return lambda: strong, strong

    def run(self, scenario: TerminalScenario) -> TerminalRun:
        assertions: list[dict[str, Any]] = []

        def check(name: str, actual: Any, expected: Any) -> None:
            assertions.append({
                "name": name,
                "status": "PASS" if actual == expected else "FAIL",
                "actual": actual,
                "expected": expected,
            })

        work = self.directory / scenario.scenario_id
        reader = SyntheticIccReader()
        try:
            kernel = self.kernel_factory()
        except TypeError:
            kernel = self.kernel_factory
        if not isinstance(kernel, InternalEmvKernel):
            return TerminalRun(
                scenario.scenario_id,
                "NON_CONFORMANT",
                {},
                reader.snapshot(),
                [{"name": "internal_kernel_required", "status": "NON_CONFORMANT", "actual": type(kernel).__name__}],
                scenario.digest(),
            )
        integration = scenario.metadata.get("integration")
        if integration is not None:
            from .integration import run_integration

            try:
                outcome = run_integration(str(integration), work)
                check("integration_execution_state", outcome.result.get("execution_state"), scenario.expected_state)
                check("integration_error_code", outcome.result.get("error_code"), scenario.expected_error)
                outcome.assertions.extend(assertions)
                return TerminalRun(
                    scenario.scenario_id,
                    outcome.status,
                    outcome.result,
                    outcome.icc,
                    outcome.assertions,
                    scenario.digest(),
                )
            except Exception as error:
                assertions.append({
                    "name": "integration_execution",
                    "status": "FAIL",
                    "actual": getattr(error, "code", type(error).__name__),
                })
                return TerminalRun(
                    scenario.scenario_id,
                    "FAIL",
                    {"execution_state": "FAILED", "error_code": getattr(error, "code", type(error).__name__)},
                    reader.snapshot(),
                    assertions,
                    scenario.digest(),
                )
        source, configured_un = self._un_source(scenario)
        kernel = self.kernel_factory(unpredictable_number_source=source)
        profile = self._profile(kernel, scenario)
        profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
        host = MockAuthorizationHost(
            work / "host-v1.sqlite3",
            timeout_operations={scenario.scenario_id} if scenario.host_timeout else set(),
        )

        def application() -> CardStudioApplication:
            return CardStudioApplication(
                data_dir=work,
                profiles=[profile],
                kernels=kernel,
                reader=reader,
                host=host,
            )

        app = application()
        request = TransactionRequest.create(
            money=Money(1000, "MXN", 2),
            profile_ref=profile_ref,
            interface=scenario.interface,
            kernel_ref=kernel.kernel_ref,
            scenario=scenario.kernel_scenario,
            operation_id=scenario.scenario_id,
            idempotency_key=f"key-{scenario.scenario_id}",
        )
        reader.configure(profile, request, configured_un)
        result: dict[str, Any] = {}
        execution_error: str | None = None
        try:
            for step in scenario.steps:
                if step.action == "EXECUTE":
                    result = app.create_transaction(request)
                elif step.action == "EXECUTE_NEW":
                    request = TransactionRequest.create(
                        money=request.money,
                        profile_ref=request.profile_ref,
                        interface=request.interface,
                        kernel_ref=request.kernel_ref,
                        scenario=request.scenario,
                        operation_id=f"{scenario.scenario_id}-second",
                        idempotency_key=f"key-{scenario.scenario_id}-second",
                    )
                    reader.reset()
                    reader.configure(profile, request, configured_un)
                    result = app.create_transaction(request)
                elif step.action == "FAULT":
                    name, _, raw = str(step.value).partition(":")
                    instruction = int(raw, 16)
                    if name == "malformed":
                        reader.malformed_instruction = instruction
                    elif name == "torn":
                        reader.torn_at = instruction
                    elif name == "card_removed":
                        reader.remove_card_at = instruction
                    elif name == "reader_removed":
                        reader.remove_reader_at = instruction
                    elif name == "reader_absent":
                        reader.remove_reader()
                    else:
                        raise ValueError(f"Unknown ICC fault: {name}")
                elif step.action == "RESET":
                    reader.reset()
                elif step.action == "RESTART":
                    kernel = self.kernel_factory(unpredictable_number_source=source)
                    host = MockAuthorizationHost(work / "host-v1.sqlite3")
                    app = application()
                elif step.action == "QUERY":
                    result = app.query(request.operation_id)
                elif step.action == "PUBLISH_LATE":
                    host.publish_late_result(request.operation_id, str(step.value))
                elif step.action == "CANCEL":
                    result = app.cancel(request.operation_id)
                elif step.action == "ASSERT_SEQUENCE":
                    check("exact_apdu_instructions", reader.instructions, list(step.value))
                elif step.action == "ASSERT_HOST_REFERENCE":
                    reference = result.get("details", {}).get("host_reference")
                    queried = host.query(request.operation_id).get("host_reference")
                    check("host_reference_trace", reference, queried)
        except UnpredictableNumberError as error:
            execution_error = error.code
            result = {"execution_state": "FAILED", "error_code": error.code}
        except Exception as error:
            execution_error = getattr(error, "code", type(error).__name__)
            if not result:
                try:
                    result = app.get(request.operation_id)
                except Exception:
                    result = {}
            assertions.append({"name": "unexpected_exception", "status": "FAIL", "actual": execution_error})

        check("execution_state", result.get("execution_state"), scenario.expected_state)
        check("error_code", result.get("error_code", execution_error), scenario.expected_error)
        if scenario.expected_instructions:
            check("scenario_apdu_instructions", reader.instructions, list(scenario.expected_instructions))
        if scenario.expected_directory is not None:
            check("directory", reader.directory, scenario.expected_directory)
        if scenario.host_timeout and result.get("execution_state") == "SUCCEEDED":
            check("single_authorization", host.operation_count("authorize"), 1)
            details = result.get("details", {})
            response = host.query(request.operation_id)
            check("late_host_reference", details.get("host_reference"), response.get("host_reference"))
            check("late_host_timestamp", details.get("host_response_timestamp"), response.get("timestamp"))
        status = "PASS" if all(item.get("status") == "PASS" for item in assertions) else "FAIL"
        return TerminalRun(
            scenario.scenario_id,
            status,
            result,
            reader.snapshot(),
            assertions,
            scenario.digest(),
        )


def scenario_digest(scenarios: Iterable[TerminalScenario]) -> str:
    import hashlib

    raw = json.dumps([item.digest() for item in scenarios], separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()
