# EMV P0 execution evidence

Implementation evidence only. `PASS` means the listed synthetic/laboratory implementation, positive and negative tests, applicable integration and regression passed. It does not mean EMVCo certification, physical validation or external scheme approval.

Baseline before P0 execution: 144/144 PASS.

## Block 1 — PASS

Full integrated regression: **196/196 PASS**.

| Gap | Implementation files | Tests | Positive cases | Negative cases | Integration evidence | Status |
|---|---|---|---|---|---|---|
| K01 | `card_studio/emv/data.py`, `dol.py`, `validation.py` | `tests/test_agent_a_p0_block1.py` DataStoreTests, DolTests | PDOL/CDOL1/CDOL2/DDOL/TDOL; ordered exact-length builds; typed padding; atomic merge; metadata/provenance | Missing mandatory data; malformed/duplicate DOL; ownership/provenance conflicts; invalid values; output limits; rollback after every failure; sensitive-value redaction | 37/37 block tests; full regression 196/196 | PASS |
| K02 | `card_studio/emv/apdu.py`, `validation.py` | `tests/test_agent_a_p0_block1.py` ApduTests | All short APDU cases; response parsing; 9000/61xx/6Cxx bounded follow-ups | Malformed Lc/Le; required SW taxonomy including 6A82/6A86/6700/6985/6D00/6E00; chain limits; pre-send versus post-send uncertainty | CardInterface-compatible transport boundary; full regression 196/196 | PASS |
| K03 | `card_studio/emv/selection.py`, `apdu.py` | SelectionTests | Direct AID SELECT and final FCI/DF validation | Duplicate/mismatched FCI; malformed/truncated responses; incorrect final selection | End-to-end exchange through CardInterface | PASS |
| K04 | `card_studio/emv/selection.py` | SelectionTests | PSE `1PAY.SYS.DDF01`; PPSE `2PAY.SYS.DDF01`; Directory Entries | Invalid environment FCI/entries; missing environment; incorrect PPSE record assumption; bounded APDU errors | CONTACT and CONTACTLESS selection E2E using synthetic responses | PASS |
| K05 | `card_studio/emv/selection.py` | SelectionTests | Exact AID; opt-in partial AID with full final SELECT; priority/kernel/interface compatibility | Duplicate candidates; unknown/unsupported kernel; partial ambiguity; multiple candidates require explicit selection | `APPLICATION_SELECTION_REQUIRED`; no arbitrary selection; full regression 196/196 | PASS |

Block 1 closure: **5/35 P0 PASS**. No external blocker. K06–K11 remained OPEN at this checkpoint.

## Block 2 — PASS

Gate: **EMV_DATA_FOUNDATION = PASS**. Targeted: **28/28 PASS**. Full integrated regression: **224/224 PASS**.

| Gap | Implementation files | Tests | Positive cases | Negative cases | Integration evidence | Status |
|---|---|---|---|---|---|---|
| K06 | `card_studio/emv/gpo.py`, `apdu.py` | `tests/test_agent_a_p0_block2.py` GPO tests | PDOL→tag 83→INS A8; strict templates 77/80; mandatory AIP/AFL extraction | Missing objects; malformed/duplicate/ambiguous templates; truncation; bounded APDU errors | SELECT→GPO through CardInterface; 28/28 targeted | PASS |
| K07 | `card_studio/emv/aip.py` | AIP tests | Exact two-byte AIP and typed capabilities | Invalid length and RFU; metadata-only evidence | Parsed within strict GPO response | PASS |
| K08 | `card_studio/emv/afl.py` | AFL tests | Valid SFI/range/offline counts and record plan | Invalid SFI/ranges/counts; overlaps; limits; locators outside AFL | READ RECORD locator accepted only from validated AFL | PASS |
| K09 | `card_studio/emv/record.py`, `data.py` | READ RECORD/atomicity tests | INS B2/P2; template 70; atomic global DataStore merge | Empty/nested/duplicate records; ownership injection; mandatory failure; second-record rollback | AFL→records→DataStore; timeout post-send remains UNCERTAIN | PASS |
| K10 | `card_studio/emv/status.py` | TVR bit-level tests | Five-byte TVR; 26 named flags; set/clear/reset; round-trip | RFU and invalid length/bit tests | Per-transaction status model integrated into data foundation | PASS |
| K11 | `card_studio/emv/status.py` | TSI bit-level tests | Two-byte TSI; six named processing flags; mark/reset; round-trip | RFU and invalid length/bit tests | Flags set only through explicit processing operation | PASS |

Block 2 closure: **11/35 P0 PASS**. New blockers: NONE. K12–K15 remained OPEN at this checkpoint.

