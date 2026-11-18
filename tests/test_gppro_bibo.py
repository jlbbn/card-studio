from types import SimpleNamespace

from card_studio.card_session import CardSession
from card_studio.gppro_bibo import GPProBIBOBridge
from card_studio.coexistent_install import DEFAULT_GP_JAR


class FakeBackend:
    def __init__(self):
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.transmit_calls = []

        self.reader_name = "Fake Reader"
        self.protocol = "T=0"
        self.atr = "3B6A00FF0031C173C84000009000"

    def connect(self):
        self.connect_calls += 1
        return self

    def transmit(self, command):
        self.transmit_calls.append(command)

        return SimpleNamespace(
            data=b"\x01\x02",
            sw1sw2="9000",
        )

    def disconnect(self):
        self.disconnect_calls += 1


def test_gppro_bibo_uses_existing_card_session():
    backend = FakeBackend()

    session = CardSession(backend)
    session.open()

    assert session.connected is True
    assert backend.connect_calls == 1


def test_gppro_bibo_roundtrip_through_java():
    import jpype


    backend = FakeBackend()

    session = CardSession(backend)
    session.open()

    bridge = GPProBIBOBridge(
        session,
        DEFAULT_GP_JAR,
    )

    proxy = bridge.start()

    BIBO = jpype.JClass("apdu4j.core.BIBO")
    CommandAPDU = jpype.JClass(
        "apdu4j.core.CommandAPDU"
    )

    command = CommandAPDU(
        0x00,
        0xA4,
        0x04,
        0x00,
        256,
    )

    response = BIBO.transmit(
        proxy,
        command,
    )

    assert response.getSW() == 0x9000
    assert bytes(
        (int(value) & 0xFF)
        for value in response.getData()
    ) == b"\x01\x02"

    assert backend.connect_calls == 1

    assert backend.transmit_calls == [
        bytes.fromhex("00A4040000"),
    ]

    assert backend.disconnect_calls == 0
    assert session.connected is True


class FakeGPToolBackend(FakeBackend):
    """
    Minimal GlobalPlatform discovery fake.

    GPTool first performs SELECT with an empty AID to discover
    the Issuer Security Domain. Return a minimal FCI containing
    a valid ISD AID.
    """

    DISCOVER = bytes.fromhex(
        "00A4040000"
    )

    ISD_FCI = bytes.fromhex(
        "6F0A"
        "8408"
        "A000000003000000"
    )

    def transmit(self, command):
        raw = bytes(command)
        self.transmit_calls.append(raw)

        if raw == self.DISCOVER:
            return SimpleNamespace(
                data=self.ISD_FCI,
                sw1sw2="9000",
            )

        return SimpleNamespace(
            data=b"",
            sw1sw2="9000",
        )


def test_gppro_run_tool_uses_existing_card_session():
    backend = FakeGPToolBackend()

    session = CardSession(
        backend
    )
    session.open()

    bridge = GPProBIBOBridge(
        session,
        DEFAULT_GP_JAR,
    )

    rc = bridge.run_tool([
        "--apdu",
        "00A4040000",
    ])

    assert rc == 0

    # CardSession opened the backend exactly once.
    assert backend.connect_calls == 1

    # GPTool must not own/close CardSession.
    assert backend.disconnect_calls == 0
    assert session.connected is True

    # GPTool actually crossed:
    # GPTool -> BIBO -> CardSession -> backend.
    assert len(
        backend.transmit_calls
    ) >= 1

    assert all(
        command == bytes.fromhex(
            "00A4040000"
        )
        for command in backend.transmit_calls
    )

    # Only the physical-session owner closes the card.
    session.close()

    assert backend.disconnect_calls == 1
    assert session.connected is False


def test_gppro_run_tool_rejects_external_reader_selection():
    from card_studio.model import CardError

    backend = FakeGPToolBackend()

    session = CardSession(
        backend
    )
    session.open()

    bridge = GPProBIBOBridge(
        session,
        DEFAULT_GP_JAR,
    )

    try:
        bridge.run_tool([
            "--reader",
            "Another Reader",
            "--list",
        ])
    except CardError as error:
        assert (
            error.code
            == "GPPRO_EXTERNAL_READER_FORBIDDEN"
        )
    else:
        raise AssertionError(
            "External GPTool reader selection was not rejected"
        )

    assert backend.connect_calls == 1
    assert backend.disconnect_calls == 0
    assert session.connected is True

    session.close()


