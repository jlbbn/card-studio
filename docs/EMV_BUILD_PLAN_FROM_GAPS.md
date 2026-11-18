# Card Studio — EMV build plan derived from reference gaps

## Decision and boundaries

This plan sequences construction by technical dependency, not implementation convenience. It does not authorize copying reference code, using unknown data, connecting a production host or executing physical validation. **SPEC_WINS** over every repository behavior. The applicable EMVCo/scheme/acquirer document versions and requirement IDs must be recorded before normative decisions are implemented.

The existing private `LAB_ONLY` protocol remains clearly identified and cannot be promoted to “EMV” by renaming commands or outcomes. The existing `LabKernelAdapter` matrix remains a service-level fake suite and cannot produce an L3 readiness claim. Legacy behavior and the 144-test baseline are compatibility constraints.

Licensing gate: dcemv is architecture-only/`DO_NOT_REUSE`; tomkp code remains blocked pending full notice/provenance; Hyperion MIT and emvpt/simulator Zlib permit consideration only after a separate reuse decision, notice preservation and security review. Clean-room reimplementation from reviewed behavior is the default. No `UNKNOWN` fixture, key, CAPK, PAN-like value, track or personalization material may enter the project.

## Dependency-ordered construction

### Stage 0 — Authority, scope and evidence contract

**IDs:** T23, T00, X01; licensing/data-classification gates.

1. Select applicable EMVCo contact/contactless books, scheme/kernel bulletins and acquirer host requirements; record exact versions and requirement IDs.
2. Freeze the distinction between private LAB simulation, EMV-oriented test implementation, physical validation and formal certification.
3. Freeze shared contracts before parallel work: typed APDU/error results, DOL/DataStore ownership, functional versus execution result, recovery actions, online response boundary and sanitized trace schema.
4. Define evidence allowlists and prohibited fields before adding richer APDU/TLV/host payloads.

**Exit:** requirement/provenance register approved; Mock/Internal contract parity strategy approved; no unknown data and no certification claim.

### Stage 1 — P0 data and transport foundations

**IDs:** K01, K02, K25, K26 preparatory hardening, K38 boundary.

1. Implement the EMV data dictionary and atomic DataStore with tag ownership, duplicate/provenance rules and mandatory-object validation.
2. Implement DOL parsing/building for PDOL first, then CDOL1/2 and DDOL/TDOL as required by the approved flows. Padding/truncation follows the specification data type, never repository convenience.
3. Implement general APDU command/response cases, Lc/Le validation, truncated-response rejection, contextual status words and bounded 61xx/6Cxx chains.
4. Preserve transport distinctions: pre-send failure may be retryable; post-send timeout/removal is UNCERTAIN; malformed data is a technical failure, not DECLINED.
5. Define the injected Unpredictable Number boundary and deterministic test source; reject weak/repeated values according to the approved policy.

**Tests:** DOL absent/short/long/duplicate data, output caps, every padding type, APDU cases, SW chains, malformed/truncated responses, before/after-send failures and redaction. No partial DataStore commit.

**Exit:** K01/K02/K25 closed with reviewed tests. Later stages cannot bypass this gate.

### Stage 2 — P0 selection and application initialization

**IDs:** K03, K04, K05, K06, K07, K08.

1. Implement strict SELECT and final FCI validation.
2. Add contact PSE and contactless PPSE discovery, Directory Entries, priorities, duplicates, exact/partial AID matching and interface/kernel compatibility.
3. Preserve `APPLICATION_SELECTION_REQUIRED` for ambiguous compatible candidates unless an explicit selection contract is approved. Never infer scheme certification from AID.
4. Build GPO using PDOL; parse 77/80 strictly, require valid two-byte AIP and AFL, and reject impossible SFI/record plans.
5. For contactless, reserve interface-specific GPO/cryptogram behavior rather than forcing the contact sequence.

**Tests:** PSE/PPSE differences, PPSE without records, multiple/no common AID, duplicate entries, explicit cancellation, invalid FCI, GPO formats, missing/wrong AIP/AFL and invalid record ranges.

**Exit:** a deterministic application and record plan exists; no card data has been trusted yet.

### Stage 3 — P0 card data and transaction state

**IDs:** K09, K10, K11, K12.

1. Execute AFL-driven READ RECORD with strict template 70 parsing and atomic DataStore updates.
2. Reject duplicate/conflicting terminal/host/GAC-owned tags and missing mandatory records; never omit failed records silently.
3. Add per-transaction TVR/TSI with symbolic bits, RFU masks and reset on every new transaction.
4. Implement application version, BCD effective/expiry dates and AUC restrictions, writing exact TVR/TSI outcomes.

**Tests:** record order, truncation, empty/nested templates, duplicate tags, provenance injection, rollback after a failed later record, TVR/TSI reset and date/AUC boundaries.

