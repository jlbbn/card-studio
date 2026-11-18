from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .card_session import CardSession
from .gppro_bibo import GPProBIBOBridge
from .hardware_validation import write_evidence
from .model import CardError
from .pcsc import create_pcsc_backend
from .write_exercise import PRESERVED_APPLET_AID

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAP = ROOT / "javacard-demo/build-coexistent/payment-demo.cap"
DEFAULT_GP_JAR = ROOT / "vendor/GlobalPlatformPro-next/tool/target/gp.jar"
PACKAGE_AID = "D4534C504D45"
APPLET_AID = "D4534C504D4501"
PRESERVED_PACKAGE_AID = "D4534C504D44"
CAP_SHA256 = "8a8d0e2acab8c12644c8ccf7fd291ca94f5c9f3fcd5c6f466fbc0f6933c244d2"
LFDB_SHA256 = "18d3ef9fe09593e1d4ccbf76184c413ec4da3709c1ad2e155c5024ee63775f26"
CONFIRMATION = "INSTALAR_CAP_COEXISTENTE_SIN_BORRAR"
GP_DEFAULT_MASTER_KEY_HEX = "404142434445464748494A4B4C4D4E4F"
FORBIDDEN_OPTIONS = {"--delete", "--uninstall", "--force", "--default", "--lock", "--put-key", "--replace-key"}

READ_ONLY_FORBIDDEN_OPTIONS = {
    "--install",
    "--load",
    "--delete",
    "--uninstall",
    "--put-key",
    "--replace-key",
    "--lock",
    "--force",
}


def build_read_only_args() -> list[str]:
    args = ["--list"]

    if READ_ONLY_FORBIDDEN_OPTIONS.intersection(args):
        raise CardError(
            "GPPRO_READ_ONLY_CONTRACT_VIOLATION",
            "El preflight read-only contiene una operación mutante.",
        )

    return args


def build_secure_channel_preflight_args() -> list[str]:
    args = [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--list",
    ]

    if READ_ONLY_FORBIDDEN_OPTIONS.intersection(args):
        raise CardError(
            "GPPRO_SECURE_PREFLIGHT_CONTRACT_VIOLATION",
            "El preflight Secure Channel contiene una operación mutante.",
        )

    return args



def _normalized_aid(value: Any) -> str:
    aid = str(value).strip().upper()
    if not re.fullmatch(r"[0-9A-F]{10,32}", aid) or len(aid) % 2:
        raise CardError("INVALID_REGISTRY_AID", "El registro contiene un AID inválido.")
    return aid


def parse_registry_text(text: str) -> dict[str, Any]:
    entries: dict[tuple[str, str], dict[str, str]] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(PKG|APP):\s+([0-9A-Fa-f]{10,32})\b", line)
        if match:
            kind = "PACKAGE" if match.group(1) == "PKG" else "APPLET"
            aid = _normalized_aid(match.group(2))
            entries[(kind, aid)] = {"type": kind, "aid": aid}
    if not entries:
        raise CardError("EMPTY_REGISTRY", "La salida no contiene paquetes ni applets GlobalPlatform reconocibles.")
    return {"schema": "globalplatform-registry-evidence/v1", "mode": "UNCLASSIFIED_TEXT", "entries": list(entries.values())}


