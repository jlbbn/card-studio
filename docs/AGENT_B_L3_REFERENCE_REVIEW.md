# Agent B — EMV/L3 reference review and gap analysis

## Scope, method and decision rules

This is a **static RESEARCH + GAP ANALYSIS** of source files and tests at the commits listed below. It is not an EMV conformance assessment, an L3 approval, a certification statement, or proof that any test suite passes. No source code, card profile, credential, key, APDU trace, or test vector is incorporated into Card Studio.

The review distinguishes implementation from documentation and terminal-side code from card-side simulation. A mock/fake proves only the behavior scripted by that mock/fake. `SPEC_WINS` applies whenever a repository behavior, comment, or document conflicts with the applicable EMVCo/scheme/acquirer specification. Repository documentation is not normative authority. Applicable licensed specifications and bulletin versions still require a separate review.

Status values used below: `EXISTS`, `PARTIAL`, `MISSING`, `BLOCKED`, `NOT_REQUIRED`, and `REVIEW_REQUIRED`. Priorities: P0 = correctness/evidence blocker, P1 = required next engineering increment, P2 = hardening/integration, P3 = optional follow-up.

Finding classes used below: `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE`, `CARD_SIMULATOR_REFERENCE`, and `DO_NOT_REUSE`.

## 1. Terminal architecture review

### 1.1 Repositories actually inspected

| Repository | Branch and immutable commit inspected | Concrete implementation and test paths |
|---|---|---|
| https://github.com/mrautio/emvpt | `master`, `74e3442e3b82b2bc92a25fce73ea937a81d92fd5` | `emvpt/src/lib.rs` (`ApduInterface`, `ApduHook`, `EmvConnection`, `TransactionState`, transaction steps and inline `#[test]` modules); `terminalsimulator/src/main.rs` (`SmartCardConnection`, `purchase`, CLI); `emvpt/test_data.yaml`; `LICENSE` |
| https://github.com/vicente-da-silva/dcemv | `master`, `7a8ee22e434563af0703b05a679e04a1cc44e55f` | `DCEMV_EMVProtocol/EMVCard/Terminal/EMVTerminalApplicationBase.cs`; `DCEMV_EMVProtocol/EMVCard/Terminal/TransactionRequest.cs`; `DCEMV_EMVProtocol/EMVCard/KernelShared/KernelBase.cs`; `DCEMV_TLVProtocol/IOnlineApprover.cs`; `DCEMV_SimulatedPaymentProvider/SimulatedPaymentProvider.cs`; `DCEMV_Shared/Interfaces/ICardReaderHAL.cs`; `DCEMV_DemoServer/Controllers/Api/TransactionController.cs`; `LICENSE` |
| https://github.com/mrautio/emv-card-simulator | `master`, `761321e437f2cd2237113fb615b709dc3f17ae72` | `src/main/java/emvcardsimulator/{EmvApplet,PaymentApplication,PaymentSystemEnvironment,ProximityPaymentSystemEnvironment,EmvTag,Fault}.java`; `src/test/java/emvcardsimulator/{FaultInjectionTest,ContactlessTransactionTest,PaymentApplicationProtocolTest,PaymentSystemEnvironmentTest}.java`; `LICENSE` |
| https://github.com/tomkp/emv | `master`, `ce9f62771f558bc66a4f456c2905b9d62570a7dc` | `src/emv-application.ts`; `src/commands.ts`; `src/cli.ts`; `src/emv-application.test.ts`; `src/commands.test.ts`; `src/cli.test.ts`; `src/emv-tags.test.ts`; `src/interactive/index.tsx`; `src/interactive/screens/ReadersScreen.tsx`; `package.json` |
| https://github.com/nyimbi/Hyperion-X-EMV | `main`, `9fe50cb9281cd8699b7a94a8df3a51631c22718e` | `include/hyperion_emv.h`; `src/{ffi,fsm,gac,issuer,selection,terminal,trace,evidence}.rs`; `tests/traceability_foundation.rs`; `LICENSE` |

The shallow clone was used only for inspection. Commit URLs for reproducibility:

