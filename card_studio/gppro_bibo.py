from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .card_session import CardSession
from .model import CardError


GP_JAR_RELATIVE_PATH = Path(
    "vendor/GlobalPlatformPro-next/tool/target/gp.jar"
)
REQUIRED_JAVA_CLASSES = (
    "apdu4j.core.BIBO",
    "pro.javacard.gptool.GPTool",
)


def resolve_gp_jar(gp_jar: str | Path | None = None) -> Path:
    if gp_jar is not None:
        return Path(gp_jar).expanduser().resolve()

    bundle_root = getattr(sys, "_MEIPASS", None)
    root = (
        Path(bundle_root)
        if bundle_root
        else Path(__file__).resolve().parents[1]
    )
    return (root / GP_JAR_RELATIVE_PATH).resolve()


def check_globalplatform_runtime(
    gp_jar: str | Path | None = None,
) -> dict[str, Any]:
    jar = resolve_gp_jar(gp_jar)
    jar_exists = jar.is_file()
    report = {
        "status": "FAIL",
        "gp_jar": {
            "path": str(jar),
            "exists": jar_exists,
        },
        "jpype": {
            "imported": False,
            "version": None,
        },
        "jvm": {
            "started": False,
            "already_started": False,
            "started_by_check": False,
        },
        "classes": {
            name: False
            for name in REQUIRED_JAVA_CLASSES
        },
        "physical_validation": "NOT_EXECUTED",
        "pcsc": "NOT_OPENED",
        "apdus": "NOT_SENT",
        "errors": [],
    }

    if not jar_exists:
        report["errors"].append({
            "code": "GPPRO_JAR_NOT_FOUND",
            "message": f"No existe gp.jar: {jar}",
        })
        return report

    try:
        import jpype
    except Exception as error:
        report["errors"].append({
            "code": "JPYPE_IMPORT_FAILED",
            "message": str(error),
        })
        return report

    report["jpype"] = {
        "imported": True,
        "version": str(getattr(jpype, "__version__", "UNKNOWN")),
    }

    try:
        already_started = bool(jpype.isJVMStarted())
        report["jvm"]["already_started"] = already_started
        if not already_started:
            jpype.startJVM(classpath=[str(jar)])
            report["jvm"]["started_by_check"] = True
        if not jpype.isJVMStarted():
            raise RuntimeError("JPype no confirmó el arranque de la JVM.")
        report["jvm"]["started"] = True
    except Exception as error:
        report["errors"].append({
            "code": "JVM_START_FAILED",
            "message": str(error),
        })
        return report

    for name in REQUIRED_JAVA_CLASSES:
        try:
            jpype.JClass(name)
        except Exception as error:
            report["errors"].append({
                "code": "JAVA_CLASS_LOAD_FAILED",
                "class": name,
                "message": str(error),
            })
        else:
            report["classes"][name] = True

    if not report["errors"]:
        report["status"] = "PASS"
    return report


