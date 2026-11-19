from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

from card_studio.cli import main
from card_studio.gppro_bibo import (
    GP_JAR_RELATIVE_PATH,
    REQUIRED_JAVA_CLASSES,
    check_globalplatform_runtime,
    resolve_gp_jar,
)


ROOT = Path(__file__).resolve().parents[1]


def make_gp_jar(tmp_path: Path) -> Path:
    jar = tmp_path / "gp.jar"
    jar.write_bytes(b"runtime-check-fixture")
    return jar


def make_fake_jpype(
    monkeypatch: pytest.MonkeyPatch,
    *,
    already_started: bool = False,
) -> tuple[ModuleType, dict[str, object]]:
    jpype = ModuleType("jpype")
    jpype.__version__ = "1.7.1-test"
    state = {
        "started": already_started,
        "start_calls": [],
        "loaded_classes": [],
    }

    def is_jvm_started():
        return state["started"]

    def start_jvm(*, classpath):
        state["start_calls"].append(classpath)
        state["started"] = True

    def load_class(name):
        state["loaded_classes"].append(name)
        return object()

    jpype.isJVMStarted = is_jvm_started
    jpype.startJVM = start_jvm
    jpype.JClass = load_class
    monkeypatch.setitem(sys.modules, "jpype", jpype)
    return jpype, state


def test_development_gp_jar_uses_project_root():
    assert resolve_gp_jar() == ROOT / GP_JAR_RELATIVE_PATH


def test_packaged_gp_jar_uses_pyinstaller_bundle_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert resolve_gp_jar() == tmp_path / GP_JAR_RELATIVE_PATH


def test_runtime_check_starts_jvm_and_loads_required_classes_without_pcsc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    jar = make_gp_jar(tmp_path)
    _, state = make_fake_jpype(monkeypatch)

    report = check_globalplatform_runtime(jar)

    assert report["status"] == "PASS"
    assert report["gp_jar"] == {"path": str(jar), "exists": True}
    assert report["jpype"] == {"imported": True, "version": "1.7.1-test"}
    assert report["jvm"] == {
        "started": True,
        "already_started": False,
        "started_by_check": True,
    }
    assert report["classes"] == {name: True for name in REQUIRED_JAVA_CLASSES}
    assert state["start_calls"] == [[str(jar)]]
    assert state["loaded_classes"] == list(REQUIRED_JAVA_CLASSES)
    assert report["pcsc"] == "NOT_OPENED"
    assert report["apdus"] == "NOT_SENT"
    assert report["physical_validation"] == "NOT_EXECUTED"
    assert report["errors"] == []


def test_runtime_check_reuses_existing_jvm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    jar = make_gp_jar(tmp_path)
    _, state = make_fake_jpype(monkeypatch, already_started=True)

    report = check_globalplatform_runtime(jar)

    assert report["status"] == "PASS"
    assert report["jvm"]["already_started"] is True
    assert report["jvm"]["started_by_check"] is False
    assert state["start_calls"] == []


def test_missing_jar_fails_before_jpype_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setitem(sys.modules, "jpype", None)

    report = check_globalplatform_runtime(tmp_path / "missing.jar")

    assert report["status"] == "FAIL"
    assert report["gp_jar"]["exists"] is False
    assert report["jpype"]["imported"] is False
    assert report["jvm"]["started"] is False
    assert [error["code"] for error in report["errors"]] == [
        "GPPRO_JAR_NOT_FOUND"
    ]


def test_jpype_import_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    jar = make_gp_jar(tmp_path)
    monkeypatch.setitem(sys.modules, "jpype", None)

    report = check_globalplatform_runtime(jar)

    assert report["status"] == "FAIL"
    assert report["gp_jar"]["exists"] is True
    assert report["jpype"]["imported"] is False
    assert report["jvm"]["started"] is False
    assert [error["code"] for error in report["errors"]] == [
        "JPYPE_IMPORT_FAILED"
    ]


