from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CHIP_ID_ROOT = Path.home() / "chip-id"


class ChipIdentifierError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChipIdentifierClient:
    root: Path
    artifact_root: Path
    timeout: float = 30.0

    def __init__(
        self,
        root: str | Path | None = None,
        artifact_root: str | Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        source = root or os.environ.get("CARD_STUDIO_CHIP_ID_ROOT", DEFAULT_CHIP_ID_ROOT)
        resolved_root = Path(source).expanduser().resolve()
        object.__setattr__(self, "root", resolved_root)
        object.__setattr__(
            self,
            "artifact_root",
            Path(artifact_root).expanduser().resolve()
            if artifact_root is not None
            else resolved_root / "artifacts",
        )
        object.__setattr__(self, "timeout", timeout)

    def _run(self, *args: str, allow_failure: bool = False) -> dict[str, Any]:
        command = [str(self.root / "run.sh")]
        if self.artifact_root != self.root / "artifacts":
            command.extend(["--root", str(self.artifact_root)])
        command.extend(args)
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=not allow_failure,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            detail = getattr(error, "stderr", "") or str(error)
            raise ChipIdentifierError(f"CHIP_IDENTIFIER_COMMAND_FAILED:{detail.strip()}") from error
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ChipIdentifierError("CHIP_IDENTIFIER_INVALID_JSON") from error
        if not isinstance(result, dict):
            raise ChipIdentifierError("CHIP_IDENTIFIER_INVALID_RESULT")
        return result

    def validate(self, artifact: str | Path) -> dict[str, Any]:
        result = self._run("validate", str(artifact))
        if not result.get("valid"):
            raise ChipIdentifierError(f"CHIP_IDENTIFIER_INVALID_ARTIFACT:{result.get('errors', [])}")
        return result

    def profile(self) -> dict[str, Any]:
        profile = self._run("profile")
        self.validate(self.artifact_root / "profile/chip-profile.json")
        return profile

    def container(self) -> dict[str, Any]:
        container = self._run("container", "build")
        self.validate(self.artifact_root / "container/atomic-container.json")
        return container

    def offline_report(self) -> dict[str, Any]:
        return self._run("offline")

    def status(self) -> dict[str, Any]:
        return self._run("status")

    def probe(self, mode: str = "auto") -> dict[str, Any]:
        if mode not in {"auto", "offline", "live"}:
            raise ValueError("INVALID_PROBE_MODE")
        result = self._run("probe", "--mode", mode, allow_failure=True)
        if result.get("status") == "FAIL":
            raise ChipIdentifierError(f"CHIP_IDENTIFIER_PROBE_FAILED:{result.get('errors', [])}")
        return result

    def hardware_summary(self, mode: str = "auto") -> dict[str, Any]:
        probe = self.probe(mode)
        reader = probe.get("reader")
        reader_text = " ".join(reader) if isinstance(reader, list) else str(reader or "")
        reader_lower = reader_text.lower()
        reader_model = None
        if "rocketek" in reader_lower and "cscr3" in reader_lower:
            reader_model = "ROCKETEK_CSCR3"
        elif reader_text:
            reader_model = "GENERIC_PCSC"
        return {
            "schema": "card-studio-chip-reader/v1",
            "source": probe["mode"].upper(),
            "status": probe["status"],
            "reader": reader,
            "reader_model": reader_model,
            "card_connected": probe["card_connected"],
            "atr": probe["atr"],
            "protocol": probe["protocol"],
            "physical_validation": "NOT_EXECUTED",
            "errors": probe["errors"],
            "warnings": probe["warnings"],
        }

    def summary(self) -> dict[str, Any]:
        profile = self.profile()
        container = self.container()
        return {
            "source": profile["source"],
            "physical_validation": profile["physical_validation"],
            "aid": profile["application"]["aid"],
            "application_label": profile["application"]["application_label"],
            "protocols": profile["capabilities"]["protocols"],
            "historical_bytes": profile["capabilities"]["historical_ascii"],
            "container_state": container["state"]["current"],
        }


def chip_identifier_summary() -> dict[str, Any]:
    return ChipIdentifierClient().summary()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--mode", choices=("auto", "offline", "live"), default="auto")
    args = parser.parse_args()
    try:
        client = ChipIdentifierClient()
        result = client.hardware_summary(args.mode) if args.probe else client.summary()
    except ChipIdentifierError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
