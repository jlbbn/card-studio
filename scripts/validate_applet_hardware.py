from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_studio.dry_run import DryRunAdapter
from card_studio.hardware_validation import HardwareValidator, validation_summary, write_evidence
from card_studio.pcsc import create_pcsc_backend
from card_studio.readiness import run_readiness
from card_studio.system import evidence_directory


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validación read-only de applet Java Card por PC/SC")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--readiness", action="store_true")
    result.add_argument("--scenario", default="aid1-selected")
    result.add_argument("--reader", default="")
    result.add_argument("--evidence", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.readiness:
        report = run_readiness(ROOT, include_tests=True)
        path = args.evidence or evidence_directory() / "readiness.json"
        write_evidence(path, report)
        for check in report["checks"]:
            print(f"[{check['status']}] {check['name']} {check['detail']}".rstrip())
        print(f"SOFTWARE READINESS: {report['software_readiness']}")
        print(f"HARDWARE READINESS: {report['hardware_readiness']}")
        print(f"PHYSICAL VALIDATION: {report['physical_validation']}")
        print(f"Evidence: {path}")
        return 0 if report["hardware_readiness"] == "PASS" else 1
    adapter = DryRunAdapter(args.scenario) if args.dry_run else create_pcsc_backend(reader_name=args.reader)
    report = HardwareValidator(adapter, preferred_reader=args.reader).run(args.evidence)
    print(validation_summary(report))
    if args.dry_run:
        print("Dry-run: PASS · SIMULATED · NO PHYSICAL CONFIRMATION")
        return 0
    if report["result"] == "PHYSICALLY_CONFIRMED":
        return 0
    if report["result"] in {"HARDWARE_NOT_CONNECTED", "READER_NOT_FOUND", "CARD_NOT_PRESENT"}:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