def test_jvm_start_failure_skips_class_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    jar = make_gp_jar(tmp_path)
    jpype, state = make_fake_jpype(monkeypatch)

    def fail_start(*, classpath):
        raise RuntimeError("synthetic JVM failure")

    jpype.startJVM = fail_start

    report = check_globalplatform_runtime(jar)

    assert report["status"] == "FAIL"
    assert report["jvm"]["started"] is False
    assert state["loaded_classes"] == []
    assert [error["code"] for error in report["errors"]] == [
        "JVM_START_FAILED"
    ]


def test_class_failure_reports_all_required_classes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    jar = make_gp_jar(tmp_path)
    jpype, state = make_fake_jpype(monkeypatch)

    def fail_bibo(name):
        state["loaded_classes"].append(name)
        if name == "apdu4j.core.BIBO":
            raise RuntimeError("synthetic class failure")
        return object()

    jpype.JClass = fail_bibo

    report = check_globalplatform_runtime(jar)

    assert report["status"] == "FAIL"
    assert report["classes"] == {
        "apdu4j.core.BIBO": False,
        "pro.javacard.gptool.GPTool": True,
    }
    assert state["loaded_classes"] == list(REQUIRED_JAVA_CLASSES)
    assert report["errors"][0]["code"] == "JAVA_CLASS_LOAD_FAILED"
    assert report["errors"][0]["class"] == "apdu4j.core.BIBO"


def test_cli_emits_json_and_nonzero_for_missing_jar(tmp_path: Path):
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        result = main([
            "--config-dir",
            str(tmp_path / "config"),
            "globalplatform-status",
            "--gp-jar",
            str(tmp_path / "missing.jar"),
        ])

    report = json.loads(output.getvalue())
    assert result == 2
    assert report["status"] == "FAIL"
    assert report["errors"][0]["code"] == "GPPRO_JAR_NOT_FOUND"
    assert report["pcsc"] == "NOT_OPENED"
    assert report["apdus"] == "NOT_SENT"


def test_cli_success_does_not_create_pcsc_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    jar = make_gp_jar(tmp_path)
    make_fake_jpype(monkeypatch)

    def forbidden_pcsc(*args, **kwargs):
        raise AssertionError("globalplatform-status must not open PC/SC")

    monkeypatch.setattr("card_studio.cli.create_pcsc_backend", forbidden_pcsc)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = main([
            "--config-dir",
            str(tmp_path / "config"),
            "globalplatform-status",
            "--gp-jar",
            str(jar),
        ])

    assert result == 0
    assert json.loads(output.getvalue())["status"] == "PASS"


@pytest.mark.skipif(
    importlib.util.find_spec("jpype") is None
    or shutil.which("java") is None
    or not (ROOT / GP_JAR_RELATIVE_PATH).is_file(),
    reason="JPype, Java o gp.jar no disponibles",
)
def test_real_globalplatform_status_in_fresh_process(tmp_path: Path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "card_studio",
            "--cli",
            "--config-dir",
            str(tmp_path / "config"),
            "globalplatform-status",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "PASS"
    assert Path(report["gp_jar"]["path"]) == ROOT / GP_JAR_RELATIVE_PATH
    assert report["gp_jar"]["exists"] is True
    assert report["jpype"]["imported"] is True
    assert report["jvm"]["started"] is True
    assert report["jvm"]["already_started"] is False
    assert report["jvm"]["started_by_check"] is True
    assert report["classes"] == {name: True for name in REQUIRED_JAVA_CLASSES}
    assert report["pcsc"] == "NOT_OPENED"
    assert report["apdus"] == "NOT_SENT"
    assert report["errors"] == []


def test_macos_workflow_checks_globalplatform_inside_app():
    workflow = (ROOT / ".github/workflows/macos-release.yml").read_text()

    assert '"$EXECUTABLE" --cli globalplatform-status' in workflow
    assert "globalplatform-status-app.json" in workflow
    assert "globalplatform-status-development.json" in workflow
    assert 'test "$(uname -m)" = "arm64"' in workflow
    assert '"_MEI" in gp_jar.parts' in workflow
    assert "hashlib.sha256(gp_jar.read_bytes()).hexdigest()" in workflow
    assert "Card-Studio-1.2.3-PACO-macOS.dmg" in workflow
    assert "Card-Studio-1.2.3-PACO-macOS.zip" in workflow