def test_gppro_fresh_jvm_reads_gp_key_from_environment():
    """
    C2.5E propagation gate.

    Verifies that a fresh JVM sees GP_KEY through
    PlaintextKeys.fromEnvironment().

    No card.
    No PC/SC.
    No GPTool.run().
    No APDUs.
    """
    import os
    import subprocess
    import sys
    import textwrap

    test_key = "00112233445566778899AABBCCDDEEFF"

    script = textwrap.dedent(
        r'''
        import jpype
        from jpype import JClass

        from card_studio.coexistent_install import DEFAULT_GP_JAR

        if not jpype.isJVMStarted():
            jpype.startJVM(
                classpath=[str(DEFAULT_GP_JAR)]
            )

        PlaintextKeys = JClass(
            "pro.javacard.gp.keys.PlaintextKeys"
        )

        env_keys = PlaintextKeys.fromEnvironment()

        assert env_keys.isPresent(), (
            "GP_KEY was not visible to "
            "PlaintextKeys.fromEnvironment()"
        )

        keys = env_keys.get()
        master = keys.getMasterKey()

        assert master.isPresent(), (
            "GP_KEY did not produce a master key"
        )

        actual = bytes(
            (int(value) & 0xFF)
            for value in master.get()
        )

        expected = bytes.fromhex(
            "00112233445566778899AABBCCDDEEFF"
        )

        assert actual == expected, (
            "Java environment key differs "
            "from expected test key"
        )

        default_bytes = bytes(
            (int(value) & 0xFF)
            for value in PlaintextKeys.DEFAULT_KEY()
        )

        assert actual != default_bytes, (
            "GP_KEY unexpectedly resolved "
            "to GlobalPlatformPro default key"
        )

        print("JAVA_GP_KEY_ENV=PASS")
        print("JAVA_GP_KEY_IS_DEFAULT=FALSE")
        '''
    )

    env = os.environ.copy()
    env["GP_KEY"] = test_key

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined = (
        result.stdout
        + "\n"
        + result.stderr
    )

    assert test_key not in combined
    assert result.returncode == 0, combined
    assert "JAVA_GP_KEY_ENV=PASS" in result.stdout
    assert (
        "JAVA_GP_KEY_IS_DEFAULT=FALSE"
        in result.stdout
    )


def test_gppro_fresh_jvm_without_gp_key_has_no_environment_keys():
    """
    Negative C2.5E gate.

    Verifies that PlaintextKeys.fromEnvironment()
    returns empty when GP_KEY and GP_KEY_* are absent.

    No card.
    No PC/SC.
    No GPTool.run().
    No APDUs.
    """
    import os
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        r'''
        import jpype
        from jpype import JClass

        from card_studio.coexistent_install import DEFAULT_GP_JAR

        if not jpype.isJVMStarted():
            jpype.startJVM(
                classpath=[str(DEFAULT_GP_JAR)]
            )

        PlaintextKeys = JClass(
            "pro.javacard.gp.keys.PlaintextKeys"
        )

        env_keys = PlaintextKeys.fromEnvironment()

        assert not env_keys.isPresent(), (
            "Environment unexpectedly supplied "
            "GlobalPlatform keys"
        )

        print("JAVA_GP_KEY_ABSENT=PASS")
        '''
    )

    env = os.environ.copy()

    for name in list(env):
        if (
            name == "GP_KEY"
            or name.startswith("GP_KEY_")
        ):
            env.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined = (
        result.stdout
        + "\n"
        + result.stderr
    )

    assert result.returncode == 0, combined

    assert (
        "JAVA_GP_KEY_ABSENT=PASS"
        in result.stdout
    )



def test_gppro_run_tool_rejects_key_arguments():
    from card_studio.model import CardError

    backend = FakeGPToolBackend()

    session = CardSession(
        backend
    )
    session.open()

    bridge = GPProBIBOBridge(
        session,
        DEFAULT_GP_JAR,
    )

    forbidden_cases = [
        ["--default", "--list"],
        ["--key", "00112233445566778899AABBCCDDEEFF", "--list"],
        ["--key=00112233445566778899AABBCCDDEEFF", "--list"],
        ["--key-enc", "00112233445566778899AABBCCDDEEFF", "--list"],
        ["--key-mac", "00112233445566778899AABBCCDDEEFF", "--list"],
        ["--key-dek", "00112233445566778899AABBCCDDEEFF", "--list"],
    ]

    try:
        for args in forbidden_cases:
            try:
                bridge.run_tool(args)
            except CardError as error:
                assert (
                    error.code
                    == "GPPRO_KEY_ARGUMENT_FORBIDDEN"
                )
            else:
                raise AssertionError(
                    f"Forbidden GP key args accepted: {args!r}"
                )

        assert backend.connect_calls == 1
        assert backend.disconnect_calls == 0
        assert backend.transmit_calls == []

    finally:
        session.close()


