from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from card_studio.transactions.model import FunctionalResult, SimulationScenario, TransactionRequest


CVM_RESULTS = frozenset({
    "NO_CVM",
    "SIGNATURE",
    "ONLINE_PIN_REQUIRED",
    "OFFLINE_PIN_REQUIRED",
    "CDCVM",
    "CVM_FAILED",
    "NOT_SUPPORTED",
})

_CVM_ALIASES = {
    "NONE": "NO_CVM",
    "SIGNATURE_SIMULATED": "SIGNATURE",
    "CDCVM_SIMULATED": "CDCVM",
}


@dataclass(frozen=True)
class CvmDecision:
    result: str
    synthetic: bool = True
    pin_captured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "synthetic": self.synthetic,
            "pin_captured": self.pin_captured,
        }


@dataclass(frozen=True)
class RiskDecision:
    online_required: bool
    floor_limit_exceeded: bool
    interface_limit_exceeded: bool
    randomly_selected: bool
    deterministic_sample: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "online_required": self.online_required,
            "floor_limit_exceeded": self.floor_limit_exceeded,
            "interface_limit_exceeded": self.interface_limit_exceeded,
            "randomly_selected": self.randomly_selected,
            "deterministic_sample": self.deterministic_sample,
            "reasons": list(self.reasons),
            "synthetic": True,
        }


def _cvm_configuration(profile: Any) -> Mapping[str, Any]:
    configured = profile.scheme_parameters.get("cvm", {})
    if isinstance(configured, str):
        return {"method": configured}
    return configured if isinstance(configured, Mapping) else {}


def configured_cvm(profile: Any) -> str:
    config = _cvm_configuration(profile)
    selected = str(config.get("method", "NO_CVM")).upper()
    return _CVM_ALIASES.get(selected, selected)


def decide_cvm(profile: Any) -> CvmDecision:
    selected = configured_cvm(profile)
    if selected not in CVM_RESULTS:
        return CvmDecision("NOT_SUPPORTED")
    normalized_capabilities = {
        _CVM_ALIASES.get(item, item) for item in profile.cvm_capabilities
    }
    if selected not in normalized_capabilities and selected not in {"CVM_FAILED", "NOT_SUPPORTED"}:
        return CvmDecision("NOT_SUPPORTED")
    return CvmDecision(selected)


def decide_risk(request: TransactionRequest, profile: Any) -> RiskDecision:
    config = profile.scheme_parameters.get("risk", {})
    if not isinstance(config, Mapping):
        config = {}
    exceptions = {str(item).upper() for item in config.get("exceptions", ())}
    amount = request.money.amount_minor
    floor_limit = int(config.get("floor_limit_minor", 0))
    interface_limits = config.get("interface_limits_minor", {})
    interface_limit = int(interface_limits.get(request.interface, 0)) if isinstance(interface_limits, Mapping) else 0
    floor_exceeded = floor_limit > 0 and amount > floor_limit
    interface_exceeded = interface_limit > 0 and amount > interface_limit
    seed = "|".join((
        "LAB_ONLY",
        request.operation_id,
        request.idempotency_key,
        request.profile_ref,
        request.interface,
        request.money.currency,
        str(amount),
    )).encode("utf-8")
    sample = int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") % 10000
    percent = float(config.get("random_selection_percent", 0))
    if not 0 <= percent <= 100:
        raise ValueError("random_selection_percent debe estar entre 0 y 100.")
    randomly_selected = "SKIP_RANDOM_SELECTION" not in exceptions and sample < round(percent * 100)
    force_online = bool(config.get("force_online")) or "FORCE_ONLINE" in exceptions
    force_offline = "FORCE_OFFLINE" in exceptions
    reasons = []
    if floor_exceeded:
        reasons.append("FLOOR_LIMIT")
    if interface_exceeded:
        reasons.append("INTERFACE_LIMIT")
    if randomly_selected:
        reasons.append("DETERMINISTIC_SELECTION")
    if force_online:
        reasons.append("PROFILE_EXCEPTION")
    online = (floor_exceeded or interface_exceeded or randomly_selected or force_online) and not force_offline
    return RiskDecision(
        online_required=online,
        floor_limit_exceeded=floor_exceeded,
        interface_limit_exceeded=interface_exceeded,
        randomly_selected=randomly_selected,
        deterministic_sample=sample,
        reasons=tuple(reasons),
    )


def terminal_action(request: TransactionRequest, profile: Any, risk: RiskDecision) -> str:
    scenario_actions = {
        SimulationScenario.APPROVED.value: "APPROVED",
        SimulationScenario.RETRY.value: "APPROVED",
        SimulationScenario.DECLINED.value: "DECLINED",
        SimulationScenario.ONLINE_REQUIRED.value: "ONLINE_REQUIRED",
        SimulationScenario.ACTION_REQUIRED.value: "ACTION_REQUIRED",
    }
    configured = profile.scheme_parameters.get("terminal_action", "APPROVED")
    if isinstance(configured, Mapping):
        configured = configured.get("action", "APPROVED")
    selected = str(configured).upper()
    aliases = {"APPROVE": "APPROVED", "DECLINE": "DECLINED", "ONLINE": "ONLINE_REQUIRED"}
    selected = aliases.get(selected, selected)
    if selected not in {"APPROVED", "DECLINED", "ONLINE_REQUIRED", "ACTION_REQUIRED"}:
        selected = "ACTION_REQUIRED"
    scenario_selected = scenario_actions.get(request.scenario, "APPROVED")
    if "ACTION_REQUIRED" in {selected, scenario_selected}:
        return "ACTION_REQUIRED"
    if "DECLINED" in {selected, scenario_selected}:
        return "DECLINED"
    if risk.online_required or "ONLINE_REQUIRED" in {selected, scenario_selected}:
        return "ONLINE_REQUIRED"
    return "APPROVED"


def combine_actions(terminal: str, card: str) -> FunctionalResult:
    if "DECLINED" in {terminal, card}:
        return FunctionalResult.DECLINED
    if "ACTION_REQUIRED" in {terminal, card}:
        return FunctionalResult.ACTION_REQUIRED
    if "ONLINE_REQUIRED" in {terminal, card}:
        return FunctionalResult.ONLINE_REQUIRED
    return FunctionalResult.APPROVED