**Exit:** authenticated-input boundaries and current transaction state are explicit; restrictions cannot be skipped.

### Stage 4 — P0 decision pipeline

**IDs:** K13, K14, K15, K27 design dependency.

1. Parse CVM List 8E and apply amount X/Y, conditions, terminal capabilities and continue-on-failure; emit 9F34 and TVR without PIN capture.
2. Connect existing profile-driven floor/interface/random decisions to TVR/TSI. Define non-volatile velocity counter ownership before implementing K27.
3. Add TAC/IAC evaluation and denial → online → default precedence. Technical failures remain separate from AAC/DECLINED.
4. Keep all rules profile-driven; no brand-name branches.

**Tests:** every CVM condition and failure path, unsupported CVM, signature/CDCVM/synthetic PIN-related handles, floor/velocity boundaries, deterministic random boundaries, TAC/IAC combinations and FAILED≠DECLINED.

**Exit:** a reviewed terminal action and complete decision state are available for cryptogram processing.

### Stage 5 — P0 ODA and cryptogram architecture

**IDs:** K18, K16, K17.

1. Define ODA selection from AIP and configuration, SDA/DDA/CDA result semantics, trust boundary and TVR/TSI effects.
2. Block CAPK/certificate/private-key use until approved TEST provenance exists. The boundary and negative tests can be built without importing reference fixtures.
3. Implement first/final GENERATE AC only after DOL/TVR/TSI/TAA; parse 77/80 and CID/AC/ATC/IAD strictly.
4. Map AAC/TC/ARQC to functional results without conflating missing/malformed cryptograms with card declines.

**Tests:** AIP/ODA selection, absent/invalid approved TEST material, certificate/signature truncation when authorized vectors exist, CDOL errors, CID types, mandatory AC/ATC/IAD and malformed 77/80.

**Exit:** cryptogram results are explicit and ODA trust decisions are auditable; unknown material remains blocked.

### Stage 6 — P0 contact kernel integration

**IDs:** K21, K24, K29–K35 relevant invariants.

1. Integrate selection, GPO, records, restrictions, CVM, risk, TAA, ODA and GENERATE AC into the strict contact FSM.
2. Add explicit events/transitions and sanitized transition history; no silent stage skipping.
3. Define cooperative cancellation, timeout and crash points for every exchange. Preserve idempotent query/cancel/close and no automatic resend.
4. Keep ReaderAdapter/CardInterface outside kernel decision logic; all card commands use `CardInterface.exchange()`.

**Tests:** full contact synthetic flow, T=0/T=1 in approved scope, invalid transitions, every-stage removal/timeout, restart and duplicate execution.

**Exit:** contact kernel flow passes its own synthetic tests, without an L3 or certification claim.

### Stage 7 — P0 host and issuer completion

**IDs:** K19, K20, T02, T03, T04, T10.

1. Extend the sanitized host response contract for 8A/ARC policy, 91 issuer authentication, 71/72 scripts and durable host references.
2. Implement strict host response parsing: mandatory/duplicate/nested/unsupported objects and malformed lengths fail closed.
3. Implement online decline/fallback policy, EXTERNAL AUTHENTICATE outcome, script phases, CDOL2/final GENERATE AC where applicable.
4. Extend MockAuthorizationHost persistence and idempotency; do not add an acquirer client.
5. A post-send host timeout remains UNCERTAIN and queryable; no decline, automatic reversal or second authorization.

**Tests:** valid/invalid/missing ARC/ARPC, authentication failure, scripts before/after, per-command results, TVR/TSI, fallback, host timeout and sanitized evidence.

**Exit:** a complete simulated online cycle works through the same KernelAdapter and host boundary for Mock/Internal.

### Stage 8 — P0 transaction engine, reversal and recovery

**IDs:** T11, T13–T17, T22, K27, K33, K35, K39, K40.

1. Orchestrate host/issuer phases in TransactionService without leaking reader/kernel/store internals to the facade.
2. Add durable host/original references, independent reversal outcome/query and reviewed refund semantics.
3. Make original-level refund/reversal invariants atomic across competing application instances.
4. Add two-process final-result convergence, late host reconciliation after restart and crash-after-host-send tests.
5. Add durable TRM counters only after their ownership and atomicity are defined.
6. Never convert restart, IN_PROGRESS or UNCERTAIN into success/failure without explicit evidence.

**Tests:** concurrent duplicate/refund/reversal, partial/excessive refund, late approval/decline, unknown/mismatched query, safe retry, unsafe retry blocked and no duplicate host request.

**Exit:** durable terminal results converge safely under restart and duplicate execution.

### Stage 9 — P0 contactless and C-8

**IDs:** K22, K23, T01, T06, then P1 K28.

