import unittest

from card_studio.model import (
    CardError,
    CardState,
    amount_apdu,
    parse_amount,
    parse_state_response,
    read_apdu,
    transition,
    validate_card_id,
    write_id_apdu,
)


class ModelTests(unittest.TestCase):
    def test_validates_identifier_and_amount(self):
        self.assertEqual(validate_card_id(" sl-caja-001 "), "SL-CAJA-001")
        self.assertEqual(parse_amount("$ 17.25"), 1725)
        with self.assertRaises(CardError):
            validate_card_id("4111111111111111")
        with self.assertRaises(CardError):
            parse_amount("1.234")

    def test_financial_transitions_are_preserved(self):
        state = CardState("SL-CAJA-001", 0)
        state = transition(state, "RELOAD", 5000)
        state = transition(state, "CHARGE", 1725)
        self.assertEqual(state.balance_minor, 3275)
        state = transition(state, "WRITE_ID", identifier="SL-NUEVA-001")
        self.assertEqual(state.identifier, "SL-NUEVA-001")
        self.assertEqual(state.balance_minor, 3275)
        with self.assertRaises(CardError) as caught:
            transition(state, "CHARGE", 4000)
        self.assertEqual(caught.exception.code, "INSUFFICIENT_FUNDS")

    def test_apdu_contract_is_own_applet_only(self):
        self.assertEqual(read_apdu(), "80CA000200")
        self.assertEqual(amount_apdu("RELOAD", 1725), "80D0000004000006BD")
        self.assertEqual(amount_apdu("CHARGE", 1725), "80D1000004000006BD")
        self.assertTrue(write_id_apdu("SL-CAJA-001").endswith("SL-CAJA-001".encode().hex().upper()))

    def test_parses_own_card_state(self):
        state = parse_state_response(b"SL1|SL-CAJA-001|00000CCB|MXN")
        self.assertEqual(state.balance_minor, 3275)
        with self.assertRaises(CardError):
            parse_state_response(b"invalid")


if __name__ == "__main__":
    unittest.main()
