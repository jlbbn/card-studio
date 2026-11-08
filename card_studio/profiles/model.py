from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from card_studio.interfaces.base import InterfaceKind


class ProfileStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED_FOR_SIMULATION = "APPROVED_FOR_SIMULATION"
    RETIRED = "RETIRED"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class CardApplication:
    aid: str
    application_label: str
    priority: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "aid", self.aid.strip().upper())
        object.__setattr__(self, "application_label", self.application_label.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "aid": self.aid,
            "application_label": self.application_label,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class CardProfile:
    schema_version: int
    profile_id: str
    profile_version: int
    profile_digest: str
    scheme_family: str
    display_name: str
    applications: tuple[CardApplication, ...]
    interfaces: tuple[str, ...]
    cvm_capabilities: tuple[str, ...]
    kernel_requirements: Mapping[str, Any]
    scheme_parameters: Mapping[str, Any]
    test_credential_ref: str | None
    synthetic_pan_profile: Mapping[str, Any] | None
    tokenization_policy: Mapping[str, Any]
    status: str

    def __post_init__(self) -> None:
        applications = tuple(
            item if isinstance(item, CardApplication) else CardApplication(**item)
            for item in self.applications
        )
        object.__setattr__(self, "applications", applications)
        object.__setattr__(self, "interfaces", tuple(str(item).upper() for item in self.interfaces))
        object.__setattr__(self, "cvm_capabilities", tuple(str(item).upper() for item in self.cvm_capabilities))
        object.__setattr__(self, "scheme_family", self.scheme_family.strip().lower())
        object.__setattr__(self, "display_name", self.display_name.strip())
        object.__setattr__(self, "status", str(self.status).upper())
        for field in ["kernel_requirements", "scheme_parameters", "synthetic_pan_profile", "tokenization_policy"]:
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _freeze(value))

        from .validation import validate_profile

        validate_profile(self)
        computed = self.computed_digest()
        if not self.profile_digest:
            object.__setattr__(self, "profile_digest", computed)
        elif self.profile_digest.lower() != computed:
            raise ValueError("profile_digest no coincide con el contenido del perfil.")

    def _to_dict(self, include_digest: bool) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "scheme_family": self.scheme_family,
            "display_name": self.display_name,
            "applications": [item.to_dict() for item in self.applications],
            "interfaces": list(self.interfaces),
            "cvm_capabilities": list(self.cvm_capabilities),
            "kernel_requirements": _thaw(self.kernel_requirements),
            "scheme_parameters": _thaw(self.scheme_parameters),
            "test_credential_ref": self.test_credential_ref,
            "synthetic_pan_profile": _thaw(self.synthetic_pan_profile),
            "tokenization_policy": _thaw(self.tokenization_policy),
            "status": self.status,
        }
        if include_digest:
            value["profile_digest"] = self.profile_digest
        return value

    def computed_digest(self) -> str:
        canonical = json.dumps(
            self._to_dict(include_digest=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(include_digest=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CardProfile":
        return cls(
            schema_version=value["schema_version"],
            profile_id=value["profile_id"],
            profile_version=value["profile_version"],
            profile_digest=str(value.get("profile_digest", "")),
            scheme_family=value["scheme_family"],
            display_name=value["display_name"],
            applications=tuple(value["applications"]),
            interfaces=tuple(value["interfaces"]),
            cvm_capabilities=tuple(value.get("cvm_capabilities", ())),
            kernel_requirements=value.get("kernel_requirements", {}),
            scheme_parameters=value.get("scheme_parameters", {}),
            test_credential_ref=value.get("test_credential_ref"),
            synthetic_pan_profile=value.get("synthetic_pan_profile"),
            tokenization_policy=value.get("tokenization_policy", {}),
            status=value["status"],
        )

    def assert_simulation_enabled(self) -> None:
        if self.status != ProfileStatus.APPROVED_FOR_SIMULATION.value:
            from .validation import ProfileValidationError

            raise ProfileValidationError(
                "PROFILE_NOT_EXECUTABLE",
                "Solo un perfil APPROVED_FOR_SIMULATION puede ejecutar simulaciones.",
            )

    def supports_interface(self, interface: InterfaceKind | str) -> bool:
        selected = interface.value if isinstance(interface, InterfaceKind) else interface.upper()
        return selected in self.interfaces