- `https://github.com/mrautio/emvpt/tree/74e3442e3b82b2bc92a25fce73ea937a81d92fd5`
- `https://github.com/vicente-da-silva/dcemv/tree/7a8ee22e434563af0703b05a679e04a1cc44e55f`
- `https://github.com/mrautio/emv-card-simulator/tree/761321e437f2cd2237113fb615b709dc3f17ae72`
- `https://github.com/tomkp/emv/tree/ce9f62771f558bc66a4f456c2905b9d62570a7dc`
- `https://github.com/nyimbi/Hyperion-X-EMV/tree/9fe50cb9281cd8699b7a94a8df3a51631c22718e`

### 1.2 Reference architectures verified in source

**emvpt — terminal library plus PC/SC terminal simulator.** `ApduInterface.send_apdu` is the narrow transport boundary and `ApduHook` can alter commands/responses for fault testing. `EmvConnection` owns card/terminal tags, TVR/TSI, APDU exchanges and explicit transaction steps. `terminalsimulator/src/main.rs::purchase` composes selection, settings, GPO, ODA, restrictions, CVM, risk, action analysis and GENERATE AC. It is a real step-oriented terminal implementation, not merely documentation. It does not provide Card Studio's durable operation store, idempotency keys, restart recovery, refund model, or a production authorization client. Classification: `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE`.

**dcemv — event/queue-driven terminal and kernels.** `EMVTerminalApplicationBase` coordinates UI, cancellation, card events and kernel queues. `KernelBase` defines explicit states/signals/actions. `IOnlineApprover` separates `DoAuth`, status check, advice and reversal; `EMVApproverResponse` carries 8A, 91, 71 and 72. These are useful boundary patterns. However, `SimulatedApprover.DoReversal`, `DoAdvice`, and EMV `DoCheckAuthStatus` throw `NotImplementedException`, so those paths are not working references. No conventional unit-test project or test methods were found in the inspected tree; `DCEMV_PersoApp/DCEMV_PersoApp/Views/TestView.xaml.cs` is a UI view, not a transaction test suite. Classification: architecture only, with `DO_NOT_REUSE` for core/code due to AGPL.

**emv-card-simulator — ICC/card-side implementation, not a terminal application.** `EmvApplet` and `PaymentApplication` implement SELECT, GPO, READ RECORD, GENERATE AC, issuer scripts and card state. `Fault` and `EmvTag` implement delay, status-word, value and encoding mutations. The JUnit tests exercise a Java Card simulator and, in some tests, cryptographic card behavior. This is a `CARD_SIMULATOR_REFERENCE` and `TEST_REFERENCE`; it must not be mistaken for a host or terminal persistence implementation.

**tomkp/emv — PC/SC explorer and simplified transaction helper.** `EmvApplication` wraps reader/card operations. `performTransaction` composes GPO → record reads → GENERATE AC with mocked tests, but it does not implement the full terminal action, host authorization, reversal, recovery, or durable orchestration found in Card Studio. `commands.ts` provides injectable `DevicesLike`/`EmvLike` boundaries and a card-wait timeout. The interactive UI is a reader/card explorer. Classification: `DIAGNOSTIC_REFERENCE`, `TEST_REFERENCE`, limited `ARCHITECTURE_REFERENCE`.

**Hyperion-X-EMV — explicit C ABI kernel boundary and stepwise host handoff.** `include/hyperion_emv.h` defines runtime callbacks, minor-unit transaction parameters, `krn_run_transaction`, online-data extraction, host-response application, issuer authentication, scripts and final GENERATE AC. `src/fsm.rs` names host/APDU timeout and card removal events. `src/issuer.rs` parses 8A/89/91/71/72, rejects duplicates/nesting/unsupported objects and tracks script outcomes. `src/evidence.rs` is explicitly a checklist/report aid, not an approval engine. No durable terminal operation store or acquirer client was established from these paths. Classification: `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`.

### 1.3 Exact Card Studio correspondence

