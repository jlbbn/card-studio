from __future__ import annotations

from .terminal import TerminalScenario, TerminalStep


CONTACT_OFFLINE = (0xA4, 0xA4, 0xA8, 0xB2, 0xAE)
CONTACT_ONLINE = (*CONTACT_OFFLINE, 0xAE)
CONTACTLESS_ONLINE = (0xA4, 0xA4, 0xA8, 0xB2)


def p1_scenarios() -> tuple[TerminalScenario, ...]:
    return (
        TerminalScenario(
            "p1-contact-approved",
            "CONTACT",
            expected_instructions=CONTACT_OFFLINE,
            expected_directory="PSE",
            steps=(
                TerminalStep("EXECUTE"),
                TerminalStep("ASSERT_SEQUENCE", list(CONTACT_OFFLINE)),
            ),
        ),
        TerminalScenario(
            "p1-contactless-online",
            "CONTACTLESS",
            "online_required",
            expected_instructions=CONTACTLESS_ONLINE,
            expected_directory="PPSE",
            steps=(
                TerminalStep("EXECUTE"),
                TerminalStep("ASSERT_SEQUENCE", list(CONTACTLESS_ONLINE)),
                TerminalStep("ASSERT_HOST_REFERENCE"),
            ),
        ),
        TerminalScenario(
            "p1-un-valid",
            "CONTACT",
            unpredictable_number="valid",
            expected_instructions=CONTACT_OFFLINE,
            expected_directory="PSE",
        ),
        TerminalScenario(
            "p1-un-weak",
            "CONTACT",
            unpredictable_number="weak",
            expected_state="FAILED",
            expected_error="WEAK_UN_OUTPUT",
        ),
        TerminalScenario(
            "p1-un-repeated",
            "CONTACT",
            unpredictable_number="repeated",
            expected_state="FAILED",
            expected_error="REPEATED_UN_OUTPUT",
            steps=(TerminalStep("EXECUTE"), TerminalStep("EXECUTE_NEW")),
        ),
        *(
            TerminalScenario(
                f"p1-malformed-{name}",
                "CONTACT",
                expected_state="FAILED",
                expected_error=error,
                expected_instructions=instructions,
                steps=(TerminalStep("FAULT", f"malformed:{instruction}"), TerminalStep("EXECUTE")),
            )
            for name, error, instruction, instructions in (
                ("select", "MALFORMED_FCI", "A4", (0xA4,)),
                ("gpo", "MALFORMED_GPO_RESPONSE", "A8", (0xA4, 0xA4, 0xA8)),
                ("record", "MALFORMED_RECORD", "B2", (0xA4, 0xA4, 0xA8, 0xB2)),
                ("gac", "MALFORMED_GAC_RESPONSE", "AE", CONTACT_OFFLINE),
            )
        ),
        TerminalScenario(
            "p1-contactless-torn",
            "CONTACTLESS",
            "online_required",
            expected_state="UNCERTAIN",
            expected_error="CONTACTLESS_TORN",
            expected_instructions=(0xA4, 0xA4, 0xA8),
            expected_directory="PPSE",
            steps=(TerminalStep("FAULT", "torn:A8"), TerminalStep("EXECUTE")),
        ),
        TerminalScenario(
            "p1-card-removed",
            "CONTACT",
            expected_state="UNCERTAIN",
            expected_error="CARD_REMOVED",
            expected_instructions=(0xA4, 0xA4, 0xA8, 0xB2),
            expected_directory="PSE",
            steps=(TerminalStep("FAULT", "card_removed:B2"), TerminalStep("EXECUTE")),
        ),
        TerminalScenario(
            "p1-reader-removed",
            "CONTACT",
            expected_state="FAILED",
            expected_error="READER_REMOVED",
            steps=(TerminalStep("FAULT", "reader_absent:00"), TerminalStep("EXECUTE")),
        ),
        TerminalScenario(
            "p1-late-host-reconcile",
            "CONTACT",
            "online_required",
            host_timeout=True,
            expected_state="SUCCEEDED",
            expected_instructions=CONTACT_OFFLINE,
            expected_directory="PSE",
            steps=(
                TerminalStep("EXECUTE"),
                TerminalStep("PUBLISH_LATE", "APPROVED"),
                TerminalStep("RESTART"),
                TerminalStep("QUERY"),
                TerminalStep("ASSERT_HOST_REFERENCE"),
            ),
        ),
        TerminalScenario(
            "p1-restart-uncertain",
            "CONTACTLESS",
            "timeout",
            expected_state="UNCERTAIN",
            expected_error="TIMEOUT_AFTER_SEND",
            expected_instructions=CONTACTLESS_ONLINE,
            expected_directory="PPSE",
            steps=(TerminalStep("EXECUTE"), TerminalStep("RESTART"), TerminalStep("QUERY")),
        ),
        TerminalScenario(
            "p1-card-reset",
            "CONTACT",
            expected_instructions=(),
            steps=(
                TerminalStep("EXECUTE"),
                TerminalStep("ASSERT_SEQUENCE", list(CONTACT_OFFLINE)),
                TerminalStep("RESET"),
            ),
        ),
        TerminalScenario(
            "p1-multiprocess-idempotency",
            "CONTACT",
            "online_required",
            steps=(),
            metadata={"integration": "multiprocess_idempotency", "processes": 2},
        ),
        TerminalScenario(
            "p1-concurrent-refund",
            "CONTACT",
            steps=(),
            metadata={"integration": "concurrent_refund", "processes": 2},
        ),
        TerminalScenario(
            "p1-concurrent-reversal",
            "CONTACT",
            steps=(),
            metadata={"integration": "concurrent_reversal", "processes": 2},
        ),
        TerminalScenario(
            "p1-cancel-exchange",
            "CONTACT",
            steps=(),
            expected_state="UNCERTAIN",
            expected_error="OPERATOR_CANCELLED",
            metadata={"integration": "cancel_exchange", "cooperative": True},
        ),
        TerminalScenario(
            "p1-contactless-torn-recovery",
            "CONTACTLESS",
            "online_required",
            steps=(),
            metadata={"integration": "torn_recovery", "recover_instruction": "D0", "expected_atc": "0001"},
        ),
    )