def test_gppro_install_rejects_default_gp_key():
    from pathlib import Path
    from unittest.mock import patch

    from card_studio.model import CardError
    from card_studio.coexistent_install import execute_install

    backend = FakeBackend()
    session = CardSession(backend)
    session.open()

    runner_calls = []

    def runner(args):
        runner_calls.append(list(args))
        return 0

    try:
        with patch.dict(
            "os.environ",
            {"GP_KEY": "default"},
            clear=False,
        ), patch(
            "card_studio.coexistent_install.prepare_install",
            return_value={
                "result": "READY",
                "errors": [],
            },
        ):
            try:
                execute_install(
                    Path("fixture.cap"),
                    DEFAULT_GP_JAR,
                    Path("fixture-registry.json"),
                    card_session=session,
                    runner=runner,
                )
            except CardError as error:
                assert (
                    error.code
                    == "GP_DEFAULT_KEY_FORBIDDEN"
                )
            else:
                raise AssertionError(
                    "default GP key was not rejected"
                )

        # Must abort before GPTool runner.
        assert runner_calls == []

        # Must abort before any card APDU.
        assert backend.transmit_calls == []

        # execute_install does not own CardSession.
        assert session.connected is True
        assert backend.connect_calls == 1
        assert backend.disconnect_calls == 0

    finally:
        session.close()

    assert backend.disconnect_calls == 1



def test_gppro_install_rejects_non_hex_gp_key_before_runner():
    from pathlib import Path
    from unittest.mock import patch

    from card_studio.model import CardError
    from card_studio.coexistent_install import execute_install

    backend = FakeBackend()
    session = CardSession(backend)
    session.open()

    runner_calls = []

    def runner(args):
        runner_calls.append(list(args))
        return 0

    try:
        with patch.dict(
            "os.environ",
            {"GP_KEY": "THIS-IS-NOT-HEX"},
            clear=False,
        ), patch(
            "card_studio.coexistent_install.prepare_install",
            return_value={
                "result": "READY",
                "errors": [],
            },
        ):
            try:
                execute_install(
                    Path("fixture.cap"),
                    DEFAULT_GP_JAR,
                    Path("fixture-registry.json"),
                    card_session=session,
                    runner=runner,
                )
            except CardError as error:
                assert error.code == "GP_KEY_INVALID_HEX"
            else:
                raise AssertionError(
                    "non-hex GP_KEY was not rejected"
                )

        assert runner_calls == []
        assert backend.transmit_calls == []

    finally:
        session.close()


def test_gppro_install_rejects_invalid_gp_key_length_before_runner():
    from pathlib import Path
    from unittest.mock import patch

    from card_studio.model import CardError
    from card_studio.coexistent_install import execute_install

    backend = FakeBackend()
    session = CardSession(backend)
    session.open()

    runner_calls = []

    def runner(args):
        runner_calls.append(list(args))
        return 0

    # Valid hexadecimal, but only 8 bytes.
    invalid_length_key = "0011223344556677"

    try:
        with patch.dict(
            "os.environ",
            {"GP_KEY": invalid_length_key},
            clear=False,
        ), patch(
            "card_studio.coexistent_install.prepare_install",
            return_value={
                "result": "READY",
                "errors": [],
            },
        ):
            try:
                execute_install(
                    Path("fixture.cap"),
                    DEFAULT_GP_JAR,
                    Path("fixture-registry.json"),
                    card_session=session,
                    runner=runner,
                )
            except CardError as error:
                assert error.code == "GP_KEY_INVALID_LENGTH"
            else:
                raise AssertionError(
                    "invalid-length GP_KEY was not rejected"
                )

        assert runner_calls == []
        assert backend.transmit_calls == []

    finally:
        session.close()