| Reference boundary | Card Studio equivalent | Assessment |
|---|---|---|
| emvpt `purchase` / Hyperion stepwise C ABI | `card_studio/application.py::CardStudioApplication` and `transactions/service.py::TransactionService` | Facade and orchestration exist and are more durable than the references. Host/issuer semantics are narrower. |
| emvpt `ApduInterface`; dcemv `ICardReaderHAL`; tomkp `DevicesLike` | `readers/base.py::ReaderAdapter`, `interfaces/base.py::CardInterface` | Appropriate abstraction separation. UNKNOWN reader capability is correctly blocked. Physical hotplug behavior is not L3 evidence. |
| emvpt `EmvConnection`; dcemv `KernelBase`; Hyperion `KrnContext` | `kernels/base.py::KernelAdapter`, resolved by `TransactionService` | Service does not branch on Mock/Internal. The L3 default `LabKernelAdapter` is a contractual fake, not an EMV kernel. |
| dcemv `IOnlineApprover`; Hyperion host-response functions | `host/base.py::AuthorizationHostAdapter`, `host/mock.py::MockAuthorizationHost` | Persistent mock boundary exists. It returns a simplified decision, not an EMV authorization response with 8A/91/scripts. |
| emvpt `TransactionState`; Hyperion `KrnTxnParams` | `transactions/model.py::{TransactionRequest,TransactionResult}` and `money.py::Money` | Minor units, currency/exponent, operation/original references and execution/recovery states exist. Acquirer-specific fields are not modelled. |
| Card simulator applets and fault injection | `l3/scenarios.py::LabKernelAdapter`, `MockReaderAdapter` | Current fake scripts outcomes and selected failures; it is not an ICC simulator and has no card-side persistent counters/torn-transaction state. |
| Reference unit/fault tests | `l3/{runner,scenarios,assertions,evidence,model}.py`, `tests/test_agent_b_*.py` | Useful LAB harness with real assertions over the facade. Its 27-case result is not independent EMV/L3 proof. |

## 2. Host boundary

Card Studio has a genuine adapter seam: `authorize`, `query`, `reverse`, and `cancel`. `MockAuthorizationHost` persists request digest/response in its own SQLite database, distinguishes failure before send from timeout after send, and returns only `SIMULATED`. This is stronger operationally than the simple online simulation in `emvpt/terminalsimulator/src/main.rs`.

The principal gap is **authorization payload semantics**. Current host responses contain `execution_state`, `functional_result`, `decision`, `outcome`, recovery and simulation markers. They do not model:

- Authorisation Response Code (8A) and acquirer-specific approval mapping;
- Issuer Authentication Data (91), external authentication and its failure outcome;
- Issuer Script Templates 71/72, phase, per-command status and TVR/TSI effect;
- malformed, duplicate, nested or unsupported host response objects;
- explicit online-decline and unable-to-go-online fallback behavior such as the Y3/Z3 handling demonstrated by emvpt.

Concrete references are `dcemv/DCEMV_TLVProtocol/IOnlineApprover.cs`, `Hyperion-X-EMV/src/issuer.rs::parse_host_response`, `emvpt/emvpt/src/lib.rs::online_authorisation_decision`, and tests `Hyperion-X-EMV/tests/traceability_foundation.rs::host_response_extracts_arpc_and_phase_specific_script_results`. These are `ARCHITECTURE_REFERENCE`/`ALGORITHM_REFERENCE`, not evidence that Card Studio supports those behaviors. Any expanded raw-TLV kernel response would also require an Agent A contract review; no contract change is assumed here.

## 3. Transaction lifecycle

The current lifecycle in `TransactionService` is explicit: validate exact profile and capabilities → create durable record → open reader/interface/kernel → mark IN_PROGRESS → execute → optionally authorize/complete online → classify → persist result/evidence → close best-effort. `application.py` exposes that lifecycle without leaking sessions or APDU payloads.

`emvpt` offers the closest terminal-step reference: selection is not the whole transaction; settings, ODA, restrictions, CVM, risk and action analysis precede cryptogram completion. Hyperion additionally separates online package construction, host application, issuer authentication, scripts and final outcome. `tomkp/emv::performTransaction` is only a simplified GPO/read/GAC chain and is not a full lifecycle reference.

Card Studio's durable orchestration is `EXISTS` for LAB. Full EMV behavior is `PARTIAL`: `LabKernelAdapter` selects a profile AID and maps scenarios, but it does not execute the reference steps. `InternalKernelTests` cover the separate Agent A laboratory kernel; that coverage must not be relabelled as coverage of the L3 runner's fake.

## 4. Recovery

Current recovery guarantees are meaningful and tested: no automatic resend, FAILED+SAFE_TO_RETRY only, UNCERTAIN preserved on unknown query, crash IN_PROGRESS becomes UNCERTAIN+READ_BACK_REQUIRED on explicit recovery, and known results can be returned by an explicit query. Relevant tests are `test_agent_b_terminal.py::{test_crash_in_progress_requires_explicit_readback_recovery,test_restart_unknown_query_conserves_uncertain,test_timeout_is_uncertain_and_explicit_query_recovers}` and `test_agent_a_kernel.py::test_result_loader_recovers_known_result_after_restart`.

