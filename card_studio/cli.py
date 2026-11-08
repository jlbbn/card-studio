from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .application import CardStudioApplication as TransactionApplication
from .backends import CardpeekBackend, SunmiBackend, VirtualCardBackend
from .config import AppSettings, SettingsStore
from .host.base import HostError
from .money import Money
from .transactions.model import TransactionError, TransactionRequest
from .transactions.recovery import RecoveryError
from .diagnostics import collect_diagnostics
from .dry_run import DryRunAdapter
from .hardware_validation import HardwareValidator, choose_reader
from .integration.physical import prepare_physical_session
from .ledger import LedgerClient, LedgerError
from .model import CardError, parse_amount
from .pcsc import create_pcsc_backend
from .readers.base import Capability, CapabilityState, ReaderError
from .readers.pcsc import PcscReaderAdapter
from .service import CardStudioService
from .startup import initialize_application
from .store import JsonStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Swift Ledger Card Studio")
    result.add_argument("--mode", choices=["virtual", "pcsc", "cardpeek", "sunmi"])
    result.add_argument("--data-dir", type=Path)
    result.add_argument("--config-dir", type=Path)
    result.add_argument("--ledger-url")
    result.add_argument("--terminal-code")
    result.add_argument("--reader", default="")
    result.add_argument("--sunmi-url")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("detect")
    commands.add_parser("read")
    write = commands.add_parser("write")
    write.add_argument("identifier")
    for name in ["reload", "charge"]:
        command = commands.add_parser(name)
        command.add_argument("amount")
    commands.add_parser("health")
    commands.add_parser("sync")
    diagnose = commands.add_parser("diagnose")
    diagnose.add_argument("--check-api", action="store_true")
    commands.add_parser("config")
    commands.add_parser("configure")
    commands.add_parser("hardware-status")
    hardware = commands.add_parser("hardware-validation")
    hardware.add_argument("--dry-run", action="store_true")
    hardware.add_argument("--scenario", default="aid1-selected")
    hardware.add_argument("--evidence", type=Path)
    commands.add_parser("physical-prepare")

    transaction = commands.add_parser("transaction")
    transaction_commands = transaction.add_subparsers(dest="transaction_command", required=True)
    create = transaction_commands.add_parser("create")
    create.add_argument("--amount-minor", type=int, required=True)
    create.add_argument("--currency", default="MXN")
    create.add_argument("--exponent", type=int, default=2)
    create.add_argument("--profile-ref")
    create.add_argument("--interface", default="VIRTUAL", choices=["VIRTUAL", "CONTACT", "CONTACTLESS"])
    create.add_argument("--kernel-ref")
    create.add_argument("--scenario", default="approved")
    create.add_argument("--transaction-type", choices=["purchase", "refund", "reversal"], default="purchase")
    create.add_argument("--original-operation-id")
    create.add_argument("--original-amount-minor", type=int)
    create.add_argument("--operation-id")
    create.add_argument("--idempotency-key")
    create.add_argument("--correlation-id")
    create.add_argument("--application-aid")
    for name in ["get", "query", "cancel", "recover"]:
        operation = transaction_commands.add_parser(name)
        operation.add_argument("operation_id")
    transaction_commands.add_parser("pending")
    transaction_commands.add_parser("list")

    kernel = commands.add_parser("kernel")
    kernel_commands = kernel.add_subparsers(dest="kernel_command", required=True)
    kernel_commands.add_parser("readiness")
    l3 = commands.add_parser("l3")
    l3_commands = l3.add_subparsers(dest="l3_command", required=True)
    l3_commands.add_parser("readiness")
    return result


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _settings(args: argparse.Namespace, store: SettingsStore) -> AppSettings:
    settings = store.load()
    if args.ledger_url is not None:
        settings.ledger_url = args.ledger_url
    if args.terminal_code is not None:
        settings.terminal_code = args.terminal_code
    if args.sunmi_url is not None:
        settings.sunmi_url = args.sunmi_url
    if args.reader:
        settings.reader = args.reader
    return settings


