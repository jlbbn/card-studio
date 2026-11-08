from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


class SpecificationAuthorityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SpecificationRequirement:
    requirement_id: str
    document_id: str
    document_version: str
    clause: str
    provenance: str = "EXTERNAL_UNVERIFIED"
    conflict_policy: str = "SPEC_WINS"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_.:-]{2,127}", self.requirement_id):
            raise SpecificationAuthorityError("INVALID_REQUIREMENT_ID", "Requirement ID inválido.")
        for name in ("document_id", "document_version", "clause"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 160:
                raise SpecificationAuthorityError("INVALID_REQUIREMENT_SOURCE", f"{name} es obligatorio.")
        if self.provenance != "EXTERNAL_UNVERIFIED":
            raise SpecificationAuthorityError(
                "EXTERNAL_AUTHORITY_UNVERIFIED",
                "La autoridad externa no puede auto-verificarse desde el laboratorio.",
            )
        if self.conflict_policy != "SPEC_WINS":
            raise SpecificationAuthorityError("SPEC_WINS_REQUIRED", "Todo conflicto debe resolverse SPEC_WINS.")

    def to_dict(self) -> dict[str, str]:
        return {
            "requirement_id": self.requirement_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "clause": self.clause,
            "provenance": self.provenance,
            "conflict_policy": self.conflict_policy,
            "certification": "NOT COMPLETED",
            "physical_validation": "NOT_EXECUTED",
        }


class SpecificationAuthorityRegistry:
    def __init__(self, requirements=()):
        self._requirements: dict[str, SpecificationRequirement] = {}
        for requirement in requirements:
            self.register(requirement)

    def register(self, requirement: SpecificationRequirement | Mapping[str, Any]) -> SpecificationRequirement:
        value = requirement if isinstance(requirement, SpecificationRequirement) else SpecificationRequirement(**requirement)
        if value.requirement_id in self._requirements:
            raise SpecificationAuthorityError("DUPLICATE_REQUIREMENT_ID", "Requirement ID duplicado.")
        self._requirements[value.requirement_id] = value
        return value

    def get(self, requirement_id: str) -> SpecificationRequirement:
        try:
            return self._requirements[requirement_id]
        except KeyError as error:
            raise SpecificationAuthorityError("REQUIREMENT_NOT_FOUND", "Requirement ID no registrado.") from error

    def resolve(self, requirement_id: str, implementation_value: Any, specification_value: Any) -> dict[str, Any]:
        requirement = self.get(requirement_id)
        return {
            "requirement_id": requirement_id,
            "resolution": "SPEC_WINS",
            "selected_value": specification_value,
            "implementation_value": implementation_value,
            "specification_value": specification_value,
            "conflict": implementation_value != specification_value,
            "provenance": "EXTERNAL_UNVERIFIED",
        }

    def bind_profile(self, requirement_id: str, profile: Any) -> dict[str, Any]:
        requirement = self.get(requirement_id)
        return {
            "requirement": requirement.to_dict(),
            "profile_ref": f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}",
            "profile_digest": profile.profile_digest,
            "profile_status": profile.status,
            "auto_approved": False,
            "authority": "EXTERNAL_UNVERIFIED",
            "mode": "LAB_ONLY",
            "certification": "NOT COMPLETED",
            "physical_validation": "NOT_EXECUTED",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "requirements": {
                key: self._requirements[key].to_dict() for key in sorted(self._requirements)
            },
            "authority": "EXTERNAL_UNVERIFIED",
            "conflict_policy": "SPEC_WINS",
            "certification": "NOT COMPLETED",
            "physical_validation": "NOT_EXECUTED",
        }