The gap is reconciliation with an independent source of truth after restart/host timeout. The mock host persists its last uncertain response and `query` returns it; there is no test of a later final host response observed by a new application/kernel instance. Nor is there a facade-injected durable result loader equivalent to the Agent A unit test. Recovery therefore remains `PARTIAL`, not automatic success. `emv-card-simulator` `recoverAcTest` concerns card-side torn transactions and is a separate recovery domain; it must not be conflated with terminal SQLite recovery.

## 5. Reversal and refund

`TransactionService._validation_failure` verifies the original approved purchase, original amount/currency/exponent, cumulative refunds and duplicate reversal. `host.reverse` is separate from `authorize`. Tests cover partial refunds, excessive refunds, original references, successful reversal and duplicate reversal. This is `EXISTS` for local LAB rules.

It is `PARTIAL` for host/acquirer behavior. The mock simply stores the same generic approval shape; there is no acquirer reversal/reference message, original host identifier, late reversal outcome, or independent refund operation. `dcemv/IOnlineApprover.cs::DoReversal` is a useful boundary name, but `SimulatedApprover.DoReversal` is unimplemented and provides no behavioral proof. No inspected primary repository supplies a working partial-refund implementation to copy. The cumulative check also lacks a cross-process concurrent refund test; the existing single-process sequential test does not establish atomicity under competing application instances.

## 6. Persistence

`transactions/store.py` provides versioned SQLite, `BEGIN IMMEDIATE`, canonical request/result records, original references, timestamps, result references and append-only audit triggers. `host/mock.py` has a separate database. Tests cover restart replay, audit update/delete rejection and evidence permissions. These components are `EXISTS` for the declared LAB scope.

Reference projects mostly do not address this layer. dcemv has server repositories, but its demo controller is not a terminal crash-recovery contract and is AGPL. Hyperion evidence/provenance helpers do not replace transactional persistence. Gaps are cross-process original-limit atomicity, deterministic reconciliation after external resolution, and binding evidence to a run manifest/profile/kernel/host version set. No hash chain or signed audit claim is made or required for current LAB.

## 7. Idempotency

Canonical digest excludes operation/idempotency/correlation identifiers, same key+canonical returns the stored record, a changed canonical request raises `IDEMPOTENCY_CONFLICT`, and the mock host applies the same persistent rule. Same-application concurrent duplicate execution is tested and executes once. This is `EXISTS` for tested conditions.

It is `PARTIAL` across processes: uniqueness prevents duplicate inserts, but a competing caller can observe CREATED/IN_PROGRESS rather than the eventual final result. There is no two-application barrier test proving final-result convergence, and no concurrent refund/reversal test proving original-level invariants. Hyperion and emvpt do not provide an idempotency model to substitute for this work.

## 8. Simulator and test harness

`L3Runner` executes scenarios through the facade and writes per-case assertions/transitions plus gates. This is a valid **service-level LAB harness**. Its default `LabKernelAdapter` is a fake: CONTACT and CONTACTLESS share scenario logic, malformed TLV is a profile-triggered `KernelError`, and reader/card removal is injected by Python fakes. Therefore a passing matrix demonstrates only that these scripted contracts pass.

References for expansion:

- `emvpt/emvpt/src/lib.rs::test_malformed_card_responses`: broad truncation/mutation/no-panic fuzz-like behavior.
- `emv-card-simulator/src/test/java/emvcardsimulator/FaultInjectionTest.java::{tagEncodingTest,threeByteTagTest,delayTest}`: concrete card-side encoding and timing faults.
- `emv-card-simulator/src/test/java/emvcardsimulator/ContactlessTransactionTest.java::{qvsdcOnlineTest,recoverAcTest,recoverAcNotMatchingTest}`: contactless card behavior and torn recovery.
- `tomkp/emv/src/emv-application.test.ts` multiple-application and transaction-flow mocks: useful for CLI/diagnostic patterns, not full EMV proof.
- Hyperion `tests/traceability_foundation.rs`: host-response, FSM, callback and masking tests. Its documentation/trace packages are not approval evidence.

P0 harness gap: the 27-case L3 suite has no required execution against the actual injected Internal kernel and no independent reference-trace replay. P1 simulator gap: there is no stateful synthetic ICC with PSE/PPSE directory, AFL records, ATC/counters, contactless qVSDC behavior, torn state, or fault timing.

