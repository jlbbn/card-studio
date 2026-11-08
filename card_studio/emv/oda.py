from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from .aip import AipCapability, ApplicationInterchangeProfile
from .cryptogram import AcEvidencePolicy
from .data import EmvDataStore
from .status import Tsi, TsiFlag, Tvr, TvrFlag


class OdaError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class OdaMethod(str, Enum):
    SDA = "SDA"
    DDA = "DDA"
    CDA = "CDA"


class OdaMaterialStatus(str, Enum):
    SYNTHETIC = "SYNTHETIC"
    TEST_AUTHORIZED = "TEST_AUTHORIZED"
    UNKNOWN = "UNKNOWN"


class OdaStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class OdaProfile:
    allow_sda: bool = True
    allow_dda: bool = True
    allow_cda: bool = True
    terminal_dynamic_authentication_supported: bool = True
    material_status: OdaMaterialStatus = OdaMaterialStatus.SYNTHETIC
    required_material_tags: Mapping[OdaMethod, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "material_status", OdaMaterialStatus(self.material_status))
        required = {OdaMethod(method): tuple(tags) for method, tags in self.required_material_tags.items()}
        object.__setattr__(self, "required_material_tags", required)
        if self.material_status == OdaMaterialStatus.SYNTHETIC and any(required.values()):
            raise OdaError("INVALID_ODA_PROFILE", "Material TEST no puede marcarse SYNTHETIC sin provider autorizado.")
        if self.material_status == OdaMaterialStatus.UNKNOWN and any(required.values()):
            raise OdaError("UNKNOWN_ODA_MATERIAL", "Material UNKNOWN no puede entrar al flujo.")


@dataclass(frozen=True)
class OdaSelection:
    method: OdaMethod | None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class OdaRequest:
    method: OdaMethod
    store: EmvDataStore
    aip: ApplicationInterchangeProfile
    operation_id: str
    profile_ref: str


@dataclass(frozen=True)
class OdaProviderResult:
    success: bool
    reason: str
    evidence_token: str


class OdaProvider(Protocol):
    def authenticate(self, request: OdaRequest) -> OdaProviderResult:
        ...


class OdaProviderError(ValueError):
    pass


@dataclass(frozen=True)
class SyntheticOdaProvider:
    """Proveedor determinista NON-CRYPTOGRAPHIC para pruebas LAB_ONLY."""

    fail_methods: frozenset[OdaMethod] = frozenset()
    error_methods: frozenset[OdaMethod] = frozenset()

    def authenticate(self, request: OdaRequest) -> OdaProviderResult:
        method = OdaMethod(request.method)
        if method in self.error_methods:
            raise OdaProviderError("SYNTHETIC_PROVIDER_ERROR")
        success = method not in self.fail_methods
        seed = f"NON-CRYPTOGRAPHIC|{request.operation_id}|{request.profile_ref}|{method.value}".encode("utf-8")
        token = hashlib.sha256(seed).hexdigest()
        return OdaProviderResult(success, "SYNTHETIC_SUCCESS" if success else "SYNTHETIC_FAILURE", token)


@dataclass(frozen=True)
class OdaBoundaryResult:
    status: OdaStatus
    selection: OdaSelection
    reason: str | None
    tvr: Tvr
    tsi: Tsi
    evidence_token: str | None = None

    @property
    def technical_failure(self) -> bool:
        return self.status == OdaStatus.BLOCKED

    def to_evidence(self, token_policy: AcEvidencePolicy = AcEvidencePolicy.SUPPRESS) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "status": self.status.value,
            "method": self.selection.method.value if self.selection.method else None,
            "reason": self.reason or self.selection.blocked_reason,
            "technical_failure": self.technical_failure,
            "provider": "SYNTHETIC_NON_CRYPTOGRAPHIC",
            "tvr": self.tvr.to_evidence(),
            "tsi": self.tsi.to_evidence(),
            "mode": "LAB_ONLY",
        }
        if self.evidence_token and AcEvidencePolicy(token_policy) == AcEvidencePolicy.DIGEST:
            evidence["synthetic_evidence_digest"] = self.evidence_token
        return evidence


