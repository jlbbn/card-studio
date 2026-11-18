from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_studio.applet import APPLET_AID_CANDIDATES, build_select_apdu


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Uso: generate_android_applet_aid.py <AppletAid.java>")
    target = Path(sys.argv[1])
    candidate = next(item for item in APPLET_AID_CANDIDATES if item.source == "Android Card Bridge")
    values = ", ".join(f"(byte) 0x{value:02X}" for value in build_select_apdu(candidate.aid))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "package mx.admin1.cardbridge;\n\n"
        "final class AppletAid {\n"
        f"    static final byte[] SELECT_AID = {{ {values} }};\n"
        "    private AppletAid() { }\n"
        "}\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