## 9. Physical diagnostics

`emvpt/terminalsimulator/src/main.rs::SmartCardConnection` and `tomkp/emv/src/commands.ts::{listReaders,waitForCard}` provide concrete PC/SC diagnostics patterns: enumeration, card presence, connect/transmit errors, interface selection and wait timeout. dcemv separates HAL calls and card-removed events. These are `DIAGNOSTIC_REFERENCE` only.

Card Studio has `ReaderAdapter`, PC/SC wrappers, dry-run hardware validation and tests such as `test_pcsc_hardware.py::test_card_removed` and `test_hardware_validation.py::test_all_controlled_scenarios_and_evidence`. Those tests use fakes/controlled scenarios. They do not show a real reader, card, firmware or L1 result. Current `physical_validation=NOT_EXECUTED/PENDING` is the correct boundary. Real hardware and acquirer access are `BLOCKED` external dependencies and `NOT_REQUIRED` for this LAB-only review; they must not be represented as L3 readiness.

## 10. UI/CLI patterns

Current CLI exposes transaction create/get/query/cancel/pending plus kernel/L3 readiness through the facade. The GTK UI uses `transactions/view.py`: UNCERTAIN gets Query/Review, only FAILED+SAFE_TO_RETRY gets Retry, and SUCCEEDED has no Retry. LAB readiness is separated from certification and physical validation.

`test_agent_b_terminal.py::test_cli_facade_create_get_query_pending_cancel_and_readiness` patches the application with the fake. It is not a subprocess test of default dependency resolution. UI coverage checks the pure view model, not actual GTK signal flows. Coverage is `PARTIAL`.

Useful reference patterns are tomkp's injectable CLI contexts and JSON output tests, and emvpt's explicit `--stop-after-connect`, `--stop-after-read`, interface and censoring options. Sensitive-input examples such as PIN options are not patterns to adopt. A future P2 increment can add subprocess CLI coverage and headless view-signal tests without exposing card/host internals.

## 11. P0/P1 gaps and AGENT_B_GAP_RESULT

### P0 — correctness/evidence blockers

1. **Host authorization response handling is simplified.** Add a reviewed LAB representation/tests for 8A decision mapping, online decline/fallback, 91 issuer authentication, 71/72 phase outcomes and malformed responses. Shared kernel changes are `BLOCKED` pending an Agent A contract decision; `SPEC_WINS`.
2. **L3 evidence is fake-backed.** Require the same facade matrix against the real injected kernel, identify the adapter in every result, and keep fake results visibly non-conformant. Do not convert the existing 27/27 fake result into an L3 claim.
3. **Contactless-specific behavior is not proven by shared fake scenarios.** Cover cryptogram-in-GPO and no second GENERATE AC where applicable, contact versus contactless online completion, and interface-specific removal/outcome behavior. Kernel ownership may be Agent A; the harness/matrix gap is owner B.

### P1 — next engineering increments

- Durable late host reconciliation after restart/timeout and a test where an independent host result becomes known.
- Cross-process idempotent final-result convergence and atomic original-level refund/reversal limits.
- PSE/PPSE multi-AID directory tests, unknown-tag tolerance and mutation/no-panic coverage beyond one profile-triggered error.
- Stateful synthetic ICC/fault injection and torn-transaction tests, clearly separated from terminal recovery.
- Run-manifest binding for profile/kernel/host/terminal versions and sanitized, replayable reference scenarios.
- Subprocess CLI and actual UI action coverage; these can be P2 if P0/P1 host/harness work is incomplete.

### AGENT_B_GAP_RESULT

