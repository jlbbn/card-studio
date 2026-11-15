import json

import unittest

from card_studio.cardlab_adapter import load_cardlab_status
from card_studio.model import CardError


def valid_evidence():
    return {
        "schema": "card-studio-validation/1.0",
        "package_aid": "D4534C504D47",
        "applet_aid": "D4534C504D4701",
        "regression": "PASS",
        "cap": "PASS",
        "verifycap": "PASS",
        "jcardsim": "PASS",
        "container": "PASS",
        "result": "PASS",
        "state": "PHYSICALLY_CONFIRMED",
        "cap_sha256": "abc123",
        "physical": {
            "reader": "Reader 0",
            "select": "PASS",
            "get_info": "PASS",
            "get_status": "PASS",
            "read_container": "PASS",
        },
    }


def test_cardlab_confirmed(tmp_path):
    path = tmp_path / "validate-all.json"
    path.write_text(json.dumps(valid_evidence()))

    result = load_cardlab_status(path)

    assert result["status"] == "PASS"
    assert result["state"] == "PHYSICALLY_CONFIRMED"
    assert result["applet_aid"] == "D4534C504D4701"
    assert result["physically_confirmed"] is True


def test_cardlab_rejects_wrong_aid(tmp_path):
    value = valid_evidence()
    value["applet_aid"] = "D4534C504D9999"

    path = tmp_path / "validate-all.json"
    path.write_text(json.dumps(value))

    with unittest.TestCase().assertRaises(CardError) as caught:
        load_cardlab_status(path)

    assert caught.exception.code == "CARDLAB_APPLET_AID_MISMATCH"


def test_cardlab_fails_when_physical_check_fails(tmp_path):
    value = valid_evidence()
    value["physical"]["get_status"] = "FAIL:6D00"

    path = tmp_path / "validate-all.json"
    path.write_text(json.dumps(value))

    result = load_cardlab_status(path)

    assert result["status"] == "FAIL"
    assert result["physically_confirmed"] is False


def test_cardlab_fails_when_container_fails(tmp_path):
    value = valid_evidence()
    value["container"] = "FAIL"

    path = tmp_path / "validate-all.json"
    path.write_text(json.dumps(value))

    result = load_cardlab_status(path)

    assert result["status"] == "FAIL"
    assert result["physically_confirmed"] is False