class GPProBIBOBridge:
    """
    Bridge:

        GPPro / GPSession
              ↓
             BIBO
              ↓
        CardSession Python

    No abre ni cierra PC/SC.
    """

    def __init__(
        self,
        card_session: CardSession,
        gp_jar: str | Path,
    ) -> None:
        self.card_session = card_session
        self.gp_jar = Path(gp_jar)

        if not self.gp_jar.is_file():
            raise CardError(
                "GPPRO_JAR_NOT_FOUND",
                f"No existe gp.jar: {self.gp_jar}",
            )

        if not self.card_session.connected:
            raise CardError(
                "GP_SESSION_NOT_OPEN",
                "CardSession debe estar abierta antes de crear BIBO.",
            )

        self._jpype = None
        self._BIBO = None
        self._JByte = None
        self._JArray = None
        self._JString = None

        self._BIBO = None
        self._ResponseAPDU = None
        self._GPTool = None

        self._proxy = None

    def start(self) -> Any:
        import jpype
        from jpype import JArray, JByte, JClass, JProxy, JString

        if not jpype.isJVMStarted():
            jpype.startJVM(
                classpath=[str(self.gp_jar)]
            )

        self._jpype = jpype
        self._JByte = JByte
        self._JArray = JArray
        self._JString = JString

        self._BIBO = JClass(
            "apdu4j.core.BIBO"
        )

        self._ResponseAPDU = JClass(
            "apdu4j.core.ResponseAPDU"
        )

        self._GPTool = JClass(
            "pro.javacard.gptool.GPTool"
        )

        outer = self

        class PythonBIBO:
            def transceive(self, raw):
                return outer.transceive(raw)

            def transmit(self, command_apdu):
                return outer.transmit(command_apdu)

            def close(self):
                return outer.close()

        self._proxy = JProxy(
            self._BIBO,
            inst=PythonBIBO(),
        )

        return self._proxy

    def _java_bytes(self, raw: bytes):
        return self._JArray(self._JByte)([
            value if value < 128 else value - 256
            for value in raw
        ])

    @staticmethod
    def _strip_t0_case4_le(command: bytes) -> bytes:
        """
        Match javax.smartcardio/jnasmartcardio T=0 semantics.

        A short Case-4 APDU has:

            CLA INS P1 P2 Lc Data Le

        T=0 cannot transport the Case-4 Le together with incoming
        command data. The PC/SC transport used by GPPro removes Le
        before SCardTransmit and handles any response continuation
        separately.

        Extended-length APDUs are deliberately left untouched here.
        """
        command = bytes(command)

        if len(command) < 6:
            return command

        lc = command[4]

        # 00 in byte 5 is either Case-2 Le=256 or the marker for an
        # extended APDU. Neither is a short Case-4 APDU here.
        if lc == 0:
            return command

        # Short Case 4:
        # 4 header + 1 Lc + Lc data + 1 Le
        if len(command) == 6 + lc:
            return command[:-1]

        return command


    def _is_t0_protocol(self) -> bool:
        info = getattr(
            self.card_session,
            "info",
            None,
        )

        protocol = str(
            getattr(
                info,
                "protocol",
                "",
            )
            or ""
        ).upper().replace(
            " ",
            "",
        )

        return (
            protocol == "T0"
            or "T=0" in protocol
        )


    def transceive(self, raw) -> Any:
        command = bytes(
            (int(value) & 0xFF)
            for value in raw
        )

        current_command = command
        payload = bytearray()

        # T0_PROCEDURE_CHAIN
        #
        # ISO/IEC 7816 T=0 procedure handling:
        #
        #   6Cxx -> retry the current APDU with corrected Le.
        #   61xx -> issue GET RESPONSE for the announced bytes.
        #
        # All APDUs remain routed through the existing CardSession.
        for _procedure_step in range(16):
            wire_command = current_command

            if self._is_t0_protocol():
                wire_command = (
                    self._strip_t0_case4_le(
                        current_command
                    )
                )

            response = self.card_session.transmit(
                wire_command
            )

            try:
                sw = int(
                    response.sw1sw2,
                    16,
                )

                sw1 = (
                    (sw >> 8)
                    & 0xFF
                )

                sw2 = (
                    sw
                    & 0xFF
                )

                response_data = bytes(
                    response.data
                )

            except (
                AttributeError,
                TypeError,
                ValueError,
            ) as exc:
                raise CardError(
                    "INVALID_APDU_RESPONSE",
                    "CardSession devolvió una respuesta APDU inválida.",
                ) from exc

            # SW1=6C:
            # card rejected the supplied Le and tells us the
            # correct value in SW2.
            if sw1 == 0x6C:
                corrected_le = sw2

                if len(current_command) == 4:
                    current_command = (
                        current_command
                        + bytes([corrected_le])
                    )

                elif len(current_command) == 5:
                    current_command = (
                        current_command[:4]
                        + bytes([corrected_le])
                    )

                else:
                    lc = current_command[4]

                    if (
                        lc
                        and len(current_command)
                        == 5 + lc
                    ):
                        current_command = (
                            current_command
                            + bytes([corrected_le])
                        )

                    elif (
                        lc
                        and len(current_command)
                        == 6 + lc
                    ):
                        current_command = (
                            current_command[:-1]
                            + bytes([corrected_le])
                        )

                    else:
                        raise CardError(
                            "T0_6C_RETRY_UNSUPPORTED_APDU",
                            "No se puede corregir Le de forma segura para este APDU.",
                        )

                continue

            # Preserve any data received before a continuation.
            payload.extend(
                response_data
            )

            # SW1=61:
            # additional response bytes are waiting.
            if sw1 == 0x61:
                current_command = bytes([
                    0x00,
                    0xC0,
                    0x00,
                    0x00,
                    sw2,
                ])

                continue

            return self._java_bytes(
                bytes(payload)
                + bytes([sw1, sw2])
            )

        raise CardError(
            "T0_PROCEDURE_LIMIT",
            "Demasiadas continuaciones T=0 en una sola operación APDU.",
        )


    def transmit(self, command_apdu) -> Any:
        """
        Explicit implementation of BIBO.transmit(CommandAPDU).

        JPype JProxy cannot reliably dispatch BIBO's Java default
        transmit() implementation through invokeSpecial on the
        GPPro version used by Card Studio.

        This method deliberately routes the APDU back through
        transceive() and therefore through the existing CardSession.
        """

        if self._ResponseAPDU is None:
            raise CardError(
                "GPPRO_BIBO_NOT_STARTED",
                "El bridge BIBO todavía no fue iniciado.",
            )

        raw = command_apdu.getBytes()

        response = self.transceive(
            raw
        )

        return self._ResponseAPDU(
            response
        )

    def close(self) -> None:
        """
        BIBO lifecycle hook.

        Deliberately NO-OP.

        GPTool does not own the physical PC/SC connection.
        CardSession is the sole owner and must be closed explicitly
        by the caller that opened it.
        """

        return None

    def run_tool(
        self,
        args: list[str] | tuple[str, ...],
    ) -> int:
        """
        Execute GPTool in-process over this bridge.

        No new PC/SC reader is opened. GPTool receives the existing
        BIBO proxy backed by the already-open CardSession.
        """

        if not self.card_session.connected:
            raise CardError(
                "GP_SESSION_NOT_OPEN",
                "CardSession debe permanecer abierta durante GPTool.",
            )

        normalized = [
            str(item)
            for item in args
        ]

        # These options would violate Card Studio's single-owner
        # transport architecture. GPTool must use the supplied BIBO,
        # never select/open another PC/SC reader itself.
        forbidden = {
            "--reader",
            "-r",
            "--pcsc-exclusive",
            "-X",

            # Card Studio owns the key contract.
            # GPTool must consume GP_KEY from environment only.
            "--default",
            "--key",
            "--key-enc",
            "--key-mac",
            "--key-dek",
        }

        for token in normalized:
            if token.startswith("--reader="):
                raise CardError(
                    "GPPRO_EXTERNAL_READER_FORBIDDEN",
                    "GPTool debe usar el BIBO existente; "
                    "no puede abrir otro lector PC/SC.",
                )

            if token in {"--reader", "-r", "--pcsc-exclusive", "-X"}:
                raise CardError(
                    "GPPRO_EXTERNAL_READER_FORBIDDEN",
                    "GPTool debe usar el BIBO existente; "
                    "no puede abrir otro lector PC/SC.",
                )

            if (
                token in {
                    "--default",
                    "--key",
                    "--key-enc",
                    "--key-mac",
                    "--key-dek",
                }
                or token.startswith("--key=")
                or token.startswith("--key-enc=")
                or token.startswith("--key-mac=")
                or token.startswith("--key-dek=")
            ):
                raise CardError(
                    "GPPRO_KEY_ARGUMENT_FORBIDDEN",
                    "Card Studio permite llaves GP únicamente "
                    "mediante GP_KEY en el entorno.",
                )

        if self._proxy is None:
            self.start()

        java_args = self._JArray(
            self._JString
        )(
            normalized
        )

        tool = self._GPTool()

        return int(
            tool.run(
                self.proxy,
                java_args,
            )
        )

    @property
    def proxy(self):
        if self._proxy is None:
            raise CardError(
                "GPPRO_BIBO_NOT_STARTED",
                "El bridge BIBO todavía no fue iniciado.",
            )
        return self._proxy
