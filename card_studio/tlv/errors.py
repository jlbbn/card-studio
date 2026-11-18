from __future__ import annotations


class TlvError(ValueError):
    code = "TLV_ERROR"

    def __init__(self, message: str, offset: int | None = None):
        super().__init__(message)
        self.message = message
        self.offset = offset


class TlvTruncatedError(TlvError):
    code = "TLV_TRUNCATED"


class TlvMalformedError(TlvError):
    code = "TLV_MALFORMED"


class TlvNonCanonicalError(TlvError):
    code = "TLV_NON_CANONICAL"


class TlvLimitError(TlvError):
    code = "TLV_LIMIT_EXCEEDED"
