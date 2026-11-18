# Punto de control congelado — Card Studio EMV P1

Fecha: 2026-11-08

## Baseline preservada

- P0: **35/35 PASS**.
- P1: **29/29 PASS**.
- L3 core InternalEmvKernel: **27/27 PASS, G1–G10 PASS**.
- L3 hardened P1: **20/20 PASS**.
- Full regression: **361/361 PASS**.
- Remaining P1: **NONE**.
- Formal certification: **NOT COMPLETED**.
- Physical validation: **NOT EXECUTED**.

## Hashes de congelación

Alcance `code_tests`: `card_studio/**/*.py` y `tests/**/*.py`, rutas y contenido ordenados.

```text
056401ecbd55d750ae95356d127d256dc0b041f27aedd482c3f6c49c1a1642ef
```

Alcance `evidence`: `docs/*.md` y `PUNTO_DE_CONTROL_EMV_P0.md`.

```text
5d9e9ab781915c2274ceb73504031a0c9b099f729f47a1a1c5d3747ff19c3ea4
```

## Regla P2

Esta baseline es histórica y no debe reescribirse. Todo cambio P2 debe identificarse en nuevos archivos de evidencia y conservar P0/P1, la matriz L3 y la regresión mínima de 361 pruebas. Cualquier regresión exige STOP/FIX/RERUN antes de continuar.
