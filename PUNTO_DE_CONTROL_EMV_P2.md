# Punto de control — Card Studio EMV P2

Fecha: 2026-11-08

## Baselines históricas preservadas

- P0: **35/35 PASS**.
- P1: **29/29 PASS**.
- L3 hardened: **20/20 PASS**.
- Baseline inicial P2: **361/361 PASS**.
- P1 code/tests hash: `056401ecbd55d750ae95356d127d256dc0b041f27aedd482c3f6c49c1a1642ef`.
- P1 evidence hash: `5d9e9ab781915c2274ceb73504031a0c9b099f729f47a1a1c5d3747ff19c3ea4`.

No se reescribieron resultados históricos P0/P1.

## Resultado P2

- Integration P2 (T24–T30): **7/7 PASS**.
- Physical Validation Preparation: **PASS**.
- Physical Validation: **NOT_EXECUTED**.
- Formal Certification: **NOT COMPLETED**.
- Full Regression: **377/377 PASS**.
- Hardened L3: **20/20 PASS**.
- External physical blocker: **PCSC_UNAVAILABLE**.

## Hash P2 de `card_studio/**/*.py` y `tests/**/*.py`

```text
e58e53b39e1156ffbb95cba5444c028d5334271d401a0a4e11d9840d45bae828
```

## Modificaciones P2 identificadas

Nuevos módulos:

- `card_studio/integration/{__init__,physical,chain,backend,sunmi}.py`.
- `docs/EMV_P2_EXECUTION_EVIDENCE.md`.
- `PUNTO_DE_CONTROL_EMV_P1.md`, `PUNTO_DE_CONTROL_EMV_P2.md`.
- `tests/{test_p2_physical,test_p2_integration,test_p2_cli_ui}.py`.

Modificados:

- `card_studio/pcsc.py` — protocolo T=0/T=1.
- `card_studio/readers/pcsc.py` — protocolo en observación/conexión y compatibilidad legacy UNKNOWN.
- `card_studio/cli.py` — `physical-prepare` y errores ReaderError como JSON sanitizado.
- `card_studio/transactions/view.py` — vista CARD separada de CASH.
- `card_studio/host/mock.py` — persistencia compatible de authorize/refund/reverse y late results vinculados.
- `docs/GAP_MATRIX_EMV_REFERENCES.md` — T24–T30 PASS; T31 BLOCKED / PCSC_UNAVAILABLE.

No se ejecutaron escrituras físicas, activación real, operaciones monetarias reales ni cambios destructivos externos. La validación física completa requiere stack PC/SC, lector, Java Card autorizada y host disponibles.
