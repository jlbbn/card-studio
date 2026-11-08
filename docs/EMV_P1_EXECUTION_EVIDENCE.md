# EMV P1 execution evidence

Implementation evidence only. `PASS` requires implementation, positive and negative tests, integration, evidence and full regression. It does not mean EMVCo certification, external scheme approval or physical validation.

## Frozen baseline and final result

- P0: **35/35 PASS**, preserved.
- Full regression before P1: **307/307 PASS**.
- Full regression after P1: **361/361 PASS**.
- InternalEmvKernel core L3 matrix: **27/27 PASS, G1–G10 PASS**.
- InternalEmvKernel hardened P1 matrix: **20/20 PASS**, including RECOVER AC.
- Fake `LabKernelAdapter`: **0/20 NON_CONFORMANT**.
- P1: **29/29 PASS**.
- Formal certification: **NOT COMPLETED**.
- Physical validation: **NOT EXECUTED**.

P0 behavior and tests remained frozen. New evidence is `SIMULATED` / `LAB_ONLY`; specification authority is `EXTERNAL_UNVERIFIED`. No synthetic result was promoted to `PHYSICAL_VALIDATED`.

## Gap register

| gap_id | files | tests | negative cases | integration | evidence | status |
|---|---|---|---|---|---|---|
| K26 | `tlv/{codec,model,evidence}.py`; `emv/{record,gpo}.py` | `test_agent_a_p1_block1.py` K26 | Invalid high-tag bytes, 00/FF, non-canonical tags, contextual duplicates, depth, second-record rollback | Real GPO/READ RECORD mutation corpus with atomic store | SIMULATED / LAB_ONLY | PASS |
| K27 | `emv/{velocity,risk,decision_engine,runtime}.py`; `kernels/internal_emv.py` | `test_agent_a_p1_block2.py` K27 | Lower/upper/cumulative limits, conflicts, competing reservations, retry/restart counter conservation | Durable SQLite counters committed by real offline/online kernel flows | SIMULATED / LAB_ONLY | PASS |
| K28 | `emv/{c8_recovery,contactless,gpo,data_foundation,dol,runtime}.py`; `simulator/{icc,integration,scenarios}.py`; `l3/p1.py` | `test_agent_a_p1_block3.py` K28; `test_agent_b_p1_simulator.py` K28 | Relay timeout, ATC mismatch, ARQC recovery, replay, second authorization | Real D0 RECOVER AC after restart with DRDOL/expected ATC; matrix `p1-contactless-torn-recovery` | SIMULATED / LAB_ONLY | PASS |
| K29 | `kernels/{internal_emv,session,control}.py` | `test_agent_a_p1_block2.py`, `block3.py` recovery | Interrupted APDU session, non-restorable state, replay prohibition | Read-back/result restoration with zero new APDU | SIMULATED / LAB_ONLY | PASS |
| K30 | `kernels/{control,session,internal_emv}.py`; `simulator/{icc,integration,terminal}.py` | `test_agent_a_p1_block2.py`; `test_agent_b_p1_simulator.py` cancel cases | Cancel before/during/after exchange, blocked exchange, post-cancel approval | Cooperative threaded cancellation; before-send CANCELLED, after-send UNCERTAIN, idempotent | SIMULATED / LAB_ONLY | PASS |
| K31 | `kernels/control.py`; `emv/runtime.py` | `test_agent_a_p1_block2.py`, `block3.py` | Before/after-send fake-clock and real timeouts at every contact/online stage, command counts | Five contact APDU stages, four online stages and real GPO timeout | SIMULATED / LAB_ONLY | PASS |
| K32 | `kernels/{control,internal_emv}.py`; `emv/{runtime,c8_recovery}.py` | `test_agent_a_p1_block2.py`, `block3.py`; B recovery cases | Timeout/cancel/relay/torn must not become DECLINED or approval | Every new stage preserves UNCERTAIN/QUERY_REQUIRED and blocks replay | SIMULATED / LAB_ONLY | PASS |
| K33 | `kernels/internal_emv.py`; `host/mock.py`; `transactions/service.py` | `test_agent_a_p1_block3.py`; `test_agent_b_p1_transactions.py` | Unknown, contradictory or invalid read-back cannot fabricate approval | Independent host/kernel late result reconciliation without resend | SIMULATED / LAB_ONLY | PASS |
| K34 | `kernels/internal_emv.py`; existing adapter contracts | `test_agent_a_p1_block3.py`; existing parity suite | Double close, cancel after final, non-idempotent transitions | Existing Mock/Internal lifecycle parity retained | SIMULATED / LAB_ONLY | PASS |
| K35 | `kernels/internal_emv.py`; `simulator/{icc,terminal,integration}.py` | `test_agent_a_p1_block3.py`; `test_agent_b_p1_simulator.py` restart cases | Crash/restart at protocol stages, loader conflicts, automatic replay | Open/execute/query after restart restores only validated durable results; zero APDU | SIMULATED / LAB_ONLY | PASS |
| K36 | `emv/trace.py`; `kernels/control.py` | `test_agent_a_p1_block2.py` K36 | Arbitrary error text, PAN/PIN/track/keys/ARPC/scripts/AC/payload leakage | Sequenced allowlisted APDU metadata with operation hash and NO_AUTOMATIC_REPLAY | SIMULATED / LAB_ONLY | PASS |
| K37 | `profiles/{authority,validation}.py`; `kernels/internal_emv.py` | `test_agent_a_p1_block1.py` K37 | Missing mappings, tampered digest, non-LAB authority, legacy auto-approval | Effective PDOL/CDOL/TAC/IAC/ODA/CAPK manifest bound during real execution | SIMULATED / LAB_ONLY / EXTERNAL_UNVERIFIED | PASS |
| K38 | `emv/{unpredictable,runtime}.py`; `kernels/internal_emv.py`; `simulator/icc.py` | `test_agent_a_p1_block1.py` K38; `test_agent_b_p1_simulator.py` UN cases | Zero, repeated, short, low-diversity and weak outputs | Injected four-byte UN in real GPO; weak output blocked before APDU; digest-only evidence | SIMULATED / LAB_ONLY | PASS |
| K39 | `emv/transaction_semantics.py`; `kernels/internal_emv.py`; `transactions/service.py` | `test_agent_a_p1_block3.py` K39; `test_orchestrator_p1_integration.py` | Card reversal attempt, second approval, duplicate/late host compensation | Reversal HOST_ONLY bypasses card kernel and APDU; host reference and late recovery verified | SIMULATED / LAB_ONLY | PASS |
| K40 | `emv/{transaction_semantics,runtime}.py`; `kernels/internal_emv.py`; `transactions/{service,store}.py`; `host/mock.py` | `test_agent_a_p1_block3.py` K40; `test_agent_b_p1_transactions.py` | Unreviewed 9C mapping, excessive/concurrent refund, late outcome | Real refund uses 9C=20, original binding, GENERATE AC and host cumulative limits | SIMULATED / LAB_ONLY | PASS |
| T00 | Existing `KernelAdapter` and Mock/Internal implementations | Existing parity plus full regression | Divergent capabilities, cancel/close, online and recovery behavior | Contract preserved; no second kernel or adapter fork | SIMULATED / LAB_ONLY | PASS |
| T11 | `application.py`; `transactions/{service,evidence}.py`; `host/mock.py` | `test_agent_b_p1_transactions.py` T11 | Reader/kernel internals and sensitive payload leakage | Sanitized authorization, issuer, refund, reversal and query phases through facade | SIMULATED / LAB_ONLY | PASS |
| T12 | `l3/authority.py`; `profiles/{authority,validation}.py` | `test_agent_b_p1_simulator.py` T12; `test_agent_a_p1_block1.py` K37 | Unsigned/legacy authority, tampered binding, auto-approval | Profile digest and requirement binding without changing profile status | SIMULATED / LAB_ONLY / EXTERNAL_UNVERIFIED | PASS |
| T13 | `host/{base,mock}.py`; `transactions/{service,store}.py` | `test_agent_b_p1_transactions.py` T13; matrix concurrent refund | Missing/unapproved original, excessive/concurrent refunds, timeout/late result | Host-linked refund with atomic cumulative reservation and one winning Internal execution | SIMULATED / LAB_ONLY | PASS |
| T14 | `host/{base,mock}.py`; `transactions/{service,store}.py` | `test_agent_b_p1_transactions.py` T14; matrix concurrent reversal | Duplicate reversal, concurrent winner, timeout/query/late result | Original host reference, one reverse, independent final outcome and no second approval | SIMULATED / LAB_ONLY | PASS |
| T15 | `transactions/{store,service}.py`; `host/mock.py`; `simulator/integration.py` | `test_agent_b_p1_transactions.py` T15; matrix multiprocess case | Two processes same key, loser APDU/host execution, divergent timestamps | Real process A+B: one transaction/authorization, kernel counts [0,1], exact result convergence | SIMULATED / LAB_ONLY | PASS |
| T16 | `transactions/store.py`; `simulator/integration.py` | `test_agent_b_p1_transactions.py` T16; matrix linked concurrency | Cross-process refund/reversal races and crash reservations | BEGIN IMMEDIATE original invariants; conservative crash block until explicit cancel | SIMULATED / LAB_ONLY | PASS |
| T17 | `host/mock.py`; `transactions/service.py`; `kernels/internal_emv.py` | `test_agent_b_p1_transactions.py` T17; `test_agent_a_p1_block3.py` | Late result conflict, duplicate publication, resend or second GAC | New application reconciles append-only late result without kernel/host reexecution | SIMULATED / LAB_ONLY | PASS |
| T18 | `simulator/terminal.py`; `l3/p1.py` | `test_agent_b_p1_simulator.py` T18; fake control | Unknown steps, fake readiness, non-Internal proof | Step-driven execute/fault/reset/restart/query/late/cancel simulator on InternalEmvKernel | SIMULATED / LAB_ONLY | PASS |
| T19 | `simulator/icc.py`; `l3/internal.py` | `test_agent_b_p1_simulator.py` T19 and malformed corpus | Malformed stages, weak/repeated UN, torn, reader/card removal, reset | Stateful PSE/PPSE/AFL/records/ATC/faults exercised by real Internal flows | SIMULATED / LAB_ONLY | PASS |
| T20 | `simulator/{scenarios,terminal}.py`; `l3/p1.py` | `test_agent_b_p1_simulator.py` T20 | Unknown schema/steps, scenario/digest tampering | Twenty versioned scenarios with digest, individual evidence and manifest | SIMULATED / LAB_ONLY | PASS |
| T21 | `simulator/{terminal,integration}.py`; `l3/p1.py` | `test_agent_b_p1_simulator.py` T21; matrix protocol assertions | Wrong APDU sequence, host reference/timestamp mismatch, evidence tampering | Exact SELECT/READ RECORD bytes, per-interface sequences, host/package/reference hashes | SIMULATED / LAB_ONLY | PASS |
| T22 | `host/mock.py`; `transactions/service.py`; `simulator/integration.py` | `test_agent_b_p1_transactions.py` T17/T22; late matrix scenario | Independent late response with changed identity/timestamp or new authorization | Same original reference/timestamp; one authorization/refund/reversal after restart | SIMULATED / LAB_ONLY | PASS |
| T23 | `l3/authority.py`; `l3/p1.py` | `test_agent_b_p1_simulator.py` T12/T23 | Duplicate/unknown requirement, non-SPEC_WINS policy, invented verified authority | Requirement IDs/document/version/clause registry; conflicts resolve SPEC_WINS | EXTERNAL_UNVERIFIED | PASS |

## Verification log

- Frozen baseline command: `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests` → **307/307 PASS**.
- Agent A targeted: **29/29 PASS**.
- Agent B targeted: **20/20 PASS**.
- Orchestrator contract: **3/3 PASS**.
- Orchestrator HOST_ONLY integration: **2/2 PASS**.
- Hardened matrix: **Internal 20/20 PASS**; **fake 0/20 NON_CONFORMANT**.
- Final full regression: **361/361 PASS**.
- Remaining P1: **NONE**.

This closure covers the synthetic P1 scope only. External specification review, formal certification, production cryptography, authorized hardware and physical laboratory validation remain separate.
