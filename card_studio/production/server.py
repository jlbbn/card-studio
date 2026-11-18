from __future__ import annotations

import base64
import os
from pathlib import Path

from .http import LedgerApplication, make_server
from .security import ServiceAuthenticator, ServiceCredentials
from .store import LedgerStore


def main() -> int:
    terminal_id = os.environ.get("CARD_P3_TERMINAL_ID", "sunmi-synthetic")
    encoded = os.environ.get("CARD_P3_SERVICE_SECRET", "")
    try:
        secret = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        credentials = ServiceCredentials(secret)
        store = LedgerStore(Path(os.environ.get("CARD_P3_DB_PATH", "/data/ledger.sqlite3")))
    except (ValueError, OSError):
        return 2
    application = LedgerApplication(store, ServiceAuthenticator({terminal_id: credentials}, store))
    server = make_server((os.environ.get("CARD_P3_BIND", "127.0.0.1"), 8080), application)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