def select_oda_method(aip: ApplicationInterchangeProfile, profile: OdaProfile) -> OdaSelection:
    if not aip.oda_required:
        return OdaSelection(None)
    if profile.material_status == OdaMaterialStatus.UNKNOWN:
        return OdaSelection(None, "UNKNOWN_ODA_MATERIAL")
    candidates = (
        (OdaMethod.CDA, AipCapability.CDA_SUPPORTED, profile.allow_cda, True),
        (OdaMethod.DDA, AipCapability.DDA_SUPPORTED, profile.allow_dda, True),
        (OdaMethod.SDA, AipCapability.SDA_SUPPORTED, profile.allow_sda, False),
    )
    for method, capability, allowed, dynamic in candidates:
        if not aip.supports(capability) or not allowed:
            continue
        if dynamic and not profile.terminal_dynamic_authentication_supported:
            continue
        return OdaSelection(method)
    return OdaSelection(None, "ODA_NOT_PERFORMED_REQUIRED")


def _blocked(
    reason: str,
    selection: OdaSelection,
    tvr: Tvr | None,
    tsi: Tsi | None,
) -> OdaBoundaryResult:
    next_tvr = Tvr.parse(tvr.to_bytes()) if tvr is not None else Tvr.cleared()
    next_tsi = Tsi.parse(tsi.to_bytes()) if tsi is not None else Tsi.cleared()
    next_tvr.set(TvrFlag.OFFLINE_DATA_AUTH_NOT_PERFORMED)
    return OdaBoundaryResult(OdaStatus.BLOCKED, selection, reason, next_tvr, next_tsi)


def evaluate_oda(
    request_store: EmvDataStore,
    aip: ApplicationInterchangeProfile,
    profile: OdaProfile,
    provider: OdaProvider,
    operation_id: str,
    profile_ref: str,
    tvr: Tvr | None = None,
    tsi: Tsi | None = None,
) -> OdaBoundaryResult:
    if not operation_id or not profile_ref:
        raise OdaError("INVALID_ODA_CONTEXT", "ODA requiere operation_id y profile_ref.")
    selection = select_oda_method(aip, profile)
    if selection.method is None:
        if selection.blocked_reason:
            return _blocked(selection.blocked_reason, selection, tvr, tsi)
        return OdaBoundaryResult(
            OdaStatus.NOT_REQUIRED,
            selection,
            None,
            Tvr.parse(tvr.to_bytes()) if tvr is not None else Tvr.cleared(),
            Tsi.parse(tsi.to_bytes()) if tsi is not None else Tsi.cleared(),
        )
    if profile.material_status == OdaMaterialStatus.TEST_AUTHORIZED:
        for tag in profile.required_material_tags.get(selection.method, ()):
            try:
                request_store.require(tag)
            except ValueError:
                return _blocked("MISSING_ODA_MATERIAL", selection, tvr, tsi)
    try:
        outcome = provider.authenticate(OdaRequest(selection.method, request_store, aip, operation_id, profile_ref))
    except Exception:
        return _blocked("ODA_PROVIDER_ERROR", selection, tvr, tsi)
    next_tvr = Tvr.parse(tvr.to_bytes()) if tvr is not None else Tvr.cleared()
    next_tsi = Tsi.parse(tsi.to_bytes()) if tsi is not None else Tsi.cleared()
    next_tsi.mark_processed(TsiFlag.OFFLINE_DATA_AUTHENTICATION_PERFORMED)
    if outcome.success:
        status = OdaStatus.SUCCESS
    else:
        status = OdaStatus.FAILED
        failure_flag = {
            OdaMethod.SDA: TvrFlag.SDA_FAILED,
            OdaMethod.DDA: TvrFlag.DDA_FAILED,
            OdaMethod.CDA: TvrFlag.CDA_FAILED,
        }[selection.method]
        next_tvr.set(failure_flag)
    return OdaBoundaryResult(status, selection, outcome.reason, next_tvr, next_tsi, outcome.evidence_token)
