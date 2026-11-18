from __future__ import annotations

import sys
from pathlib import Path

from card_studio.card_session import CardSession
from card_studio.coexistent_install import (
    build_read_only_args,
    create_pcsc_backend,
)
from card_studio.gppro_bibo import GPProBIBOBridge
from card_studio.model import CardError


GP_JAR = Path(
    "/home/admin1/payment/vendor/"
    "GlobalPlatformPro-next/tool/target/gp.jar"
)


def main() -> int:
    session = None

    try:
        backend = create_pcsc_backend("linux")

        print("backend_mode =", backend.mode)

        session = CardSession(backend)

        print("session_created = YES")

        session.open()

        print("session_open = YES")

        atr = backend.getATR()
        protocol = backend.getProtocol()
        reader = backend.getReader()

        print("reader =", reader)
        print("atr =", atr.hex().upper())
        print("protocol =", protocol)

        if protocol not in {"T=0", "T=1"}:
            print("FAIL: negotiated protocol unknown")
            return 2

        print("PASS: negotiated protocol =", protocol)
        print("PASS: ATR acquired")

        args = build_read_only_args()

        print("gppro_args =", args)

        if args != ["--list"]:
            print("FAIL: read-only contract changed")
            return 3

        bridge = GPProBIBOBridge(
            session,
            GP_JAR,
        )

        print("bridge_created = YES")

        rc = bridge.run_tool(args)

        print("gppro_exit_code =", rc)

        if rc != 0:
            print("FAIL: GPPro --list")
            return 4

        print("PASS: GPPro --list over existing CardSession")

        if not session.connected:
            print("FAIL: GPTool closed CardSession")
            return 5

        print("PASS: CardSession still owned by Card Studio")

        return 0

    except CardError as exc:
        print(
            "FAIL:",
            exc.code,
            "-",
            exc.message,
        )
        return 10

    except Exception as exc:
        print(
            "FAIL: unexpected:",
            type(exc).__name__,
            str(exc),
        )
        return 11

    finally:
        if session is not None:
            try:
                session.close()
                print("session_closed = YES")
            except Exception as exc:
                print(
                    "FAIL: session close:",
                    type(exc).__name__,
                    str(exc),
                )


if __name__ == "__main__":
    sys.exit(main())