## Block 3 — PASS

Gate: **EMV_DECISION_ENGINE = PASS**. Targeted: **28/28 PASS**. Full integrated regression: **252/252 PASS**.

| Gap | Implementation files | Tests | Positive cases | Negative cases | Integration evidence | Status |
|---|---|---|---|---|---|---|
| K12 | `card_studio/emv/restrictions.py` | `tests/test_agent_a_p0_block3.py` restrictions tests | Version, injected terminal date, strict BCD effective/expiry, AUC/channel/interface; TVR/TSI | Invalid/leap dates; malformed/missing objects; ownership; RFU; technical versus functional failure | Data foundation→restrictions with state rollback on error | PASS |
| K13 | `card_studio/emv/cvm.py` | CVM tests | 8E, X/Y, conditions, continue/fail; NO_CVM/SIGNATURE/PIN-related synthetic/CDCVM; 9F34 | Unknown rules; invalid amounts; unsupported CVM; missing external test handles; no PIN capture/storage | Restrictions→CVM with TVR/TSI and redacted evidence | PASS |
| K14 | `card_studio/emv/risk.py` | TRM tests | Floor/exception/type/interface limits; deterministic sample; merchant online | Random boundaries; invalid limits/profile inputs; no brand branches | CVM→TRM with TVR/TSI and deterministic profile data | PASS |
| K15 | `card_studio/emv/taa.py` | TAA tests | TAC/IAC default/denial/online; AAC/TC/ARQC proposals | Invalid lengths/rules; precedence combinations; FAILED never equated to DECLINED | Full DecisionEngine through TAA with state/evidence | PASS |

Block 3 closure: **15/35 P0 PASS**. New blockers: NONE. K16–K18 remained OPEN at this checkpoint.

## Block 4 — PASS

Gate: **EMV_CARD_ACTION_FLOW = PASS**. Targeted: **20/20 PASS**. Full integrated regression: **272/272 PASS**.

| Gap | Implementation files | Tests | Positive cases | Negative cases | Integration evidence | Status |
|---|---|---|---|---|---|---|
| K16 | `card_studio/emv/gac.py`, `cryptogram.py` | `tests/test_agent_a_p0_block4.py` GENERATE AC tests | CDOL1/CDOL2→INS AE; AAC/TC/ARQC P1; strict 77/80; final GAC primitive | Invalid CDOL/P1; duplicate/missing CID/AC/ATC/IAD; malformed responses; post-send UNCERTAIN | DecisionEngine→ODA→GENERATE AC through CardInterface | PASS |
| K17 | `card_studio/emv/cryptogram.py` | Cryptogram tests | AAC→DECLINED, TC→APPROVED, ARQC→ONLINE_REQUIRED; sensitive ownership | AAR/invalid CID; technical failure distinct from decline; full AC excluded from evidence | CardAction returns typed cryptogram result with redacted metadata | PASS |
| K18 | `card_studio/emv/oda.py`, `card_action.py` | ODA boundary tests | CDA/DDA/SDA selection; injectable provider; deterministic NON-CRYPTOGRAPHIC synthetic success/failure | Missing/unknown material; provider errors; BLOCKED/FAILED states; TVR/TSI | ODA boundary invoked before GAC; no production cryptography or external fixtures | PASS |

Block 4 closure: **18/35 P0 PASS**. New blockers: NONE. K19/K20/T02/T03/T04 remained OPEN or IN_PROGRESS at this checkpoint.

## Block 5 — PASS

Gate: **EMV_ONLINE_COMPLETION = PASS**. Targeted A: **11/11 PASS**. Targeted B: **8/8 PASS**. Full integrated regression: **291/291 PASS**.