def load_registry(path: Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise CardError("REGISTRY_NOT_READABLE", "No se pudo leer la evidencia del registro.") from error
    if not text.lstrip().startswith("{"):
        return parse_registry_text(text)
    try:
        document = json.loads(text)
        entries = document["entries"]
        mode = document["mode"]
        if not isinstance(entries, list) or not entries:
            raise ValueError
        normalized = {
            (str(item["type"]).upper(), _normalized_aid(item["aid"])): {
                "type": str(item["type"]).upper(),
                "aid": _normalized_aid(item["aid"]),
            }
            for item in entries
        }
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise CardError("INVALID_REGISTRY_EVIDENCE", "La evidencia JSON del registro es inválida.") from error
    if any(kind not in {"PACKAGE", "APPLET"} for kind, _ in normalized):
        raise CardError("INVALID_REGISTRY_EVIDENCE", "La evidencia contiene tipos de entrada no admitidos.")
    if mode not in {"SIMULATED", "LIVE_READ_ONLY"}:
        raise CardError("INVALID_REGISTRY_MODE", "La evidencia debe declarar SIMULATED o LIVE_READ_ONLY.")
    if mode == "LIVE_READ_ONLY" and not document.get("captured_at"):
        raise CardError("REGISTRY_TIMESTAMP_REQUIRED", "La evidencia física debe incluir captured_at.")
    return {"schema": "globalplatform-registry-evidence/v1", "mode": mode, "entries": list(normalized.values())}


def validate_registry(
    registry: dict[str, Any],
    *,
    require_live: bool = False,
) -> dict[str, Any]:
    if (
        require_live
        and registry["mode"] != "LIVE_READ_ONLY"
    ):
        raise CardError(
            "LIVE_REGISTRY_REQUIRED",
            "La instalación física requiere evidencia "
            "LIVE_READ_ONLY con fecha.",
        )

    pairs = {
        (
            item["type"],
            item["aid"],
        )
        for item in registry["entries"]
    }

    target_package_present = (
        "PACKAGE",
        PACKAGE_AID,
    ) in pairs

    target_applet_present = (
        "APPLET",
        APPLET_AID,
    ) in pairs

    if (
        target_package_present
        or target_applet_present
    ):
        raise CardError(
            "COEXISTENT_AID_COLLISION",
            "El paquete o applet objetivo ya aparece "
            "en el registro; no se instalará.",
        )

    preserved_package_present = (
        "PACKAGE",
        PRESERVED_PACKAGE_AID,
    ) in pairs

    preserved_applet_present = (
        "APPLET",
        PRESERVED_APPLET_AID,
    ) in pairs

    # Partial legacy state is suspicious and remains fail-closed.
    if (
        preserved_package_present
        != preserved_applet_present
    ):
        raise CardError(
            "PRESERVED_AID_MISSING",
            "El estado preservado está incompleto; "
            "no se instalará.",
        )

    if (
        preserved_package_present
        and preserved_applet_present
    ):
        card_profile = "COEXISTENT"

    else:
        # Both legacy AIDs absent is the valid state of a new card.
        card_profile = "NEW_CARD"

    return {
        "registry_mode":
            registry["mode"],

        "card_profile":
            card_profile,

        "preserved_package_present":
            preserved_package_present,

        "preserved_applet_present":
            preserved_applet_present,

        "coexistent_package_free":
            True,

        "coexistent_applet_free":
            True,

        "result":
            "PASS",
    }



def inspect_cap(cap_path: Path, gp_jar: Path) -> dict[str, Any]:
    try:
        raw = Path(cap_path).read_bytes()
    except OSError as error:
        raise CardError("CAP_NOT_READABLE", "No se pudo leer el CAP coexistible.") from error
    cap_sha256 = hashlib.sha256(raw).hexdigest()
    if cap_sha256 != CAP_SHA256:
        raise CardError("CAP_HASH_MISMATCH", "El SHA-256 del CAP no coincide con la variante aprobada.")
    command = ["java", "-jar", str(gp_jar), "--cap", str(cap_path)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CardError("CAP_PARSE_FAILED", "GlobalPlatformPro no pudo analizar el CAP.") from error
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise CardError("CAP_PARSE_FAILED", "GlobalPlatformPro rechazó el CAP coexistible.")
    package = re.search(r"^Package:\s+\S+\s+([0-9A-Fa-f]{10,32})\s+v", output, re.MULTILINE)
    applet = re.search(r"^Applet:\s+\S+\s+([0-9A-Fa-f]{10,32})\s*$", output, re.MULTILINE)
    lfdb = re.search(r"^SHA-256\s+([0-9a-f]{64})\s*$", output, re.MULTILINE)
    if not package or _normalized_aid(package.group(1)) != PACKAGE_AID:
        raise CardError("CAP_PACKAGE_AID_MISMATCH", "El paquete dentro del CAP no coincide con el aprobado.")
    if not applet or _normalized_aid(applet.group(1)) != APPLET_AID:
        raise CardError("CAP_APPLET_AID_MISMATCH", "El applet dentro del CAP no coincide con el aprobado.")
    if not lfdb or lfdb.group(1) != LFDB_SHA256:
        raise CardError("CAP_LFDB_MISMATCH", "El LFDB del CAP no coincide con la variante aprobada.")
    return {
        "cap_path": str(cap_path),
        "cap_bytes": len(raw),
        "cap_sha256": cap_sha256,
        "load_file_data_hash_sha256": lfdb.group(1).upper(),
        "package_aid": PACKAGE_AID,
        "applet_aid": APPLET_AID,
        "capfile_parse": "PASS",
    }


def prepare_install(cap_path: Path, gp_jar: Path, registry_path: Path, *, require_live: bool = False) -> dict[str, Any]:
    registry = load_registry(registry_path)
    validation = validate_registry(registry, require_live=require_live)
    cap = inspect_cap(cap_path, gp_jar)
    return {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "PHYSICAL" if require_live else "SIMULATED",
        "cap": cap,
        "registry": validation,
        "physical_install": "NOT_EXECUTED",
        "physical_validation": "PENDING" if require_live else "NOT_EXECUTED",
        "post_install_registry": "REQUIRED_AFTER_PHYSICAL_INSTALL",
        "result": "READY_FOR_AUTHORIZED_PHYSICAL_INSTALL" if require_live else "SIMULATED_READY",
        "errors": [],
    }


def validate_install_args(
    args: list[str] | tuple[str, ...],
    cap_path: Path,
) -> list[str]:
    normalized = [
        str(item)
        for item in args
    ]

    expected = [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--install", str(cap_path),
    ]

    if normalized != expected:
        raise CardError(
            "INSTALL_ARGUMENT_CONTRACT_VIOLATION",
            "Los argumentos de instalación no coinciden con "
            "el contrato autorizado.",
        )

    destructive = {
        "--delete",
        "--uninstall",
        "--force",
        "--default",
        "--lock",
        "--put-key",
        "--replace-key",
        "--load",
    }

    transport = {
        "--reader",
        "-r",
        "--pcsc-exclusive",
        "-X",
    }

    key_cli = {
        "--key",
        "--key-enc",
        "--key-mac",
        "--key-dek",
    }

    for token in normalized:
        if token in destructive:
            raise CardError(
                "FORBIDDEN_INSTALL_OPTION",
                "La instalación contiene una operación destructiva "
                "o no autorizada.",
            )

        if (
            token in transport
            or token.startswith("--reader=")
        ):
            raise CardError(
                "GPPRO_EXTERNAL_READER_FORBIDDEN",
                "La instalación debe usar exclusivamente "
                "la CardSession/BIBO existente.",
            )

        if (
            token in key_cli
            or token.startswith("--key=")
            or token.startswith("--key-enc=")
            or token.startswith("--key-mac=")
            or token.startswith("--key-dek=")
        ):
            raise CardError(
                "GPPRO_KEY_ARGUMENT_FORBIDDEN",
                "Las llaves GP solo pueden provenir de GP_KEY.",
            )

    return normalized


def build_install_args(cap_path: Path) -> list[str]:
    """
    Arguments passed directly to GPTool.run(BIBO, String[]).

    Deliberately contains no java executable, gp.jar, reader selection,
    PC/SC option or secret key.
    """

    args = [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--install", str(cap_path),
    ]

    if FORBIDDEN_OPTIONS.intersection(args):
        raise CardError(
            "FORBIDDEN_INSTALL_OPTION",
            "Los argumentos contienen una opción prohibida.",
        )

    forbidden_transport = {
        "--reader",
        "-r",
        "--pcsc-exclusive",
        "-X",
    }

    if forbidden_transport.intersection(args):
        raise CardError(
            "GPPRO_EXTERNAL_READER_FORBIDDEN",
            "La instalación debe usar exclusivamente el BIBO existente.",
        )

    return validate_install_args(
        args,
        cap_path,
    )


def redact_output(text: str | bytes, key: str) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    redacted = text
    variants = {key, key.upper(), key.lower()}
    compact = re.sub(r"[^0-9A-Fa-f]", "", key)
    if compact:
        variants.add(compact)
        variants.add(compact.upper())
        variants.add(compact.lower())
        variants.add(" ".join(compact[index:index + 2] for index in range(0, len(compact), 2)))
        variants.add(":".join(compact[index:index + 2] for index in range(0, len(compact), 2)))
    for variant in sorted((item for item in variants if item), key=len, reverse=True):
        redacted = redacted.replace(variant, "[REDACTED]")
    return re.sub(r"(?im)^(\s*(?:GP_KEY|key)\s*[:=]\s*).*$", r"\1[REDACTED]", redacted)


def validate_gp_key_environment() -> bytes:
    key = os.environ.get(
        "GP_KEY",
        "",
    ).strip()

    if not key:
        raise CardError(
            "GP_KEY_REQUIRED",
            "Define GP_KEY en el entorno sin incluirla en "
            "argumentos ni evidencias.",
        )

    if key.lower().startswith("default"):
        raise CardError(
            "GP_DEFAULT_KEY_FORBIDDEN",
            "Card Studio no permite usar la llave default de "
            "GlobalPlatformPro.",
        )

    try:
        key_bytes = bytes.fromhex(key)
    except ValueError as exc:
        raise CardError(
            "GP_KEY_INVALID_HEX",
            "GP_KEY debe ser hexadecimal válido.",
        ) from exc

    if key_bytes == bytes.fromhex(GP_DEFAULT_MASTER_KEY_HEX):
        raise CardError(
            "GP_DEFAULT_KEY_FORBIDDEN",
            "Card Studio no permite usar la llave default de GlobalPlatformPro.",
        )

    if len(key_bytes) not in {16, 24, 32}:
        raise CardError(
            "GP_KEY_INVALID_LENGTH",
            "GP_KEY debe tener 16, 24 o 32 bytes.",
        )

    return key_bytes


def _decode_split_gp_key(
    name: str,
    value: str,
) -> bytes:
    value = value.strip()

    try:
        key_bytes = bytes.fromhex(
            value
        )
    except ValueError:
        raise CardError(
            "GP_KEY_INVALID_HEX",
            f"{name} debe ser hexadecimal válido.",
        )

    if len(key_bytes) not in (
        16,
        24,
        32,
    ):
        raise CardError(
            "GP_KEY_INVALID_LENGTH",
            f"{name} debe tener 16, 24 o 32 bytes.",
        )

    if key_bytes == bytes.fromhex(
        GP_DEFAULT_MASTER_KEY_HEX
    ):
        raise CardError(
            "GP_DEFAULT_KEY_FORBIDDEN",
            f"{name} no puede ser la llave default de GPPro.",
        )

    return key_bytes


def validate_gp_key_material_environment() -> dict[str, Any]:
    """
    Resolve GlobalPlatform authentication material.

    Supported production contracts:

      MASTER:
        GP_KEY

      SPLIT:
        GP_KEY_ENC
        GP_KEY_MAC
        GP_KEY_DEK

    Mixed or incomplete configurations fail closed.
    """

    master = os.environ.get(
        "GP_KEY",
        "",
    ).strip()

    split_values = {
        "GP_KEY_ENC":
            os.environ.get(
                "GP_KEY_ENC",
                "",
            ).strip(),

        "GP_KEY_MAC":
            os.environ.get(
                "GP_KEY_MAC",
                "",
            ).strip(),

        "GP_KEY_DEK":
            os.environ.get(
                "GP_KEY_DEK",
                "",
            ).strip(),
    }

    split_present = {
        name:
            bool(value)
        for name, value in split_values.items()
    }

    any_split = any(
        split_present.values()
    )

    all_split = all(
        split_present.values()
    )

    if master and any_split:
        raise CardError(
            "GP_KEY_CONFIGURATION_AMBIGUOUS",
            "No se permite mezclar GP_KEY con "
            "GP_KEY_ENC/MAC/DEK.",
        )

    if any_split and not all_split:
        raise CardError(
            "GP_SPLIT_KEYS_INCOMPLETE",
            "Las llaves SCP requieren ENC, MAC y DEK.",
        )

    if all_split:
        return {
            "mode":
                "SPLIT",

            "enc":
                _decode_split_gp_key(
                    "GP_KEY_ENC",
                    split_values["GP_KEY_ENC"],
                ),

            "mac":
                _decode_split_gp_key(
                    "GP_KEY_MAC",
                    split_values["GP_KEY_MAC"],
                ),

            "dek":
                _decode_split_gp_key(
                    "GP_KEY_DEK",
                    split_values["GP_KEY_DEK"],
                ),
        }

    return {
        "mode":
            "MASTER",

        "master":
            validate_gp_key_environment(),
    }


def _to_java_gp_key(
    raw: bytes,
    JArray: Any,
    JByte: Any,
) -> Any:
    return JArray(
        JByte
    )([
        value
        if value < 128
        else value - 256
        for value in raw
    ])


def build_plaintext_keys_from_material(
    key_material: dict[str, Any],
    PlaintextKeys: Any,
    JArray: Any,
    JByte: Any,
) -> Any:
    if key_material["mode"] == "MASTER":
        return PlaintextKeys.fromMasterKey(
            _to_java_gp_key(
                key_material["master"],
                JArray,
                JByte,
            )
        )

    if key_material["mode"] != "SPLIT":
        raise CardError(
            "GP_KEY_CONFIGURATION_INVALID",
            "Modo de llaves GlobalPlatform desconocido.",
        )

    return PlaintextKeys.fromKeys(
        _to_java_gp_key(
            key_material["enc"],
            JArray,
            JByte,
        ),
        _to_java_gp_key(
            key_material["mac"],
            JArray,
            JByte,
        ),
        _to_java_gp_key(
            key_material["dek"],
            JArray,
            JByte,
        ),
    )



def execute_secure_channel_preflight(
    *,
    card_session: CardSession,
    gp_jar: Path,
    runner: Any | None = None,
) -> int:
    """
    Execute a read-only GlobalPlatform Secure Channel preflight
    over the already-open CardSession.

    Production uses GPSession directly over Card Studio's
    existing BIBO/CardSession.

    The optional runner remains a test seam only.

    This path must never LOAD, INSTALL, DELETE, PUT KEY,
    or otherwise mutate card state.
    """

    key_material = validate_gp_key_material_environment()

    if not card_session.connected:
        raise CardError(
            "GP_SESSION_NOT_OPEN",
            "CardSession debe estar abierta antes del preflight seguro.",
        )

    # Test seam only.
    #
    # Production no longer delegates Secure Channel key handling
    # to GPTool/run_tool().
    if runner is not None:
        return int(
            runner(
                build_secure_channel_preflight_args()
            )
        )

    from jpype import (
        JArray,
        JByte,
        JClass,
    )

    bridge = GPProBIBOBridge(
        card_session,
        gp_jar,
    )

    proxy = bridge.start()

    GPSession = JClass(
        "pro.javacard.gp.GPSession"
    )

    APDUMode = JClass(
        "pro.javacard.gp.GPSession$APDUMode"
    )

    PlaintextKeys = JClass(
        "pro.javacard.gp.keys.PlaintextKeys"
    )

    EnumSet = JClass(
        "java.util.EnumSet"
    )

    keys = build_plaintext_keys_from_material(
        key_material,
        PlaintextKeys,
        JArray,
        JByte,
    )

    keys.setVersion(
        0
    )

    gp = GPSession.discover(
        proxy
    )

    modes = EnumSet.noneOf(
        APDUMode.class_
    )

    modes.add(
        APDUMode.MAC
    )

    modes.add(
        APDUMode.ENC
    )

    gp.openSecureChannel(
        keys,
        None,
        None,
        modes,
    )

    # Authenticated but read-only operation.
    gp.getRegistry()

    return 0


def execute_install(
    cap_path: Path,
    gp_jar: Path,
    registry_path: Path,
    *,
    card_session: CardSession,
    evidence_path: Path | None = None,
    runner: Any | None = None,
) -> dict[str, Any]:
    """
    Execute the authorized coexistence install over the already-open
    CardSession.

    Production uses GPSession directly over Card Studio's existing
    BIBO/CardSession.

    The optional runner remains a test seam only.

    Before the first mutating LOAD APDU, production MUST:

      1. validate the configured GP key,
      2. open Secure Channel with explicit key material,
      3. obtain an authenticated live registry,
      4. re-run the coexistence contract against that live registry.

    No DELETE, FORCE, PUT KEY, replacement or uninstall operation is
    permitted by this path.
    """

    report = prepare_install(
        cap_path,
        gp_jar,
        registry_path,
        require_live=True,
    )

    key_material = validate_gp_key_material_environment()

    # Keep the environment representation only for exception/evidence
    # redaction. Authentication uses key_bytes, not this string.
    key = os.environ.get(
        "GP_KEY",
        "",
    ).strip()

    if not card_session.connected:
        raise CardError(
            "GP_SESSION_NOT_OPEN",
            "CardSession debe estar abierta antes de instalar.",
        )

    # Pure/test compatibility contract.
    args = build_install_args(
        cap_path
    )

    report["transport"] = {
        "type": "IN_PROCESS_BIBO",
        "single_card_session": True,
        "external_reader_selection": False,
        "subprocess_install": False,
        "gp_execution": "DIRECT_GPSession",
        "gptool_run": False,
    }

    mutation_started = False

    try:

        # -----------------------------------------------------
        # TEST SEAM ONLY
        # -----------------------------------------------------
        if runner is not None:

            mutation_started = True

            returncode = int(
                runner(args)
            )

        # -----------------------------------------------------
        # PRODUCTION DIRECT GPSession PATH
        # -----------------------------------------------------
        else:

            from jpype import (
                JArray,
                JByte,
                JClass,
            )

            bridge = GPProBIBOBridge(
                card_session,
                gp_jar,
            )

            proxy = bridge.start()

            GPSession = JClass(
                "pro.javacard.gp.GPSession"
            )

            APDUMode = JClass(
                "pro.javacard.gp.GPSession$APDUMode"
            )

            PlaintextKeys = JClass(
                "pro.javacard.gp.keys.PlaintextKeys"
            )

            CAPFile = JClass(
                "pro.javacard.capfile.CAPFile"
            )

            Privilege = JClass(
                "pro.javacard.gp.GPRegistryEntry$Privilege"
            )

            EnumSet = JClass(
                "java.util.EnumSet"
            )

            Paths = JClass(
                "java.nio.file.Paths"
            )

            # ---------------------------------------------
            # Explicit key ownership.
            # ---------------------------------------------

            keys = build_plaintext_keys_from_material(
                key_material,
                PlaintextKeys,
                JArray,
                JByte,
            )

            keys.setVersion(
                0
            )

            gp = GPSession.discover(
                proxy
            )

            modes = EnumSet.noneOf(
                APDUMode.class_
            )

            modes.add(
                APDUMode.MAC
            )

            modes.add(
                APDUMode.ENC
            )

            gp.openSecureChannel(
                keys,
                None,
                None,
                modes,
            )

            # ---------------------------------------------
            # AUTHENTICATED LIVE REGISTRY GATE.
            #
            # Absolutely no LOAD has been transmitted yet.
            # ---------------------------------------------

            java_registry = gp.getRegistry()

            live_registry = {
                "mode": "LIVE_READ_ONLY",
                "entries": (
                    [
                        {
                            "type": "PACKAGE",
                            "aid": str(aid).upper(),
                        }
                        for aid
                        in java_registry.allPackageAIDs()
                    ]
                    + [
                        {
                            "type": "APPLET",
                            "aid": str(aid).upper(),
                        }
                        for aid
                        in java_registry.allAppletAIDs()
                    ]
                ),
            }

            live_validation = validate_registry(
                live_registry,
                require_live=True,
            )

            report[
                "authenticated_live_registry"
            ] = live_validation

            # ---------------------------------------------
            # CAP contract from the exact file being loaded.
            # ---------------------------------------------

            java_path = Paths.get(
                str(cap_path)
            )

            cap = CAPFile.fromFile(
                java_path
            )

            package_aid = str(
                cap.getPackageAID()
            ).upper()

            applets = [
                aid
                for aid in cap.getAppletAIDs()
            ]

            applet_aids = [
                str(aid).upper()
                for aid in applets
            ]

            if package_aid != PACKAGE_AID:
                raise CardError(
                    "CAP_PACKAGE_AID_MISMATCH",
                    "El paquete CAP cambió antes de LOAD.",
                )

            if (
                len(applet_aids) != 1
                or applet_aids[0] != APPLET_AID
            ):
                raise CardError(
                    "CAP_APPLET_AID_MISMATCH",
                    "El applet CAP cambió antes de LOAD.",
                )

            package = cap.getPackageAID()
            applet = applets[0]

            # Default GPTool semantics already established:
            #
            #   target SD      = current ISD
            #   DAP domain     = null
            #   DAP signature  = null
            #   LFDBH          = null
            #   privileges     = empty
            #   params         = byte[0]
            #   instance AID   = applet AID

            privileges = EnumSet.noneOf(
                Privilege.class_
            )

            empty_params = JArray(
                JByte
            )([])

            # =============================================
            # MUTATION BOUNDARY
            #
            # Nothing above this line performs LOAD/INSTALL.
            # =============================================

            mutation_started = True

            gp.loadCapFile(
                cap,
                gp.getAID(),
                None,
                None,
                None,
            )

            gp.installAndMakeSelectable(
                package,
                applet,
                applet,
                privileges,
                empty_params,
            )

            returncode = 0

        report["command_result"] = {
            "returncode": returncode,
            "transport": "IN_PROCESS_BIBO",
            "reader_supplied_to_gptool": False,
            "gptool_used": False,
            "gp_execution": (
                "TEST_RUNNER"
                if runner is not None
                else "DIRECT_GPSession"
            ),
            "stdout_redacted": "",
            "stderr_redacted": "",
        }

        if returncode == 0:

            report["physical_install"] = (
                "EXECUTED_PASS_POSTCHECK_REQUIRED"
            )

            report["physical_validation"] = (
                "INCONCLUSIVE"
            )

            report["result"] = (
                "INSTALL_COMMAND_PASS_POSTCHECK_REQUIRED"
            )

        else:

            report["physical_install"] = (
                "EXECUTED_FAIL"
            )

            report["physical_validation"] = (
                "FAIL"
            )

            report["result"] = (
                "FAIL"
            )

            report["errors"].append(
                "INSTALL_COMMAND_FAILED"
            )

    except subprocess.TimeoutExpired as error:

        report["command_result"] = {
            "returncode": None,
            "transport": "IN_PROCESS_BIBO",
            "reader_supplied_to_gptool": False,
            "gptool_used": False,
            "stdout_redacted": redact_output(
                error.stdout or "",
                key,
            ),
            "stderr_redacted": redact_output(
                error.stderr or "",
                key,
            ),
        }

        if mutation_started:

            report["physical_install"] = (
                "EXECUTED_TIMEOUT_OUTCOME_UNKNOWN"
            )

            report["physical_validation"] = (
                "INCONCLUSIVE"
            )

            report["errors"].append(
                "INSTALL_TIMEOUT_QUERY_REQUIRED"
            )

        else:

            report["physical_install"] = (
                "NOT_EXECUTED"
            )

            report["physical_validation"] = (
                "FAIL"
            )

            report["errors"].append(
                "INSTALL_BLOCKED_BEFORE_MUTATION"
            )

        report["result"] = "FAIL"

    except Exception as error:

        report["command_result"] = {
            "returncode": None,
            "transport": "IN_PROCESS_BIBO",
            "reader_supplied_to_gptool": False,
            "gptool_used": False,
            "exception_redacted": redact_output(
                str(error),
                key,
            ),
        }

        if mutation_started:

            # LOAD or INSTALL may already have crossed the card.
            # Never claim rollback or a clean pre-mutation failure.
            report["physical_install"] = (
                "EXECUTED_EXCEPTION_OUTCOME_UNKNOWN"
            )

            report["physical_validation"] = (
                "INCONCLUSIVE"
            )

            report["errors"].append(
                "INSTALL_EXCEPTION_QUERY_REQUIRED"
            )

        else:

            # Secure Channel discovery/authentication, live registry
            # validation and CAP contract checks all happen before
            # the first mutating LOAD APDU.
            report["physical_install"] = (
                "NOT_EXECUTED"
            )

            report["physical_validation"] = (
                "FAIL"
            )

            report["errors"].append(
                "INSTALL_BLOCKED_BEFORE_MUTATION"
            )

        report["result"] = "FAIL"

    if evidence_path is not None:

        write_evidence(
            Path(evidence_path),
            report,
        )

        report["evidence_path"] = str(
            evidence_path
        )

    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validación e instalación del CAP JavaCard coexistible sin borrar el anterior")
    result.add_argument("--cap", type=Path, default=DEFAULT_CAP)
    result.add_argument("--gp-jar", type=Path, default=DEFAULT_GP_JAR)
    result.add_argument("--registry", type=Path, required=True)
    result.add_argument("--execute", action="store_true")
    result.add_argument("--reader", default="")
    result.add_argument("--confirm-install", default="")
    result.add_argument("--evidence", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute and args.confirm_install != CONFIRMATION:
        print(json.dumps({
            "error": "INSTALL_CONFIRMATION_REQUIRED",
            "required_confirmation": CONFIRMATION,
            "physical_install": "NOT_EXECUTED",
        }, ensure_ascii=False, indent=2))
        return 2
    try:
        if args.execute:
            backend = create_pcsc_backend(
                reader_name=args.reader,
            )

            card_session = CardSession(
                backend
            )

            try:
                card_session.open()

                report = execute_install(
                    args.cap,
                    args.gp_jar,
                    args.registry,
                    card_session=card_session,
                    evidence_path=args.evidence,
                )
            finally:
                card_session.close()
        else:
            report = prepare_install(args.cap, args.gp_jar, args.registry)
            if args.evidence:
                write_evidence(args.evidence, report)
                report["evidence_path"] = str(args.evidence)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["result"] in {"SIMULATED_READY", "INSTALL_COMMAND_PASS_POSTCHECK_REQUIRED"} else 2
    except CardError as error:
        print(json.dumps({"error": error.code, "message": error.message, "physical_install": "NOT_EXECUTED"}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