| Component | Current | Target | Gap | Priority | Reference |
|---|---|---|---|---|---|
| Terminal application facade | EXISTS (LAB) | Full orchestration with richer host steps | Host/issuer phases not represented | P1 | emvpt `terminalsimulator/src/main.rs::purchase`; Hyperion `include/hyperion_emv.h` (owner B) |
| Transaction model | EXISTS | Durable request/result plus required host references | No acquirer response identifiers | P2 | Hyperion `src/ffi.rs::KrnTxnParams` (owner B) |
| Money | EXISTS | Minor-unit amount/currency/exponent and original amounts | Cross-currency/acquirer edge policy review | P2 | dcemv `TransactionRequest.cs`; Hyperion `KrnTxnParams` (owner B) |
| Profile | PARTIAL | Exact LAB digest now; authoritative signed scheme profile if required later | Signed authority/provenance and bulletins | P1 | Hyperion `src/config.rs`, `src/evidence.rs`; Agent A + external authority |
| Reader abstraction | EXISTS (mock/PCSC wrappers) | Capability-safe physical diagnostics | Hotplug/device proof absent | P2 | emvpt `SmartCardConnection`; tomkp `DevicesLike` (owner B diagnostics) |
| Interface abstraction | PARTIAL | Interface-specific protocol and outcome behavior | Shared fake does not prove differences | P0 | emvpt `handle_2nd_generate_ac` (Agent A/B boundary) |
| Kernel adapter | EXISTS (contract) | Stable selection/APDU/host/FSM boundary | L3 fake is not an EMV kernel | P0 | dcemv `KernelBase.cs`; Hyperion `KrnContext` (Agent A contract, owner B harness) |
| Host adapter | PARTIAL | Authorization/query/reversal with EMV response semantics | Only simplified decision mapping | P0 | dcemv `IOnlineApprover.cs`; Hyperion `src/issuer.rs` (owner B) |
| Authorization | PARTIAL | 8A/91/scripts, decline/fallback and malformed response handling | Response fields and tests missing | P0 | emvpt `online_authorisation_decision`; Hyperion `parse_host_response` (owner B + Agent A) |
| Refund | PARTIAL | Original-linked, cumulative and host-reconciled refunds | No independent host refund/late result; race untested | P1 | No working primary-reference implementation; current B tests (owner B) |
| Reversal | PARTIAL | Original-linked reversal with outcome/query recovery | Generic mock response; acquirer linkage absent | P1 | dcemv `IOnlineApprover.DoReversal` boundary only; implementation throws (owner B) |
| Idempotency | EXISTS / PARTIAL | Final-result convergence across processes | Two-process convergence and original-level races | P1 | Current SQLite/store tests; no reference substitute (owner B) |
| Persistence | EXISTS (LAB) | Atomic operations and original invariants after restart | Cross-process original invariant evidence | P1 | Current `transactions/store.py` (owner B) |
| Recovery restart | PARTIAL | Independent host/kernel reconciliation after restart | Late final host response not tested | P1 | Agent A result-loader test + current B recovery tests (owner B) |
| Audit | EXISTS | Reproducible run-linked append-only history | Run manifest/external trace linkage | P2 | Hyperion `src/trace.rs` and `src/provenance.rs` (owner B) |
| Evidence | PARTIAL | Sanitized replayable scenarios bound to exact adapters | Fake/Internal evidence not distinguished as a release gate | P0 | Hyperion `src/evidence.rs` is checklist only (owner B) |
| Contact transaction | PARTIAL | Actual kernel contact lifecycle through facade | L3 matrix uses shared fake | P0 | emvpt `test_purchase_transaction`, `test_step_by_step_transaction` (owner B harness) |
| Contactless transaction | PARTIAL | Contactless-specific GPO/online/outcome behavior | No second-GAC/qVSDC/torn coverage in B | P0 | emvpt `test_contactless_arqc_has_no_second_generate_ac`; simulator `ContactlessTransactionTest` (owner B harness) |
| Terminal simulator | PARTIAL | Step-driven terminal simulator with host boundary | Fake scenario mapper only | P1 | emvpt `EmvConnection` + `terminalsimulator` (owner B) |
| Card simulator | MISSING | Stateful synthetic ICC with counters and faults | No PSE/PPSE/AFL/ATC/torn card model | P1 | `emv-card-simulator/src/main/java/emvcardsimulator/PaymentApplication.java`; `emv-card-simulator/src/main/java/emvcardsimulator/Fault.java` (owner B/reference only) |
| Reader diagnostics | PARTIAL / BLOCKED physical | Real enumeration/hotplug evidence when hardware is authorized | Only fake/controlled tests | P2 | tomkp `commands.ts`; emvpt `main.rs`; external hardware blocker |
| L3 runner/scenarios/assertions/evidence | PARTIAL | Same matrix against real injected kernel and reference traces | No required Internal run; fake gates only | P0 | Current `l3/*.py`; Hyperion test style (owner B) |
| CLI | PARTIAL | Default-resolution subprocess E2E and error/recovery paths | Existing B test patches facade | P2 | tomkp `src/cli.test.ts`, `src/commands.test.ts` (owner B) |
| UI | PARTIAL | Headless action/signal tests for Query/Review/Retry/readiness | Pure view-model coverage only | P2 | Current `transactions/view.py`; tomkp interactive tests (owner B) |
| Readiness gates | PARTIAL | Gates tied to real adapter evidence and external blockers | Fake 27/27 must remain non-conformant | P0 | Hyperion `src/evidence.rs` distinction (owner B) |
| Host timeout/recovery | PARTIAL | Later final host response without blind reversal/resend | Static uncertain mock only | P1 | Hyperion FSM `HostTimeout`; current B host tests (owner B) |
| Physical validation | BLOCKED / NOT_REQUIRED for LAB | Separate authorized device/L1 evidence | Hardware, firmware, lab and acquirer unavailable | P2 | Hyperion `src/evidence.rs`; external owner, no L3 claim |
| Specification authority | REVIEW_REQUIRED | Applicable licensed EMVCo/scheme/acquirer requirements | Repositories are not normative and may deviate | P1 | `SPEC_WINS`; licensed-spec review required |