| Gap | Implementation files | Tests | Positive cases | Negative cases | Integration evidence | Status |
|---|---|---|---|---|---|---|
| K19 | `card_studio/emv/issuer.py`, `online.py`, `kernels/internal_emv.py` | `tests/test_agent_a_p0_block5.py` issuer authentication tests | Strict 91 parsing; EXTERNAL AUTHENTICATE; TVR/TSI; opt-in Internal online FSM | Malformed/missing/contradictory host data; failed or invalid card response; post-send timeout remains UNCERTAIN without replay | Internal→host→EXTERNAL AUTHENTICATE→final result; full regression 291/291 | PASS |
| K20 | `card_studio/emv/issuer.py`, `online.py` | Script phase and failure tests | 71 before final GAC; 72 after final GAC; per-command SW status and redacted history | Malformed scripts; phase-specific failure stops subsequent commands and sets the corresponding TVR bit | Ordered 91→71→CDOL2/final GAC→72 sequence through synthetic CardInterface | PASS |
| T02 | `card_studio/host/response.py`, `mock.py`, `evidence.py`; `card_studio/transactions/evidence.py` | `tests/test_agent_b_host_authorization.py`, `test_agent_b_p0_block5.py` | Durable 8A/91/71/72 templates; references; metadata-only host/transaction evidence | Malformed BER, duplicate/nested objects, invalid ARC/digest; raw payload absent from public result, evidence and transaction DB | MockAuthorizationHost v2 integrated through TransactionService | PASS |
| T03 | `card_studio/host/response.py`; `card_studio/transactions/service.py`; `card_studio/emv/online.py` | ARC/fallback/host failure tests | 00/Y3 approval, 05/Z3 decline or explicit uncertainty; final decision source KERNEL_ARC | Contradictory textual decision cannot override ARC; invalid digest does not complete; timeout has no decline/retry/reversal | Approved, declined, unable and timeout paths through the application facade | PASS |
| T04 | `card_studio/emv/online.py`; `card_studio/kernels/internal_emv.py`, `session.py`, `state.py` | Internal and facade integration tests | ONLINE_REQUIRED→issuer authentication→scripts→CDOL2/final GAC→ARC result; replay without new APDU | Completion conflict; authentication/script/final-cryptogram mismatch; APDU post-send UNCERTAIN | Real InternalEmvKernel→TransactionService→MockAuthorizationHost→complete_online; 291/291 full regression | PASS |

Block 5 closure: **23/35 P0 PASS**. New blockers: NONE. Legacy host-decision completion remains supported only when `online_completion` is not configured. This is synthetic LAB evidence, not EMVCo certification or physical validation. K21–K25 and T01/T05–T10 remained OPEN at this checkpoint.

## Block 6 — PASS

Gate: **EMV_INTERFACE_RUNTIME = PASS**. Targeted: **11/11 PASS**. Full integrated regression: **302/302 PASS**.

| Gap | Implementation files | Tests | Positive cases | Negative cases | Integration evidence | Status |
|---|---|---|---|---|---|---|
| K21 | `card_studio/emv/runtime.py`; `card_studio/readers/base.py`, `mock.py`; `card_studio/kernels/internal_emv.py` | `tests/test_agent_a_p0_block6.py` contact tests | Opt-in PSE→SELECT→GPO→READ RECORD→restrictions/CVM/TRM/TAA→ODA→GAC; T=0/T=1; second GAC online | UNKNOWN contact protocol fails before APDU; malformed/missing record data is technical, never DECLINED | Exact contact APDU order and final ARC result through the real application facade | PASS |
| K22 | `card_studio/emv/runtime.py`, `gpo.py`, `selection.py` | Contactless runtime tests | PPSE/Kernel ID; TTQ in PDOL; CTQ; cryptogram in GPO; one first GAC only when GPO has no cryptogram | Missing/RFU CTQ; no contact sequence reuse; no second GAC after GPO ARQC | PPSE→SELECT→GPO→READ RECORD→online host without GENERATE AC for cryptogram-in-GPO | PASS |
| K23 | `card_studio/emv/contactless.py`, `runtime.py` | C-8 primitive and runtime tests | READY/APPROVED/DECLINED/ONLINE_REQUIRED/TRY_AGAIN/END_APPLICATION; UI metadata and restart policy | Limit ends before APDU; no automatic restart after send/uncertainty; CTQ policy fails closed | C8_OUTCOME bound to contactless cryptogram and final host result | PASS |
| K24 | `card_studio/kernels/state.py`, `internal_emv.py`; stage hooks in `data_foundation.py`, `decision_engine.py`, `card_action.py`, `online.py` | FSM sequence assertions | Explicit APPLICATION_SELECTION through ODA/GAC/issuer/C-8 states; sanitized history | Existing invalid transitions remain rejected; runtime failures cannot skip to approval | Real Internal state history verified for offline, online and contactless flows | PASS |
| K25 | `card_studio/emv/runtime.py`, `gpo.py`; `card_studio/kernels/internal_emv.py` | Malformed data and timeout tests | Typed stage errors retain code/recovery and technical/functional separation | Malformed/missing mandatory data; UNKNOWN protocol; CTQ/limit failures; post-send APDU timeout remains UNCERTAIN and replay does not resend | Full regression 302/302; all previous negative suites remain green | PASS |

Block 6 closure: **28/35 P0 PASS**. New blockers: NONE. `emv_execution` is opt-in; legacy profiles retain their original behavior. Physical T=0/T=1 validation and scheme certification remain unexecuted. T01 and T05–T10 remained OPEN at this checkpoint.