def _transaction_cli(args: argparse.Namespace) -> int:
    app = TransactionApplication(data_dir=args.data_dir, config_dir=args.config_dir)
    if args.command == "kernel":
        emit(app.kernel_readiness())
        return 0
    if args.command == "l3":
        emit(app.l3_readiness())
        return 0
    command = args.transaction_command
    if command == "create":
        original_amount = None
        if args.original_amount_minor is not None:
            original_amount = Money(args.original_amount_minor, args.currency, args.exponent)
        readiness = app.kernel_readiness()
        request = TransactionRequest.create(
            money=Money(args.amount_minor, args.currency, args.exponent),
            profile_ref=args.profile_ref or app.default_profile_ref,
            interface=args.interface,
            kernel_ref=args.kernel_ref or readiness["kernels"][0]["kernel_ref"],
            transaction_type=args.transaction_type,
            scenario=args.scenario,
            original_operation_id=args.original_operation_id,
            original_amount=original_amount,
            operation_id=args.operation_id,
            idempotency_key=args.idempotency_key,
            correlation_id=args.correlation_id,
            application_aid=args.application_aid or app.default_profile.applications[0].aid,
        )
        emit(app.create_transaction(request))
        return 0
    if command == "get":
        emit(app.get(args.operation_id))
    elif command == "query":
        emit(app.query(args.operation_id))
    elif command == "cancel":
        emit(app.cancel(args.operation_id))
    elif command == "recover":
        emit(app.recover(args.operation_id))
    elif command == "pending":
        emit({"transactions": app.list_pending(), "simulation": True, "mode": "LAB / SYNTHETIC"})
    else:
        emit({"transactions": app.list_transactions(), "simulation": True, "mode": "LAB / SYNTHETIC"})
    return 0


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        settings_store = SettingsStore(args.config_dir)
        settings = _settings(args, settings_store)
        if args.command in {"transaction", "kernel", "l3"}:
            return _transaction_cli(args)
        if args.command == "configure":
            if args.mode:
                settings.mode = args.mode
            settings_store.save(settings)
            emit({"saved": True, "settings": settings.to_dict(), "secretKeySaved": False})
            return 0
        if args.command == "config":
            emit({"settings": settings.to_dict(), "path": str(settings_store.path), "secretKeySaved": False})
            return 0
        if args.command == "diagnose":
            emit(collect_diagnostics(settings, check_api=args.check_api))
            return 0
        if args.command == "hardware-status":
            report = initialize_application(settings_store)
            report["settings"] = report["settings"].to_dict()
            emit(report)
            return 0 if report["software_ready"] else 2
        if args.command == "hardware-validation":
            adapter = DryRunAdapter(args.scenario) if args.dry_run else create_pcsc_backend(reader_name=settings.reader)
            report = HardwareValidator(adapter, preferred_reader=settings.reader).run(args.evidence)
            emit(report)
            return 0 if args.dry_run or report["result"] == "PHYSICALLY_CONFIRMED" else 2
        if args.command == "physical-prepare":
            adapter = PcscReaderAdapter(
                create_pcsc_backend(reader_name=settings.reader),
                interface_capabilities={
                    "CONTACT": Capability(
                        CapabilityState.SUPPORTED,
                        source="PHYSICAL_PREPARATION_COMMAND",
                    ),
                },
            )
            reader_names = [reader.reader_id for reader in adapter.enumerate_readers()]
            reader_id, error = choose_reader(reader_names, settings.reader)
            if error:
                emit({
                    "status": error,
                    "reader_names": reader_names,
                    "physical_validation": "PENDING",
                    "simulation": False,
                })
                return 2
            report = prepare_physical_session(adapter, reader_id, "physical-preparation")
            emit(report)
            return 0 if report["status"] == "PREPARED" else 2

        ledger = LedgerClient(
            settings.ledger_url,
            settings.terminal_code,
            os.environ.get("SWIFT_LEDGER_TERMINAL_KEY", ""),
        )
        if args.command == "health":
            emit(ledger.health())
            return 0

        store = JsonStore(args.data_dir)
        service = CardStudioService(store)
        if args.command == "sync":
            recorded, last_error = service.sync_pending(ledger)
            emit({"synchronized": recorded, "lastError": last_error})
            return 1 if last_error else 0

        mode = args.mode or "virtual"
        if mode == "virtual":
            backend = VirtualCardBackend(store)
        elif mode == "pcsc":
            backend = create_pcsc_backend(reader_name=settings.reader)
        elif mode == "cardpeek":
            backend = CardpeekBackend()
        else:
            backend = SunmiBackend(settings.sunmi_url)
        if args.command in {"detect", "read"}:
            state = backend.detect() if args.command == "detect" else backend.read()
            emit({"mode": backend.mode, "transport": backend.label, "card": state.to_dict()})
            return 0

        if args.command == "write":
            kind = "WRITE_ID"
            amount = 0
            identifier = args.identifier
        elif args.command == "reload":
            kind = "RELOAD"
            amount = parse_amount(args.amount)
            identifier = None
        else:
            kind = "CHARGE"
            amount = parse_amount(args.amount)
            identifier = None
        after, operation = service.perform(backend, kind, amount, identifier, ledger)
        emit({"card": after.to_dict(), "operation": operation.to_dict()})
        return 0 if operation.api_status == "RECORDED" else 2
    except (TransactionError, RecoveryError, HostError) as error:
        emit({"error": error.code, "message": error.message, "simulation": True, "mode": "LAB / SYNTHETIC"})
        return 1
    except ReaderError as error:
        emit({"error": error.code, "message": error.message, "physical_validation": "NOT_EXECUTED"})
        return 1
    except CardError as error:
        emit({"error": error.code, "message": error.message})
        return 1
    except LedgerError as error:
        emit({"error": "LEDGER_ERROR", "message": str(error)})
        return 1
    except ValueError as error:
        emit({"error": "CONFIG_ERROR", "message": str(error)})
        return 1
