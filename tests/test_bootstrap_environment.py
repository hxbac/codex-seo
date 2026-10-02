import json
from pathlib import Path
import sys


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


import bootstrap_environment as bootstrap_module  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _pip_path_by_default(monkeypatch):
    """The older tests describe the pip path; the uv tests turn uv back on."""
    monkeypatch.setenv("AI_CONTENT_NO_UV", "1")


def test_bootstrap_environment_allows_core_ready_without_playwright(monkeypatch, tmp_path: Path):
    venv_dir = tmp_path / "fake-venv"
    python_path = venv_dir / "Scripts" / "python.exe"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("", encoding="utf-8")

    verification_payload = {
        "ready": True,
        "capabilities": {
            "core_ready": True,
            "visual_ready": False,
            "premium_report_ready": False,
            "full_ready": False,
        },
    }

    def fake_run_command(cmd: list[str], cwd: Path | None = None):
        if "verify_environment.py" in " ".join(cmd):
            return {
                "cmd": cmd,
                "returncode": 0,
                "stdout": json.dumps(verification_payload),
                "stderr": "",
                "ok": True,
            }
        if "playwright" in cmd:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "browser install failed", "ok": False}
        return {"cmd": cmd, "returncode": 0, "stdout": "", "stderr": "", "ok": True}

    monkeypatch.setattr(bootstrap_module, "run_command", fake_run_command)
    result = bootstrap_module.bootstrap_environment(venv_dir=venv_dir, install_playwright_browser=True)

    assert result["ok"] is True
    assert result["full_ready"] is False
    assert result["verification"]["capabilities"]["core_ready"] is True
    assert result["verification"]["capabilities"]["visual_ready"] is False


def test_bootstrap_environment_allows_optional_requirement_failures(monkeypatch, tmp_path: Path):
    venv_dir = tmp_path / "fake-venv"
    python_path = venv_dir / "Scripts" / "python.exe"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("", encoding="utf-8")

    verification_payload = {
        "ready": True,
        "capabilities": {
            "core_ready": True,
            "visual_ready": False,
            "premium_report_ready": False,
            "full_ready": False,
        },
    }

    def fake_run_command(cmd: list[str], cwd: Path | None = None):
        command_text = " ".join(cmd)
        if "requirements-ocr.txt" in command_text:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "onnxruntime unavailable", "ok": False}
        if "verify_environment.py" in command_text:
            return {
                "cmd": cmd,
                "returncode": 0,
                "stdout": json.dumps(verification_payload),
                "stderr": "",
                "ok": True,
            }
        return {"cmd": cmd, "returncode": 0, "stdout": "", "stderr": "", "ok": True}

    monkeypatch.setattr(bootstrap_module, "run_command", fake_run_command)
    result = bootstrap_module.bootstrap_environment(venv_dir=venv_dir, install_playwright_browser=False)

    assert result["ok"] is True
    assert result["optional_failed_groups"] == ["ocr"]
    failed_optional_steps = [
        step for step in result["steps"] if step.get("group") == "ocr" and step["ok"] is False
    ]
    assert len(failed_optional_steps) == 1


def test_bootstrap_environment_fails_when_core_requirements_fail(monkeypatch, tmp_path: Path):
    venv_dir = tmp_path / "fake-venv"
    python_path = venv_dir / "Scripts" / "python.exe"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("", encoding="utf-8")

    verification_payload = {
        "ready": False,
        "capabilities": {
            "core_ready": False,
            "visual_ready": False,
            "premium_report_ready": False,
            "full_ready": False,
        },
    }

    def fake_run_command(cmd: list[str], cwd: Path | None = None):
        command_text = " ".join(cmd)
        if "requirements-core.txt" in command_text:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "lxml unavailable", "ok": False}
        if "verify_environment.py" in command_text:
            return {
                "cmd": cmd,
                "returncode": 1,
                "stdout": json.dumps(verification_payload),
                "stderr": "",
                "ok": False,
            }
        return {"cmd": cmd, "returncode": 0, "stdout": "", "stderr": "", "ok": True}

    monkeypatch.setattr(bootstrap_module, "run_command", fake_run_command)
    result = bootstrap_module.bootstrap_environment(venv_dir=venv_dir, install_playwright_browser=False)

    assert result["ok"] is False
    failed_required_steps = [
        step for step in result["steps"] if step.get("required") and step["ok"] is False
    ]
    assert len(failed_required_steps) == 2


def test_run_command_truncates_large_output(monkeypatch):
    class Completed:
        returncode = 0
        stdout = "x" * (bootstrap_module.OUTPUT_LIMIT + 50)
        stderr = "y" * (bootstrap_module.OUTPUT_LIMIT + 50)

    def fake_run(*args, **kwargs):
        return Completed()

    monkeypatch.setattr(bootstrap_module.subprocess, "run", fake_run)

    result = bootstrap_module.run_command(["python", "--version"])

    assert result["ok"] is True
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert len(result["stdout"]) < len(Completed.stdout)
    assert "...[truncated]..." in result["stdout"]