## Block 7 — PASS

Gate: **EMV_REAL_KERNEL_L3_EVIDENCE = PASS**. Targeted: **5/5 PASS**. Real Internal L3 matrix: **27/27 PASS, G1–G10 PASS**. Full integrated regression: **307/307 PASS**.

| Gap | Implementation files | Tests | Positive cases | Negative cases | Integration evidence | Status |
|---|---|---|---|---|---|---|
| T01 | `card_studio/l3/runner.py`, `internal.py`; `card_studio/readers/mock.py` | Real matrix sequence assertions | Instruction-only traces distinguish PSE/contact/GAC from PPSE/contactless/GPO outcomes | Contact sequence is not accepted as contactless evidence; raw APDU payloads are not written | Exact per-interface instruction sequences asserted in the 27-case real-kernel run | PASS |
| T05 | `card_studio/l3/internal.py`, `runner.py`; `card_studio/emv/runtime.py` | `tests/test_agent_b_p0_block7.py` real matrix tests | Actual Internal contact offline/online, first/final GAC, host, reversal/refund and recovery scenarios | Malformed FCI, unsupported AID, invalid state, unsafe retry and uncertain outcomes remain distinct | Same facade matrix against InternalEmvKernel; 27/27 cases and G1–G10 PASS | PASS |
| T06 | `card_studio/l3/internal.py`, `runner.py`; `card_studio/emv/gpo.py`, `contactless.py` | Contactless matrix and Block 6 tests | PPSE/TTQ/CTQ; cryptogram in GPO; online ARC completion without GENERATE AC | GENERATE AC is forbidden in the GPO-cryptogram contactless cases | contactless-approved and contactless-online_required instruction assertions PASS | PASS |
| T07 | `card_studio/l3/runner.py`, `model.py` | Real and fake matrix tests | Injectable Internal factory runs the identical 27 definitions; real adapter identity is mandatory | LabKernelAdapter remains NON_CONFORMANT even with all P0 labels supplied | Real 27/27 PASS; fake 27/27 assertions but NON_CONFORMANT | PASS |
| T08 | `card_studio/l3/evidence.py`, `model.py`; `card_studio/application.py` | Manifest and tamper tests | `manifest.json` binds run/case identity, profile/host/terminal versions and SHA-256 of matrix/readiness/every case; mode 0600 | Missing manifest, changed identity, changed case evidence or changed digest makes facade FAIL | `CardStudioApplication.l3_readiness()` validates persisted bytes before PASS | PASS |
| T09 | `card_studio/l3/model.py`, `runner.py`; `card_studio/application.py` | Missing P0 and readiness tests | PASS requires 27/27, G1–G10, conformant identity, valid manifest and all 35 P0 gates | Real 27/27 without complete P0 stays NON_CONFORMANT; forged/mixed/tampered identities FAIL | Certification remains NOT COMPLETED; physical validation remains PENDING/NOT_EXECUTED | PASS |
| T10 | `card_studio/l3/runner.py`; `card_studio/transactions/service.py`; `card_studio/host/mock.py` | Host timeout case and persistent audit assertions | Durable UNCERTAIN survives explicit query after restart | No DECLINED, ONLINE_COMPLETED, REVERSAL_STARTED, retry or second HOST_AUTHORIZATION_STARTED | Real-kernel host-timeout case PASS with one authorization before and after restart | PASS |

Block 7 closure: **35/35 P0 PASS**. New blockers: NONE within the synthetic P0 scope. This is not EMVCo/scheme certification, production cryptographic proof or physical L1/L3 validation. External specification authority, approved key material, hardware, acquirer and laboratory activities remain separate.

## P1 hardening baseline — FROZEN

P0 remains **35/35 PASS** and its behavior is frozen for P1. The verified starting regression is **307/307 PASS**; InternalEmvKernel L3 remains **27/27 PASS, G1–G10 PASS** and the fake remains **NON_CONFORMANT**. P1 implementation, positive/negative tests, integration and evidence are tracked separately in `docs/EMV_P1_EXECUTION_EVIDENCE.md`. No P1 result changes the certification or physical-validation status.

## P1 hardening closure — P0 PRESERVED

P1 completed **29/29 PASS** without changing any P0 status. Final verification: **361/361 full regression PASS**, core InternalEmvKernel L3 **27/27 PASS, G1–G10 PASS**, hardened P1 matrix **20/20 PASS** and fake **NON_CONFORMANT**. Detailed P1 files, tests, negative cases, integration and evidence are recorded in `docs/EMV_P1_EXECUTION_EVIDENCE.md`. Formal certification remains **NOT COMPLETED** and physical validation **NOT EXECUTED**.