def test_gppro_read_only_list_uses_existing_card_session():
    from card_studio.coexistent_install import build_read_only_args

    backend = FakeGPToolBackend()

    session = CardSession(
        backend
    )
    session.open()

    assert backend.connect_calls == 1
    assert backend.disconnect_calls == 0
    assert session.connected is True

    bridge = GPProBIBOBridge(
        session,
        DEFAULT_GP_JAR,
    )

    captured = {}

    class FakeGPTool:
        def run(self, proxy, args):
            captured["proxy"] = proxy
            captured["args"] = list(args)
            return 0

    bridge._proxy = object()
    bridge._JString = str
    bridge._JArray = lambda _kind: lambda values: list(values)
    bridge._GPTool = FakeGPTool

    args = build_read_only_args()

    assert args == ["--list"]

    rc = bridge.run_tool(args)

    assert rc == 0
    assert captured["args"] == ["--list"]
    assert captured["proxy"] is bridge.proxy

    # GPTool must reuse the CardSession already opened by Card Studio.
    assert backend.connect_calls == 1

    # GPTool/BIBO must not own or close the physical session.
    assert backend.disconnect_calls == 0
    assert session.connected is True

    # The read-only invocation itself must not introduce a physical
    # backend transmission in this isolated runner-contract test.
    assert backend.transmit_calls == []

    # Only the CardSession owner closes PC/SC.
    session.close()

    assert backend.disconnect_calls == 1
    assert session.connected is False



def test_gppro_secure_preflight_uses_existing_card_session():
    from card_studio.coexistent_install import (
        build_secure_channel_preflight_args,
    )

    backend = FakeGPToolBackend()

    session = CardSession(
        backend
    )
    session.open()

    assert backend.connect_calls == 1
    assert backend.disconnect_calls == 0
    assert session.connected is True

    bridge = GPProBIBOBridge(
        session,
        DEFAULT_GP_JAR,
    )

    captured = {}

    class FakeGPTool:
        def run(self, proxy, args):
            captured["proxy"] = proxy
            captured["args"] = list(args)
            captured["connected"] = session.connected
            return 0

    bridge._proxy = object()
    bridge._JString = str
    bridge._JArray = lambda _kind: lambda values: list(values)
    bridge._GPTool = FakeGPTool

    args = build_secure_channel_preflight_args()

    assert args == [
        "--key-ver", "0x00",
        "--mode", "MAC",
        "--mode", "ENC",
        "--list",
    ]

    rc = bridge.run_tool(args)

    assert rc == 0
    assert captured["args"] == args
    assert captured["proxy"] is bridge.proxy
    assert captured["connected"] is True

    # Card Studio must remain the only owner of PC/SC.
    assert backend.connect_calls == 1
    assert backend.disconnect_calls == 0
    assert session.connected is True

    # No APDU is expected in this isolated runner-contract harness.
    assert backend.transmit_calls == []

    session.close()

    assert backend.disconnect_calls == 1
    assert session.connected is False



def test_t0_case4_short_apdu_strips_le():
    from card_studio.gppro_bibo import (
        GPProBIBOBridge,
    )

    command = bytes.fromhex(
        "8482030010"
        "00112233445566778899AABBCCDDEEFF"
        "00"
    )

    result = (
        GPProBIBOBridge._strip_t0_case4_le(
            command
        )
    )

    assert result == command[:-1]


def test_t0_case3_short_apdu_is_unchanged():
    from card_studio.gppro_bibo import (
        GPProBIBOBridge,
    )

    command = bytes.fromhex(
        "8482030010"
        "00112233445566778899AABBCCDDEEFF"
    )

    assert (
        GPProBIBOBridge._strip_t0_case4_le(
            command
        )
        == command
    )


def test_t0_case2_apdu_is_unchanged():
    from card_studio.gppro_bibo import (
        GPProBIBOBridge,
    )

    command = bytes.fromhex(
        "00C0000000"
    )

    assert (
        GPProBIBOBridge._strip_t0_case4_le(
            command
        )
        == command
    )


def test_t0_case4_wire_transport_strips_le(
    tmp_path,
):
    from types import SimpleNamespace

    from card_studio.gppro_bibo import (
        GPProBIBOBridge,
    )

    jar = tmp_path / "gp.jar"
    jar.write_bytes(b"test")

    sent = []

    class FakeSession:
        connected = True

        info = SimpleNamespace(
            protocol="T=0"
        )

        def transmit(
            self,
            command,
        ):
            sent.append(
                bytes(command)
            )

            return SimpleNamespace(
                sw1sw2="9000",
                data=b"",
            )

    bridge = GPProBIBOBridge(
        FakeSession(),
        jar,
    )

    # Avoid JVM dependency in this transport-only test.
    bridge._java_bytes = lambda raw: bytes(raw)

    command = bytes.fromhex(
        "8482030010"
        "00112233445566778899AABBCCDDEEFF"
        "00"
    )

    response = bridge.transceive(
        command
    )

    assert sent == [
        command[:-1]
    ]

    assert response == bytes.fromhex(
        "9000"
    )