def test_bootstrap_cli_writes_json_output_file(monkeypatch, tmp_path: Path, capsys):
    output_path = tmp_path / "bootstrap.json"
    payload = {
        "ok": True,
        "full_ready": True,
        "created_venv": False,
        "venv": "venv",
        "python": "venv/bin/python",
        "optional_failed_groups": [],
        "steps": [],
        "verification": {"capabilities": {"core_ready": True, "full_ready": True}},
    }

    monkeypatch.setattr(bootstrap_module, "bootstrap_environment", lambda **kwargs: payload)
    monkeypatch.setattr(
        sys,
        "argv",
        ["bootstrap_environment.py", "--json", "--json-output", str(output_path)],
    )

    assert bootstrap_module.main() == 0

    stdout_payload = json.loads(capsys.readouterr().out)
    file_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout_payload == payload
    assert file_payload == payload


def test_bootstrap_cli_returns_json_on_unhandled_exception(monkeypatch, tmp_path: Path, capsys):
    output_path = tmp_path / "bootstrap-error.json"

    def fail_bootstrap(**kwargs):
        raise RuntimeError("venv module unavailable")

    monkeypatch.setattr(bootstrap_module, "bootstrap_environment", fail_bootstrap)
    monkeypatch.setattr(
        sys,
        "argv",
        ["bootstrap_environment.py", "--json", "--json-output", str(output_path)],
    )

    assert bootstrap_module.main() == 1

    stdout_payload = json.loads(capsys.readouterr().out)
    file_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert stdout_payload["ok"] is False
    assert stdout_payload["error"] == "venv module unavailable"
    assert stdout_payload["exception_type"] == "RuntimeError"
    assert file_payload == stdout_payload


def _fake_recorder(calls, verification=None):
    verification = verification or {"ready": True, "capabilities": {"core_ready": True, "full_ready": True}}

    def fake_run_command(cmd, cwd=None):
        calls.append(list(cmd))
        text = " ".join(cmd)
        stdout = json.dumps(verification) if "verify_environment.py" in text else ""
        return {"cmd": cmd, "returncode": 0, "stdout": stdout, "stderr": "", "ok": True}

    return fake_run_command


def test_find_uv_honours_opt_out_and_override(monkeypatch, tmp_path: Path):
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("", encoding="utf-8")
    monkeypatch.setenv("UV", str(fake_uv))
    assert bootstrap_module.find_uv() is None  # AI_CONTENT_NO_UV=1
    monkeypatch.delenv("AI_CONTENT_NO_UV")
    assert bootstrap_module.find_uv() == str(fake_uv)
    monkeypatch.delenv("UV")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(bootstrap_module.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    assert bootstrap_module.find_uv() is None


def test_bootstrap_uses_uv_for_venv_and_installs_when_present(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AI_CONTENT_NO_UV")
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("", encoding="utf-8")
    monkeypatch.setenv("UV", str(fake_uv))
    venv_dir = tmp_path / "new-venv"
    calls: list[list[str]] = []
    recorder = _fake_recorder(calls)

    def fake_run_command(cmd, cwd=None):
        result = recorder(cmd, cwd)
        if cmd[:2] == [str(fake_uv), "venv"]:
            python = bootstrap_module.python_in_venv(venv_dir)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(bootstrap_module, "run_command", fake_run_command)
    result = bootstrap_module.bootstrap_environment(venv_dir=venv_dir, install_playwright_browser=False)

    assert result["ok"] is True
    assert result["installer"] == "uv"
    assert result["created_venv"] is True
    assert calls[0][:3] == [str(fake_uv), "venv", "--quiet"]
    installs = [c for c in calls if c[1:3] == ["pip", "install"]]
    assert installs and all(c[0] == str(fake_uv) and "--python" in c for c in installs)
    # No `python -m pip install --upgrade pip` step under uv.
    assert not any("--upgrade" in c for c in calls)


def test_bootstrap_without_uv_keeps_the_pip_path(monkeypatch, tmp_path: Path):
    venv_dir = tmp_path / "fake-venv"
    python_path = bootstrap_module.python_in_venv(venv_dir)
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(bootstrap_module, "run_command", _fake_recorder(calls))
    result = bootstrap_module.bootstrap_environment(venv_dir=venv_dir, install_playwright_browser=False)
    assert result["installer"] == "pip"
    pip_calls = [c for c in calls if c[1:4] == ["-m", "pip", "install"]]
    assert any("--upgrade" in c for c in pip_calls)
    assert any(str(bootstrap_module.CORE_REQUIREMENTS) in c for c in pip_calls)


def test_uv_venv_failure_falls_back_to_the_builtin_venv(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AI_CONTENT_NO_UV")
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("", encoding="utf-8")
    monkeypatch.setenv("UV", str(fake_uv))
    venv_dir = tmp_path / "v"
    built: list[Path] = []

    class FakeBuilder:
        def __init__(self, with_pip=False):
            assert with_pip is True

        def create(self, path):
            built.append(Path(path))
            python = bootstrap_module.python_in_venv(Path(path))
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")

    calls: list[list[str]] = []
    recorder = _fake_recorder(calls)

    def fake_run_command(cmd, cwd=None):
        result = recorder(cmd, cwd)
        if cmd[:2] == [str(fake_uv), "venv"]:
            result.update(ok=False, returncode=2)
        return result

    monkeypatch.setattr(bootstrap_module, "run_command", fake_run_command)
    monkeypatch.setattr(bootstrap_module.venv, "EnvBuilder", FakeBuilder)
    result = bootstrap_module.bootstrap_environment(venv_dir=venv_dir, install_playwright_browser=False)
    assert built == [venv_dir]
    assert result["installer"] == "pip"
    assert result["ok"] is True
