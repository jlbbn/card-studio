from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "GAP_MATRIX_EMV_REFERENCES.md"
EVIDENCE = ROOT / "docs" / "EMV_P1_EXECUTION_EVIDENCE.md"
P0_IDS = {
    *(f"K{number:02d}" for number in range(1, 26)),
    *(f"T{number:02d}" for number in range(1, 11)),
}
P1_IDS = {
    *(f"K{number:02d}" for number in range(26, 41)),
    "T00",
    *(f"T{number:02d}" for number in range(11, 24)),
}


def rows(priority):
    result = {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or f" | {priority} | " not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] in result:
            raise AssertionError(f"Duplicate {priority} gap: {cells[0]}")
        result[cells[0]] = cells[-1]
    return result


class P1ContractTests(unittest.TestCase):
    def test_p0_remains_complete_and_pass(self):
        value = rows("P0")
        self.assertEqual(set(value), P0_IDS)
        self.assertEqual(set(value.values()), {"PASS"})

    def test_p1_register_has_exact_unique_gap_set(self):
        self.assertEqual(set(rows("P1")), P1_IDS)

    def test_every_p1_has_execution_evidence_and_frozen_baseline(self):
        value = EVIDENCE.read_text(encoding="utf-8")
        for gap_id in P1_IDS:
            self.assertIn(f"| {gap_id} |", value)
        self.assertIn("307/307 PASS", value)
        self.assertIn("35/35 PASS", value)
        self.assertIn("27/27 PASS", value)
        self.assertIn("NOT COMPLETED", value)
        self.assertIn("NOT EXECUTED", value)
        self.assertIn("EXTERNAL_UNVERIFIED", value)


if __name__ == "__main__":
    unittest.main()
