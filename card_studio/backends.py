from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .applet import APPLET_AID_CANDIDATES, build_select_apdu
from .model import (
    CardError,
    CardState,
    amount_apdu,
    parse_state_response,
    read_apdu,
    transition,
    write_id_apdu,
)
from .store import JsonStore


class CardBackend:
    mode = "VIRTUAL"
    label = "Virtual"

    def detect(self) -> CardState:
        return self.read()

    def read(self) -> CardState:
        raise NotImplementedError

    def write_id(self, identifier: str) -> CardState:
        raise NotImplementedError

    def reload(self, amount_minor: int) -> CardState:
        raise NotImplementedError

    def charge(self, amount_minor: int) -> CardState:
        raise NotImplementedError


class VirtualCardBackend(CardBackend):
    mode = "VIRTUAL"
    label = "Tarjeta virtual"

    def __init__(self, store: JsonStore):
        self.store = store

    def read(self) -> CardState:
        return self.store.load_card()

    def write_id(self, identifier: str) -> CardState:
        state = transition(self.read(), "WRITE_ID", identifier=identifier)
        self.store.save_card(state)
        return state

    def reload(self, amount_minor: int) -> CardState:
        state = transition(self.read(), "RELOAD", amount_minor)
        self.store.save_card(state)
        return state

    def charge(self, amount_minor: int) -> CardState:
        state = transition(self.read(), "CHARGE", amount_minor)
        self.store.save_card(state)
        return state


class ApduCardBackend(CardBackend):
    mode = "PHYSICAL"

    def exchange(self, apdu_hex: str) -> bytes:
        raise NotImplementedError

    def command(self, apdu_hex: str) -> CardState:
        return parse_state_response(self.exchange(apdu_hex))

    def read(self) -> CardState:
        return self.command(read_apdu())

    def write_id(self, identifier: str) -> CardState:
        return self.command(write_id_apdu(identifier))

    def reload(self, amount_minor: int) -> CardState:
        return self.command(amount_apdu("RELOAD", amount_minor))

    def charge(self, amount_minor: int) -> CardState:
        return self.command(amount_apdu("CHARGE", amount_minor))


def check_status_word(sw: int, data: str) -> bytes:
    if sw != 0x9000:
        if sw == 0x6A85:
            raise CardError("INSUFFICIENT_FUNDS", "Saldo insuficiente o límite de la tarjeta excedido.")
        if sw == 0x6A82:
            raise CardError("INCOMPATIBLE_CARD", "La tarjeta no contiene el applet Swift Ledger.")
        if sw == 0x6A80:
            raise CardError("INVALID_CARD_DATA", "El applet rechazó el identificador o el importe.")
        raise CardError("CARD_REJECTED", f"La tarjeta rechazó la operación (SW {sw:04X}).")
    try:
        return bytes.fromhex(data)
    except ValueError as exc:
        raise CardError("INVALID_CARD_RESPONSE", "El lector devolvió datos no hexadecimales.") from exc


class CardpeekBackend(ApduCardBackend):
    label = "PC/SC · Cardpeek"

    def __init__(self, script_path: Path | None = None, timeout: float = 15.0):
        self.script_path = script_path or Path(__file__).resolve().parent / "cardpeek" / "studio.lua"
        self.timeout = timeout

    def exchange(self, apdu_hex: str) -> bytes:
        executable = shutil.which("cardpeek")
        if not executable:
            raise CardError("CARDPEEK_MISSING", "Cardpeek no está instalado o no está en PATH.")
        environment = os.environ.copy()
        environment["CARD_STUDIO_SELECT_APDU"] = build_select_apdu(APPLET_AID_CANDIDATES[0].aid).hex().upper()
        environment["CARD_STUDIO_APDU"] = apdu_hex
        command = [executable, "--console", "--exec", f"dofile({json.dumps(str(self.script_path))})"]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise CardError("READER_TIMEOUT", "El lector no respondió a tiempo.") from exc
        for line in reversed(completed.stdout.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            if "error" in value:
                code = value["error"]
                messages = {
                    "NO_CARD": "No se detectó una tarjeta en el lector.",
                    "SELECT_FAILED": "La tarjeta no contiene el applet Swift Ledger.",
                    "CARDPEEK_ERROR": "Cardpeek no pudo completar la operación.",
                }
                raise CardError(code, messages.get(code, "Cardpeek no pudo completar la operación."))
            if "sw" in value:
                return check_status_word(int(value["sw"]), str(value.get("data", "")))
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CardError("CARDPEEK_UNAVAILABLE", detail or "No hay un lector Cardpeek disponible.")


class SunmiBackend(ApduCardBackend):
    label = "SUNMI V3 · ADB"

    def __init__(self, base_url: str = "http://127.0.0.1:5380", timeout: float = 12.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            method=method,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if payload else {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                value = json.loads(exc.read().decode("utf-8"))
                message = value.get("message", f"El puente SUNMI respondió HTTP {exc.code}.")
            except (ValueError, UnicodeDecodeError):
                message = f"El puente SUNMI respondió HTTP {exc.code}."
            raise CardError("SUNMI_REJECTED", message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CardError(
                "SUNMI_UNAVAILABLE",
                "Abre Card Bridge en la SUNMI y ejecuta adb forward tcp:5380 tcp:5380.",
            ) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise CardError("SUNMI_INVALID_RESPONSE", "El puente SUNMI devolvió JSON inválido.") from exc
        if not isinstance(value, dict):
            raise CardError("SUNMI_INVALID_RESPONSE", "El puente SUNMI devolvió una respuesta inesperada.")
        return value

    def health(self) -> dict[str, Any]:
        value = self.request("GET", "/health")
        if value.get("ok") is not True or value.get("reader") is not True:
            raise CardError("NFC_UNAVAILABLE", "La SUNMI no tiene NFC disponible en Card Bridge.")
        return value

    def detect(self) -> CardState:
        self.health()
        return self.read()

    def exchange(self, apdu_hex: str) -> bytes:
        value = self.request("POST", "/apdu", {"apdu": apdu_hex})
        if "error" in value:
            raise CardError(str(value["error"]), str(value.get("message", "La SUNMI rechazó la operación.")))
        return check_status_word(int(value.get("sw", 0)), str(value.get("data", "")))
