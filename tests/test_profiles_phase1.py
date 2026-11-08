import unittest
from dataclasses import FrozenInstanceError

from card_studio.money import Money, MoneyValidationError
from card_studio.profiles import (
    CardApplication,
    CardProfile,
    CardProfileRegistry,
    ProfileStatus,
    ProfileValidationError,
)


def profile_values():
    return {
        "schema_version": 1,
        "profile_id": "lab-generic",
        "profile_version": 1,
        "profile_digest": "",
        "scheme_family": "lab-scheme",
        "display_name": "Perfil de laboratorio",
        "applications": (CardApplication("D4534C504D4401", "Laboratorio"),),
        "interfaces": ("CONTACT",),
        "cvm_capabilities": ("NO_CVM",),
        "kernel_requirements": {"simulation": True, "adapter": "MOCK"},
        "scheme_parameters": {"laboratory": True},
        "test_credential_ref": "test://credentials/lab-generic",
        "synthetic_pan_profile": None,
        "tokenization_policy": {"enabled": True, "simulation": True},
        "status": ProfileStatus.APPROVED_FOR_SIMULATION.value,
    }


def make_profile(**changes):
    values = profile_values()
    values.update(changes)
    return CardProfile(**values)


class CardProfileTests(unittest.TestCase):
    def test_profile_is_immutable_and_digest_is_deterministic(self):
        profile = make_profile()
        self.assertEqual(profile.profile_digest, profile.computed_digest())
        self.assertEqual(profile.profile_digest, make_profile().profile_digest)
        self.assertEqual(CardProfile.from_dict(profile.to_dict()).to_dict(), profile.to_dict())
        with self.assertRaises(FrozenInstanceError):
            profile.status = ProfileStatus.DRAFT.value
        with self.assertRaises(TypeError):
            profile.kernel_requirements["simulation"] = False

    def test_profile_digest_changes_with_content_and_rejects_mismatch(self):
        original = make_profile()
        values = original.to_dict()
        values["profile_digest"] = ""
        values["display_name"] = "Otra versión"
        changed = CardProfile.from_dict(values)
        self.assertNotEqual(original.profile_digest, changed.profile_digest)
        values["profile_digest"] = original.profile_digest
        with self.assertRaises(ValueError):
            CardProfile.from_dict(values)

    def test_invalid_aid_is_rejected(self):
        for aid in ["XYZ", "00", "A4"]:
            with self.subTest(aid=aid), self.assertRaises(ProfileValidationError) as caught:
                make_profile(applications=(CardApplication(aid, "Inválido"),))
            self.assertEqual(caught.exception.code, "INVALID_AID")

    def test_duplicate_aid_is_rejected(self):
        with self.assertRaises(ProfileValidationError) as caught:
            make_profile(applications=(
                CardApplication("D4534C504D4401", "Uno"),
                CardApplication("d4534c504d4401", "Dos"),
            ))
        self.assertEqual(caught.exception.code, "DUPLICATE_AID")

    def test_unsupported_interface_is_rejected(self):
        with self.assertRaises(ProfileValidationError) as caught:
            make_profile(interfaces=("MAGNETIC",))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_INTERFACE")

    def test_draft_and_retired_profiles_cannot_execute(self):
        for status in [ProfileStatus.DRAFT.value, ProfileStatus.RETIRED.value]:
            with self.subTest(status=status):
                profile = make_profile(status=status)
                with self.assertRaises(ProfileValidationError) as caught:
                    profile.assert_simulation_enabled()
                self.assertEqual(caught.exception.code, "PROFILE_NOT_EXECUTABLE")
        make_profile().assert_simulation_enabled()

    def test_approved_profile_requires_simulation(self):
        with self.assertRaises(ProfileValidationError) as caught:
            make_profile(kernel_requirements={"simulation": False})
        self.assertEqual(caught.exception.code, "SIMULATION_REQUIRED")

    def test_sensitive_profile_data_is_rejected(self):
        cases = [
            {"scheme_parameters": {"track2": "4111111111111111=2512"}},
            {"scheme_parameters": {"pin": "1234"}},
            {"synthetic_pan_profile": {"synthetic": False, "pan": "4111111111111111"}},
            {"synthetic_pan_profile": {"synthetic": True, "pan": "4242424242424242"}},
            {"test_credential_ref": "production://issuer/key"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ProfileValidationError):
                make_profile(**changes)

    def test_registry_preserves_versions_and_rejects_conflicts(self):
        first = make_profile()
        registry = CardProfileRegistry([first])
        self.assertIs(registry.register(first), first)
        values = first.to_dict()
        values.update({"profile_version": 2, "profile_digest": ""})
        second = CardProfile.from_dict(values)
        registry.register(second)
        self.assertEqual(registry.get("lab-generic").profile_version, 2)
        self.assertEqual(registry.get("lab-generic", 1).profile_digest, first.profile_digest)
        conflict = first.to_dict()
        conflict.update({"display_name": "Conflicto", "profile_digest": ""})
        with self.assertRaises(ProfileValidationError) as caught:
            registry.register_dict(conflict)
        self.assertEqual(caught.exception.code, "PROFILE_VERSION_CONFLICT")


class MoneyTests(unittest.TestCase):
    def test_money_validation_and_round_trip(self):
        money = Money(1725, "MXN", 2)
        self.assertEqual(Money.from_dict(money.to_dict()), money)
        self.assertEqual(Money(0, "JPY", 0).amount_minor, 0)

    def test_invalid_money_is_rejected(self):
        values = [
            (True, "MXN", 2),
            (-1, "MXN", 2),
            (100, "mxn", 2),
            (100, "MX", 2),
            (100, "MXN", True),
            (100, "MXN", 4),
        ]
        for amount, currency, exponent in values:
            with self.subTest(amount=amount, currency=currency, exponent=exponent):
                with self.assertRaises(MoneyValidationError):
                    Money(amount, currency, exponent)


if __name__ == "__main__":
    unittest.main()