1. Select contactless kernel using reviewed AID/Kernel ID/configuration; implement TTQ/CTQ and applicable outcome model.
2. Implement interface-specific cryptogram-in-GPO and no-second-GENERATE-AC behavior where applicable; do not reuse contact assertions as proof.
3. Add C-8 start/restart/UI/limits and structured outcomes.
4. After the base is stable, add relay resistance and torn RECOVER AC/DRDOL behavior in approved scope.

**Tests:** contactless approved/declined/online/timeout/reversal, command sequence differences, removal at every stage, alternate interface/CVM/online outcomes, relay timing and matching/mismatched torn recovery.

**Exit:** contactless has independent evidence for its own sequence and outcomes.

### Stage 10 — P1 synthetic ICC, trace and profile hardening

**IDs:** T18, T19, T20, K27, K36, K37, K38, K26.

1. Build a stateful local SYNTHETIC ICC with PSE/PPSE, AFL records, ATC/counters, deterministic reset and fault injection.
2. Add malformed/unknown/duplicate TLV and status/delay/response-chain mutations generated locally.
3. Add sequenced redacted APDU/host traces and replayable scenario manifests.
4. Version profile DOL/TAC/IAC/CVM/ODA/kernel mappings with provenance; no automatic approval from legacy configuration.
5. Complete TLV canonicality and DataStore hardening against the new fault corpus.

**Exit:** tests can reproduce card, terminal and host faults without unknown fixtures or real cardholder data.

### Stage 11 — P0/P1 L3 harness and evidence gates

**IDs:** T05–T09, T20, T21.

1. Replace mandatory fake-backed L3 execution with the actual Internal kernel through the unchanged TransactionService/facade.
2. Keep fake tests separately labelled as contract tests; fake-only, mixed or tampered adapter identities cannot produce readiness.
3. Bind every case to profile/kernel/host/terminal/reader versions and a sanitized run manifest.
4. Add per-interface command/host-sequence assertions, malformed host/card responses, restart and late reconciliation.
5. Execute the complete CONTACT and CONTACTLESS matrix plus negative/refund/reversal/recovery cases.

**Gate rule:** L3 LAB READINESS remains blocked until all 35 consolidated P0 rows are closed or formally re-scoped with evidence, all required P1 dependencies for those rows pass, and no gate is based solely on `LabKernelAdapter`.

### Stage 12 — P2 integration hardening

**IDs:** T24–T30.

Add only reviewed host fields, public Money boundaries, reader/run manifests, audit correlation, unpatched subprocess CLI tests and headless UI signal tests. Preserve legacy CLI/UI and recovery-aware actions. These tasks cannot compensate for an open P0 protocol or evidence gap.

### Stage 13 — External physical and formal evaluation

**ID:** T31 and formal certification outside X01 construction scope.

Only after authorized hardware, test cards, reader/L1 environment, host/acquirer and laboratory access exist:

- execute physical reader/card diagnostics and approved L3 test cases;
- preserve separate device/L1/host evidence;
- perform a formal conformance gap assessment against the exact external test plan;
- leave certification NOT COMPLETED until a formal evaluator issues the relevant result.

## Integration gates

| Gate | Required evidence | Blocks |
|---|---|---|
| G1 — Contracts | Shared models reviewed; Mock/Internal parity; no private protocol relabelling | All implementation stages |
| G2 — Data/APDU | Stage 1 tests and SPEC_WINS requirement references | Selection and all later stages |
| G3 — Kernel flow | Stages 2–6 strict transition and negative tests | Host and contact E2E |
| G4 — Contactless | Independent Stage 9 sequence/outcome tests | Contactless L3 cases |
| G5 — Persistence | Atomic original invariants and append-only audit | Restart/recovery gates |
| G6 — Host | Strict response parsing, issuer phases and durable idempotency | Online completion |
| G7 — Reversal/recovery | Late query/reversal, safe retry and unsafe retry blocked | Transaction engine closure |
| G8 — Restart/idempotency | Two-process convergence and crash-after-send reconciliation | Durable terminal closure |
| G9 — Security/redaction | No sensitive values in database, traces, evidence, errors or replay | Every evidence gate |
| G10 — L3 matrix | Real Internal adapter identity and complete per-interface matrix | L3 LAB readiness |
| G11 — Legacy | Original 94 tests plus all later regression tests pass | Every merge |
| G12 — External blockers | Physical validation and certification visibly pending | Any physical/certified claim |

## Immediate next action

Start **K01 — DOL and atomic EMV DataStore**, together with the K02 APDU/error contract review required to consume it. Do not start by expanding the L3 fake, adding scheme names, implementing cryptography with unknown fixtures or changing UI readiness labels. The first implementation increment must include requirement references, negative tests, redaction tests and preservation of the full regression baseline.
