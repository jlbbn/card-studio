from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any

from .model import CardProfile
from .validation import ProfileValidationError


class CardProfileRegistry:
    def __init__(self, profiles: Iterable[CardProfile] = ()):
        self._profiles: dict[tuple[str, int], CardProfile] = {}
        self._lock = threading.RLock()
        for profile in profiles:
            self.register(profile)

    def register(self, profile: CardProfile) -> CardProfile:
        if not isinstance(profile, CardProfile):
            raise TypeError("Solo se pueden registrar instancias CardProfile.")
        key = (profile.profile_id, profile.profile_version)
        with self._lock:
            current = self._profiles.get(key)
            if current is not None:
                if current.profile_digest == profile.profile_digest:
                    return current
                raise ProfileValidationError(
                    "PROFILE_VERSION_CONFLICT",
                    "La misma versión de perfil no puede cambiar de contenido.",
                )
            self._profiles[key] = profile
            return profile

    def register_dict(self, value: Mapping[str, Any]) -> CardProfile:
        return self.register(CardProfile.from_dict(value))

    def get(self, profile_id: str, profile_version: int | None = None) -> CardProfile:
        with self._lock:
            if profile_version is not None:
                try:
                    return self._profiles[(profile_id, profile_version)]
                except KeyError as exc:
                    raise ProfileValidationError("PROFILE_NOT_FOUND", "Perfil no registrado.") from exc
            versions = [version for identifier, version in self._profiles if identifier == profile_id]
            if not versions:
                raise ProfileValidationError("PROFILE_NOT_FOUND", "Perfil no registrado.")
            return self._profiles[(profile_id, max(versions))]

    def list(self, scheme_family: str | None = None) -> tuple[CardProfile, ...]:
        with self._lock:
            values = list(self._profiles.values())
        if scheme_family is not None:
            selected = scheme_family.strip().lower()
            values = [profile for profile in values if profile.scheme_family == selected]
        return tuple(sorted(values, key=lambda item: (item.profile_id, item.profile_version)))
