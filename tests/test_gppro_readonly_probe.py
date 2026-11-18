from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


PROBE = Path("tools/gppro_readonly_probe.py")


def load_probe():
    spec = importlib.util.spec_from_file_location(
        "gppro_readonly_probe_test",
        PROBE,
    )

    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


class FakeBackend:
    mode = "PHYSICAL"

    def __init__(self):
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.transmit_calls = []

    def connect(self):
        self.connect_calls += 1
        return self

    def disconnect(self):
        self.disconnect_calls += 1

    def getATR(self):
        return bytes.fromhex(
            "3BF81300008131FE454A434F5076323431B7"
        )

    def getProtocol(self):
        return "T=1"

    def getReader(self):
        return "FAKE ALCOR READER"

    def transmit(self, command):
        self.transmit_calls.append(bytes(command))
        return SimpleNamespace(
            data=b"",
            sw1sw2="9000",
        )


def test_readonly_probe_single_session_lifecycle():
    probe = load_probe()

    backend = FakeBackend()

    captured = {
        "bridge_init": 0,
        "args": None,
        "connected": None,
    }

    class FakeBridge:
        def __init__(self, session, gp_jar):
            captured["bridge_init"] += 1
            captured["session"] = session
            captured["gp_jar"] = gp_jar

            assert session.connected is True

        def run_tool(self, args):
            captured["args"] = list(args)
            captured["connected"] = (
                captured["session"].connected
            )
            return 0

    probe.create_pcsc_backend = lambda platform: backend
    probe.GPProBIBOBridge = FakeBridge

    rc = probe.main()

    assert rc == 0
    assert backend.connect_calls == 1
    assert backend.disconnect_calls == 1
    assert backend.transmit_calls == []

    assert captured["bridge_init"] == 1
    assert captured["args"] == ["--list"]
    assert captured["connected"] is True


def test_readonly_probe_rejects_unknown_protocol():
    probe = load_probe()

    backend = FakeBackend()
    backend.getProtocol = lambda: "UNKNOWN"

    bridge_created = {"value": False}

    class FakeBridge:
        def __init__(self, session, gp_jar):
            bridge_created["value"] = True

    probe.create_pcsc_backend = lambda platform: backend
    probe.GPProBIBOBridge = FakeBridge

    rc = probe.main()

    assert rc == 2
    assert backend.connect_calls == 1
    assert backend.disconnect_calls == 1
    assert bridge_created["value"] is False