## 12. Licensing notes

### 12.1 Repository license decisions

| Repository | License | Can inspect? | Can derive architecture? | Can reuse code? | Conditions? | Risk? |
|---|---|---|---|---|---|---|
| `mrautio/emvpt` | Zlib (`LICENSE`, copyright Mika Rautio) | Yes | Yes, clean-room architecture/behavior review | Conditionally; none copied here | Preserve notice in source distributions; mark altered sources; do not misrepresent origin | Low licensing risk; medium technical risk from relying on non-authoritative behavior |
| `vicente-da-silva/dcemv` | AGPL-3.0-or-later (`LICENSE` and source headers) | Yes | High-level architecture only | **No — DO_NOT_REUSE core/code for this task** | AGPL copyleft/source and network-use obligations would require legal review; no code or derived core incorporated | High copyleft/contamination risk; also several methods unimplemented |
| `mrautio/emv-card-simulator` | Zlib (`LICENSE`) | Yes | Yes, card simulator and fault-model concepts | Conditionally; none copied here | Same Zlib notice/alteration/origin conditions | Low licensing risk; high misuse risk if card-side behavior is mistaken for terminal proof |
| `tomkp/emv` | MIT declared in `package.json`; no root LICENSE file at inspected commit | Yes | Yes | **REVIEW_REQUIRED / do not reuse until full license notice/provenance is confirmed** | MIT requires copyright and permission notice in copies/substantial portions; declaration alone does not supply the root text | Medium license/provenance risk; simplified implementation also unsuitable as full-kernel proof |
| `nyimbi/Hyperion-X-EMV` | MIT with full `LICENSE` text | Yes | Yes | Conditionally with copyright+permission notice; none copied here | Preserve notices; independently validate algorithms and fixture provenance | Medium technical/provenance risk; repository itself distinguishes pending external approval artifacts |

### 12.2 Finding and data classification

- `emvpt`: architecture, algorithms, tests and PC/SC diagnostics references. `test_data.yaml` behavior is `TEST`; private-key/config material and production-like settings are `UNKNOWN` for this task and were not incorporated.
- `dcemv`: `ARCHITECTURE_REFERENCE` plus `DO_NOT_REUSE`. Demo server/provider values, endpoints and cryptographic material are `UNKNOWN`; none are candidates for Card Studio.
- `emv-card-simulator`: `CARD_SIMULATOR_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`. JUnit behavior is `TEST`; card setup files, private keys, personalizations and private-card data are `UNKNOWN` and must not be imported.
- `tomkp/emv`: `DIAGNOSTIC_REFERENCE` and `TEST_REFERENCE`. Inline mocked values are `TEST`; any PAN-like/track/cryptographic example remains `UNKNOWN` for reuse and was not incorporated. Code reuse is pending the license-file review above.
- `Hyperion-X-EMV`: `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`. Inline unit vectors are `TEST`; certification/profile/CAPK/ODA/trace bundles are `UNKNOWN` unless independently authorized and verified. Documentation and generated checklists are not implementation or approval evidence.

No repository behavior overrides EMVCo. No code, keys, profiles, vectors or unknown-provenance data were reused. No L3-ready, compliance, or certification conclusion follows from this review.
