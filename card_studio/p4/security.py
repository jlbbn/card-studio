from __future__ import annotations

from typing import Mapping

from card_studio.production.security import AuthenticationError, ServiceAuthenticator


class RotatingServiceAuthenticator(ServiceAuthenticator):
    def __init__(self, credentials, previous_credentials, nonce_store, **kwargs):
        super().__init__(credentials, nonce_store, **kwargs)
        self._previous = ServiceAuthenticator(previous_credentials, nonce_store, **kwargs) if previous_credentials else None

    def authenticate(self, method: str, path: str, headers: Mapping[str, str], body: bytes, *, operation_type: str) -> str:
        try:
            return super().authenticate(method, path, headers, body, operation_type=operation_type)
        except AuthenticationError as error:
            if error.code != "INVALID_SIGNATURE" or self._previous is None:
                raise
            return self._previous.authenticate(method, path, headers, body, operation_type=operation_type)


def validate_secret_metadata(value: Mapping[str, object]) -> bool:
    allowed = {"name", "rotation_enabled", "last_changed", "kms_key_reference", "environment_mapping"}
    forbidden = {"secret_string", "password", "token", "key_material", "credential_value", "secret"}
    keys = {str(key).casefold() for key in value}
    return bool(value.get("name") and keys <= allowed and not keys & forbidden)
