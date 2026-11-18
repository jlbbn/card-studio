from __future__ import annotations

import re
from typing import Any, Mapping

from card_studio.interfaces.base import InterfaceKind


class ProfileValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SYNTHETIC_TEST_PANS = frozenset({
    "4111111111111111",
    "5555555555554444",
    "378282246310005",
    "6011111111111117",
    "3530111333300000",
    "6200000000000005",
})
FORBIDDEN_FIELDS = frozenset({
    "pin",
    "cvv",
    "cvc",
    "track1",
    "track2",
    "private_key",
    "issuer_key",
    "production_key",
    "master_key",
    "session_key",
    "secret",
    "password",
    "api_key",
})
ALLOWED_CVM = frozenset({
    "NONE",
    "NO_CVM",
    "SIGNATURE",
    "SIGNATURE_SIMULATED",
    "ONLINE_PIN_REQUIRED",
    "OFFLINE_PIN_REQUIRED",
    "CDCVM",
    "CDCVM_SIMULATED",
    "CVM_FAILED",
    "NOT_SUPPORTED",
})


def validate_aid(aid: str) -> str:
    normalized = aid.strip().upper()
    if not re.fullmatch(r"[0-9A-F]+", normalized) or len(normalized) % 2:
        raise ProfileValidationError("INVALID_AID", "El AID debe contener bytes hexadecimales completos.")
    if not 5 <= len(normalized) // 2 <= 16:
        raise ProfileValidationError("INVALID_AID", "El AID debe medir entre 5 y 16 bytes.")
    return normalized


def _normalized_key(value: Any) -> str:
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def _reject_forbidden(value: Any, allow_synthetic_pan: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in FORBIDDEN_FIELDS or (normalized == "pan" and not allow_synthetic_pan):
                raise ProfileValidationError("SENSITIVE_PROFILE_DATA", f"Campo prohibido en perfil: {key}.")
            _reject_forbidden(item, allow_synthetic_pan=False)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden(item, allow_synthetic_pan=False)


def _validate_synthetic_pan(value: Mapping[str, Any] | None) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or value.get("synthetic") is not True:
        raise ProfileValidationError(
            "INVALID_SYNTHETIC_PAN",
            "synthetic_pan_profile debe declarar synthetic=true.",
        )
    if "pan" in value and value["pan"] not in SYNTHETIC_TEST_PANS:
        raise ProfileValidationError(
            "NON_SYNTHETIC_PAN",
            "El PAN no pertenece a los vectores sintéticos permitidos.",
        )
    _reject_forbidden(value, allow_synthetic_pan=True)


def validate_profile(profile: Any) -> None:
    if profile.schema_version != 1:
        raise ProfileValidationError("INVALID_SCHEMA_VERSION", "schema_version debe ser 1.")
    if not re.fullmatch(r"[a-z][a-z0-9._-]{2,63}", profile.profile_id):
        raise ProfileValidationError("INVALID_PROFILE_ID", "profile_id no tiene un formato válido.")
    if not isinstance(profile.profile_version, int) or isinstance(profile.profile_version, bool) or profile.profile_version < 1:
        raise ProfileValidationError("INVALID_PROFILE_VERSION", "profile_version debe ser un entero positivo.")
    if not re.fullmatch(r"[a-z][a-z0-9._-]{1,31}", profile.scheme_family):
        raise ProfileValidationError("INVALID_SCHEME", "scheme_family no tiene un formato válido.")
    if not profile.display_name:
        raise ProfileValidationError("INVALID_DISPLAY_NAME", "display_name no puede estar vacío.")
    if not profile.applications:
        raise ProfileValidationError("APPLICATIONS_REQUIRED", "El perfil necesita al menos una aplicación.")

    seen_aids: set[str] = set()
    for application in profile.applications:
        aid = validate_aid(application.aid)
        if aid in seen_aids:
            raise ProfileValidationError("DUPLICATE_AID", f"AID duplicado: {aid}.")
        seen_aids.add(aid)
        if not application.application_label:
            raise ProfileValidationError("INVALID_APPLICATION", "application_label no puede estar vacío.")
        if not isinstance(application.priority, int) or isinstance(application.priority, bool) or application.priority < 0:
            raise ProfileValidationError("INVALID_APPLICATION", "priority debe ser un entero no negativo.")

    supported_interfaces = {item.value for item in InterfaceKind}
    if not profile.interfaces:
        raise ProfileValidationError("INTERFACES_REQUIRED", "El perfil necesita al menos una interfaz.")
    if len(set(profile.interfaces)) != len(profile.interfaces):
        raise ProfileValidationError("DUPLICATE_INTERFACE", "El perfil contiene interfaces duplicadas.")
    for interface in profile.interfaces:
        if interface not in supported_interfaces:
            raise ProfileValidationError("UNSUPPORTED_INTERFACE", f"Interfaz no soportada: {interface}.")

    for capability in profile.cvm_capabilities:
        if capability not in ALLOWED_CVM:
            raise ProfileValidationError("UNSUPPORTED_CVM", f"CVM no permitido en esta fase: {capability}.")
    if profile.status not in {"DRAFT", "APPROVED_FOR_SIMULATION", "RETIRED"}:
        raise ProfileValidationError("INVALID_STATUS", "Estado de perfil no reconocido.")
    if profile.status == "APPROVED_FOR_SIMULATION" and profile.kernel_requirements.get("simulation") is not True:
        raise ProfileValidationError(
            "SIMULATION_REQUIRED",
            "Un perfil ejecutable debe declarar kernel_requirements.simulation=true.",
        )
    if profile.tokenization_policy.get("enabled") is True and profile.tokenization_policy.get("simulation") is not True:
        raise ProfileValidationError(
            "SIMULATION_REQUIRED",
            "La tokenización de esta fase debe ser exclusivamente simulada.",
        )
    if profile.test_credential_ref is not None and not profile.test_credential_ref.startswith("test://"):
        raise ProfileValidationError(
            "INVALID_TEST_CREDENTIAL",
            "test_credential_ref debe usar el esquema test:// y nunca contener material productivo.",
        )

    _reject_forbidden(profile.kernel_requirements)
    _reject_forbidden(profile.scheme_parameters)
    _reject_forbidden(profile.tokenization_policy)
    _validate_synthetic_pan(profile.synthetic_pan_profile)

    from .authority import validate_configuration_authority

    validate_configuration_authority(profile)
