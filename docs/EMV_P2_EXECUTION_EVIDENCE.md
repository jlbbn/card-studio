# EMV P2 execution evidence

Integration-hardening evidence only. `PASS` requires implementation, positive and negative tests, integration, sanitized evidence and full regression. It does not mean physical validation or certification.

## Frozen baselines

- P0: **35/35 PASS**, preserved.
- P1: **29/29 PASS**, preserved.
- Core L3: **27/27 PASS, G1–G10 PASS**.
- Hardened L3: **20/20 PASS**.
- Initial P2 regression: **361/361 PASS**.
- Final P2 regression: **377/377 PASS**.
- P1 frozen code/tests hash: `056401ecbd55d750ae95356d127d256dc0b041f27aedd482c3f6c49c1a1642ef`.
- P1 frozen evidence hash: `5d9e9ab781915c2274ceb73504031a0c9b099f729f47a1a1c5d3747ff19c3ea4`.

Historical P0/P1 results were not rewritten. P2 changes are identified in `PUNTO_DE_CONTROL_EMV_P2.md`.

## Integration gaps

| gap_id | files | tests | negative cases | integration | evidence | status |
|---|---|---|---|---|---|---|
| T24 | `host/mock.py`; `integration/{chain,backend,sunmi}.py`; `transactions/model.py` | `test_p2_integration.py`; host/P1 regression | Missing original reference, duplicate/late operation, incorrect host operation | Durable authorize/refund/reverse references, original references and timestamps in CARD results | SIMULATED / LAB_ONLY | PASS |
| T25 | `money.py`; `transactions/model.py`; `test_p2_integration.py` | Money/original boundary tests | Negative/zero transaction amounts, invalid exponent/currency and original mismatch | Integer minor units and exact original currency/exponent validation | SIMULATED / LAB_ONLY | PASS |
| T26 | `integration/physical.py`; `readers/{base,pcsc}.py`; `pcsc.py` | `test_p2_physical.py`; legacy reader regression | UNKNOWN protocol/capability, missing reader, legacy backend without getProtocol | Sanitized reader manifest with adapter version, ATR, interface and T=0/T=1 evidence | SIMULATED / LAB_ONLY | PASS |
| T27 | `integration/chain.py`; `integration/backend.py`; existing transaction audit | `test_p2_integration.py` full chain | Missing evidence, broken correlation, payload leakage | `run_id`, correlation UUID-compatible identifiers, operation id, profile/kernel/host identity and evidence SHA-256 | SIMULATED / LAB_ONLY | PASS |
| T28 | `integration/physical.py`; `readers/pcsc.py`; `pcsc.py`; `cli.py` | `test_p2_physical.py`; `test_p2_cli_ui.py`; legacy reader tests | Timeout, card removal, reader absence, disconnect/reconnect and uncertain recovery | Non-destructive detect/ATR/protocol/SELECT/disconnect/reconnect preparation; no write | SIMULATED / LAB_ONLY | PASS |
| T29 | `cli.py`; `integration/physical.py` | `test_p2_cli_ui.py` unpatched subprocesses | Invalid amount, PCSC unavailable, non-JSON errors and secret leakage | Real `python -m card_studio --cli` readiness, dry-run, physical-prepare and JSON errors | SIMULATED / LAB_ONLY | PASS |
| T30 | `transactions/view.py`; `integration/sunmi.py`; `test_p2_cli_ui.py` | Headless CARD view and invalid transition tests | Query/review actions, invalid state transition, CASH/CARD state mixing | CARD-specific flow/view with no CASH state reuse and physical NOT_EXECUTED | SIMULATED / LAB_ONLY | PASS |

**Integration P2: 7/7 PASS.**

## Physical validation preparation — T31

Implementation: `integration/physical.py`, `pcsc.py`, `readers/pcsc.py`, `cli.py` and `tests/test_p2_physical.py`, `test_p2_cli_ui.py`.

Verified locally, without physical writes or monetary operations:

- Rocketek/PCSC reader selection contract, ATR and protocol manifests.
- Both AIDs evaluated in every run: one 9000 + one 6A82 selects only in PHYSICAL mode; both 9000 → `MULTIPLE_AID_MATCH`; both 6A82 → `APPLET_NOT_FOUND`.
- SIMULATED matches never populate physical `selected_aid` and never create physical PASS.
- Gate order: BLANK → DETECTED → VALIDATED → APPLET_READY → PERSONALIZING → PERSONALIZED → VERIFIED → REGISTERED → TOKEN_BOUND → ACTIVE.
- Personalization requires explicit authorization and exact WRITE → READ-BACK → COMPARE → VERIFY. Unauthorized writes and mismatches cannot advance.
- Disconnect, reconnect, timeout, removal and UNCERTAIN/QUERY_REQUIRED recovery are covered by positive and negative tests.
- SUNMI V3 uses a separate CARD state machine: CARD_DETECTED → APPLICATION_SELECTED → PROCESSING → ONLINE_REQUIRED → APPROVED / DECLINED / UNCERTAIN, with APPROVED → REVERSED when applicable. CASH state is not reused.
- Backend CARD operations use correlation/idempotency, persistence, Swift Ledger recording, reconciliation, sanitized evidence and receipt metadata.

**Physical Validation Preparation: PASS.**
**Physical Validation: NOT_EXECUTED.**

Actual non-destructive environment probe returned `PCSC_UNAVAILABLE` because the `smartcard` module is unavailable. The CLI returns this as sanitized JSON, not a traceback or physical PASS. T31 therefore remains `BLOCKED / PCSC_UNAVAILABLE` in the gap matrix until an authorized PC/SC stack, reader, card and host are available.

## Verification log

- Frozen baseline: **361/361 PASS**.
- P2 targeted physical/integration: **11/11 PASS**.
- P2 targeted CLI/UI: **5/5 PASS**.
- Host and P1 regression during integration: **31/31 PASS**.
- Legacy PC/SC regression fix: **3/3 PASS**, followed by full regression **372/372 PASS**.
- Final full regression: **377/377 PASS**.
- P0: **35/35 PASS**; P1: **29/29 PASS**; hardened L3: **20/20 PASS**.
- Formal certification: **NOT COMPLETED**.
- Physical validation: **NOT EXECUTED**.
- Remaining integration P2: **NONE**. External physical blocker: **PCSC_UNAVAILABLE**.
