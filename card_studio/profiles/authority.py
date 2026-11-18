from __future__ import annotations

import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping


class ConfigurationAuthorityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def expected_configuration_mappings(profile: Any) -> dict[str, str]:
    parameters = profile.scheme_parameters
    emv = parameters.get("emv_execution")
    if emv is None:
        if parameters.get("online_completion") is not None:
            raise ConfigurationAuthorityError(
                "EMV_CONFIG_REQUIRED",
                "online_completion con autoridad requiere emv_execution explícito.",
            )
        return {}
    if not isinstance(emv, Mapping):
        raise ConfigurationAuthorityError("INVALID_EMV_CONFIG", "emv_execution debe ser Mapping.")
    decision = emv.get("decision", {})
    if not isinstance(decision, Mapping):
        raise ConfigurationAuthorityError("INVALID_EMV_DECISION", "decision debe ser Mapping.")
    oda = emv.get("oda", {})
    if not isinstance(oda, Mapping):
        raise ConfigurationAuthorityError("INVALID_ODA_CONFIG", "oda debe ser Mapping.")
    values = {
        "PDOL": emv.get("pdol"),
        "CDOL1": emv.get("cdol1"),
        "TAC": decision.get("tac", {
            "denial": "0000000000",
            "online": "0000000000",
            "default": "0000000000",
        }),
        "IAC": decision.get("iac", "CARD_PROVIDED"),
        "ODA": {
            "allow_sda": oda.get("allow_sda", True) is True,
            "allow_dda": oda.get("allow_dda", True) is True,
            "allow_cda": oda.get("allow_cda", True) is True,
            "terminal_dynamic_authentication_supported": oda.get(
                "terminal_dynamic_authentication_supported", True
            ) is True,
            "material_status": oda.get("material_status", "SYNTHETIC"),
        },
        "CAPK": {
            "provider": "SYNTHETIC",
            "material_status": oda.get("material_status", "SYNTHETIC"),
            "physical_validation": "NOT_EXECUTED",
        },
    }
    if parameters.get("online_completion") is not None:
        online = parameters["online_completion"]
        if not isinstance(online, Mapping):
            raise ConfigurationAuthorityError("INVALID_ONLINE_CONFIG", "online_completion debe ser Mapping.")
        values["CDOL2"] = online.get("cdol2", [])
    return {name: _canonical_digest(value) for name, value in values.items()}


def validate_configuration_authority(profile: Any, kernel_ref: str | None = None) -> None:
    raw = profile.scheme_parameters.get("configuration_authority")
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise ConfigurationAuthorityError("INVALID_CONFIG_AUTHORITY", "configuration_authority debe ser Mapping.")
    if raw.get("schema_version") != 1:
        raise ConfigurationAuthorityError("INVALID_CONFIG_AUTHORITY_SCHEMA", "La autoridad de configuración debe usar schema_version=1.")
    version = raw.get("configuration_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ConfigurationAuthorityError("INVALID_CONFIG_VERSION", "configuration_version debe ser un entero positivo.")
    authority_ref = raw.get("authority_ref")
    if not isinstance(authority_ref, str) or not re.fullmatch(r"(?:lab|simulated)://[A-Za-z0-9._/-]{1,120}", authority_ref):
        raise ConfigurationAuthorityError(
            "INVALID_CONFIG_AUTHORITY_REF",
            "authority_ref debe ser una referencia lab:// o simulated:// sin credenciales.",
        )
    if raw.get("mode") not in {"LAB_ONLY", "SIMULATED"}:
        raise ConfigurationAuthorityError("INVALID_CONFIG_AUTHORITY_MODE", "La autoridad debe ser LAB_ONLY o SIMULATED.")
    if raw.get("scheme_family") != profile.scheme_family:
        raise ConfigurationAuthorityError("CONFIG_SCHEME_MISMATCH", "La autoridad pertenece a otro scheme_family.")
    required_kernel = raw.get("kernel_ref")
    if required_kernel != profile.kernel_requirements.get("kernel_ref"):
        raise ConfigurationAuthorityError("CONFIG_KERNEL_MISMATCH", "La autoridad no coincide con el kernel del perfil.")
    if kernel_ref is not None and required_kernel != kernel_ref:
        raise ConfigurationAuthorityError("CONFIG_KERNEL_MISMATCH", "La autoridad exige otro kernel.")
    mappings = raw.get("mappings")
    if not isinstance(mappings, Mapping):
        raise ConfigurationAuthorityError("CONFIG_MAPPINGS_REQUIRED", "La autoridad requiere mappings de configuración.")
    expected = expected_configuration_mappings(profile)
    if set(mappings) != set(expected):
        raise ConfigurationAuthorityError(
            "CONFIG_MAPPING_SET_MISMATCH",
            "Los mappings deben cubrir exactamente PDOL/CDOL/TAC/IAC/ODA/CAPK y los DOL online aplicables.",
        )
    for name, digest in expected.items():
        if mappings.get(name) != digest:
            raise ConfigurationAuthorityError(
                "CONFIG_MAPPING_DIGEST_MISMATCH",
                f"El digest de autoridad no coincide para {name}.",
            )


def configuration_provenance(profile: Any, kernel_ref: str) -> dict[str, Any] | None:
    validate_configuration_authority(profile, kernel_ref)
    raw = profile.scheme_parameters.get("configuration_authority")
    if raw is None:
        return None
    mappings = MappingProxyType(dict(raw["mappings"]))
    return {
        "schema_version": raw["schema_version"],
        "configuration_version": raw["configuration_version"],
        "authority_ref": raw["authority_ref"],
        "mode": "LAB_ONLY",
        "scheme_family": raw["scheme_family"],
        "kernel_ref": raw["kernel_ref"],
        "profile_digest": profile.profile_digest,
        "mapping_sha256": dict(sorted(mappings.items())),
        "physical_validation": "NOT_EXECUTED",
    }
