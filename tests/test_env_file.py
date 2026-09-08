"""Tests for the .env credential loader.

The loader exists so that keys never have to be typed on a command line, where
they land in shell history and in the process table. These tests pin the two
properties that make that safe: values are parsed literally, and no value is
ever echoed back.
"""

import os
import stat
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import env_file

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class TestParse:
    def test_plain_pair(self):
        assert env_file.parse("A=hello world") == {"A": "hello world"}

    def test_hash_is_part_of_the_value_not_a_comment(self):
        """Passwords routinely contain '#'. Inline comment stripping would corrupt them."""
        assert env_file.parse("B=p@ss#w0rd!") == {"B": "p@ss#w0rd!"}

    def test_only_the_first_equals_splits(self):
        assert env_file.parse("C=abc=def") == {"C": "abc=def"}

    def test_export_prefix_is_tolerated(self):
        assert env_file.parse("export D=xyz") == {"D": "xyz"}

    def test_no_shell_expansion(self):
        assert env_file.parse("E=$HOME/x") == {"E": "$HOME/x"}

    def test_double_quotes_are_stripped_and_escapes_decoded(self):
        assert env_file.parse('F="a\\nb"') == {"F": "a\nb"}

    def test_single_quotes_are_literal(self):
        assert env_file.parse("G='a\\nb'") == {"G": "a\\nb"}

    @pytest.mark.parametrize("line", ["# H=nope", "", "   ", "not a pair", "=novalue", "1BAD=x"])
    def test_malformed_lines_are_skipped_without_raising(self, line):
        assert env_file.parse(line) == {}

    def test_byte_order_mark_does_not_swallow_the_first_key(self):
        assert env_file.parse("﻿I=ok") == {"I": "ok"}


class TestLoad:
    def test_existing_environment_variable_wins(self, tmp_path, monkeypatch):
        path = tmp_path / ".env"
        path.write_text("K_ENVFILE=from-file\n", encoding="utf-8")
        monkeypatch.setenv("K_ENVFILE", "from-shell")
        env_file.load(path)
        assert os.environ["K_ENVFILE"] == "from-shell"

    def test_override_flag_replaces_it(self, tmp_path, monkeypatch):
        path = tmp_path / ".env"
        path.write_text("K_ENVFILE=from-file\n", encoding="utf-8")
        monkeypatch.setenv("K_ENVFILE", "from-shell")
        env_file.load(path, override=True)
        assert os.environ["K_ENVFILE"] == "from-file"

    def test_returns_names_never_values(self, tmp_path, monkeypatch):
        path = tmp_path / ".env"
        path.write_text("K_SECRET_ENVFILE=swordfish\n", encoding="utf-8")
        monkeypatch.delenv("K_SECRET_ENVFILE", raising=False)
        applied = env_file.load(path)
        assert applied == ["K_SECRET_ENVFILE"]
        assert "swordfish" not in repr(applied)

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert env_file.load(tmp_path / "absent") == []


class TestDiscovery:
    def test_empty_override_disables_loading(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_ENV_FILE", "")
        assert env_file.candidate_paths() == []
        assert env_file.find_env_file() is None

    def test_override_points_at_one_file(self, tmp_path, monkeypatch):
        path = tmp_path / "custom.env"
        path.write_text("A=1\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(path))
        assert env_file.find_env_file() == path

    def test_default_search_order_is_canonical_then_claude_then_repo(self, monkeypatch):
        """Phase B moved the agent neutral canonical path ahead of ~/.claude/.env.

        A machine that already has ~/.claude/.env still resolves to the same
        file (find_env_file picks the first one that exists), so this is a
        change to the search order, not to what an existing single key setup
        actually loads.
        """
        monkeypatch.delenv("AI_CONTENT_ENV_FILE", raising=False)
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        paths = env_file.candidate_paths()
        assert paths[0] == Path.home() / ".config" / "ai-content" / "credentials.env"
        assert paths[1] == Path.home() / ".claude" / ".env"
        assert paths[2] == SCRIPTS.parent / ".env"

    def test_ai_content_env_file_overrides_claude_env_file(self, tmp_path, monkeypatch):
        path = tmp_path / "custom.env"
        path.write_text("A=1\n", encoding="utf-8")
        monkeypatch.setenv("AI_CONTENT_ENV_FILE", str(path))
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "should-not-be-used.env"))
        assert env_file.find_env_file() == path

    def test_empty_ai_content_env_file_disables_loading(self, monkeypatch):
        monkeypatch.setenv("AI_CONTENT_ENV_FILE", "")
        assert env_file.candidate_paths() == []
        assert env_file.find_env_file() is None


class TestNoLeakage:
    def test_check_masks_secret_values(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "DATAFORSEO_PASSWORD=SENTINEL-PASSWORD\nPEXELS_API_KEY=SENTINEL-KEY\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        env = dict(os.environ)
        env["CLAUDE_ENV_FILE"] = str(path)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "env_file.py"), "--check"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert proc.returncode == 0
        combined = proc.stdout + proc.stderr
        assert "SENTINEL-PASSWORD" not in combined
        assert "SENTINEL-KEY" not in combined
        assert "DATAFORSEO_PASSWORD" in combined

    def test_loose_permissions_are_reported(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("A=1\n", encoding="utf-8")
        path.chmod(0o644)
        env = dict(os.environ)
        env["CLAUDE_ENV_FILE"] = str(path)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "env_file.py"), "--check"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert "chmod 600" in proc.stderr


# No TestWiring class here: wiring env_file.py into codex-seo's own credential
# reading scripts (backlinks_auth.py, dataforseo_merchant.py, and friends) is
# Phase C work, done by a different agent. This module only has to prove the
# loader and the rotation API work; claude-blog and claude-seo already assert
# their own scripts import it.


# ---------------------------------------------------------------------------
# Phase B: key rotation
# ---------------------------------------------------------------------------

TEST_GROUP = "pexels"
TEST_GROUP_VAR = "PEXELS_API_KEY"
TEST_MULTI_GROUP = "dataforseo"


@pytest.fixture(autouse=True)
def _clean_test_group_vars(monkeypatch):
    """Every rotation test owns a small slice of the environment. Clear it
    first so a variable left behind by one test cannot leak into the next.
    """
    prefixes = (TEST_GROUP_VAR, "DATAFORSEO_USERNAME", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")
    for name in list(os.environ):
        if name.startswith(prefixes):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def enable_state(tmp_path, monkeypatch):
    """Opt back into the cooldown state file for one test, entirely inside
    tmp_path, so a test run never touches a real machine's credential state.

    conftest.py sets CLAUDE_ENV_FILE to the empty string for every test to
    keep loading disabled; that same switch disables the state file, so a
    test that exercises cooldown behaviour must give it a non-empty value.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "not-loaded.env"))
    monkeypatch.delenv("AI_CONTENT_ENV_FILE", raising=False)
    return tmp_path


def make_http_error(code):
    return urllib.error.HTTPError("https://example.invalid", code, "status", None, None)


class TestSlots:
    def test_base_name_only_is_one_slot(self, monkeypatch):
        monkeypatch.setenv(TEST_GROUP_VAR, "sentinel-not-a-real-key")
        result = env_file.slots(TEST_GROUP)
        assert len(result) == 1
        assert result[0].index == 1
        assert result[0].values == {TEST_GROUP_VAR: "sentinel-not-a-real-key"}

    def test_base_plus_2_and_3_are_three_slots_in_order(self, monkeypatch):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")
        monkeypatch.setenv(TEST_GROUP_VAR + "_3", "s3")
        result = env_file.slots(TEST_GROUP)
        assert [s.index for s in result] == [1, 2, 3]
        assert [s.values[TEST_GROUP_VAR] for s in result] == ["s1", "s2", "s3"]

    def test_gap_at_3_while_4_exists_stops_at_two_slots_with_one_warning(self, monkeypatch, capsys):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")
        monkeypatch.setenv(TEST_GROUP_VAR + "_4", "s4")
        result = env_file.slots(TEST_GROUP)
        assert [s.index for s in result] == [1, 2]
        err = capsys.readouterr().err
        assert err.count("warning:") == 1
        assert "slot 3" in err

    def test_incomplete_group_is_skipped_with_one_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "user1")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw-1")
        monkeypatch.setenv("DATAFORSEO_USERNAME_2", "user2")
        # No DATAFORSEO_PASSWORD_2: slot 2 is incomplete.
        result = env_file.slots(TEST_MULTI_GROUP)
        assert [s.index for s in result] == [1]
        err = capsys.readouterr().err
        assert err.count("warning:") == 1
        assert "slot 2" in err

    def test_dataforseo_login_alias_resolves_at_every_slot(self, monkeypatch):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "user1")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw-1")
        monkeypatch.setenv("DATAFORSEO_LOGIN_2", "login2")
        monkeypatch.setenv("DATAFORSEO_PASSWORD_2", "sentinel-pw-2")
        result = env_file.slots(TEST_MULTI_GROUP)
        assert [s.index for s in result] == [1, 2]
        assert result[1].values["DATAFORSEO_USERNAME"] == "login2"

    def test_username_wins_over_login_when_both_present_and_differ(self, monkeypatch):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "canonical-user")
        monkeypatch.setenv("DATAFORSEO_LOGIN", "legacy-user")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw")
        result = env_file.slots(TEST_MULTI_GROUP)
        assert result[0].values["DATAFORSEO_USERNAME"] == "canonical-user"

    def test_more_than_20_slots_is_capped_at_20(self, monkeypatch):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        for i in range(2, 26):
            monkeypatch.setenv("{0}_{1}".format(TEST_GROUP_VAR, i), "s{0}".format(i))
        result = env_file.slots(TEST_GROUP)
        assert len(result) == 20
        assert result[-1].index == 20

    def test_unknown_group_returns_no_slots(self):
        assert env_file.slots("not-a-real-group") == []


class TestRotate:
    def test_first_slot_429_uses_second_slot(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")
        seen = []

        def call(slot):
            seen.append(slot.index)
            if slot.index == 1:
                raise make_http_error(429)
            return "result-from-{0}".format(slot.index)

        result = env_file.rotate(TEST_GROUP, call)
        assert result == "result-from-2"
        assert seen == [1, 2]

    def test_first_slot_500_propagates_and_second_slot_never_called(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")
        seen = []

        def call(slot):
            seen.append(slot.index)
            raise make_http_error(500)

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            env_file.rotate(TEST_GROUP, call)
        assert excinfo.value.code == 500
        assert seen == [1]

    def test_all_slots_429_raises_all_slots_failed_with_cause(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")

        def call(slot):
            raise make_http_error(429)

        with pytest.raises(env_file.AllSlotsFailed) as excinfo:
            env_file.rotate(TEST_GROUP, call)
        assert isinstance(excinfo.value.__cause__, urllib.error.HTTPError)
        assert excinfo.value.__cause__.code == 429

    def test_cooldown_recorded_second_call_starts_at_slot_2(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")

        def failing_first(slot):
            if slot.index == 1:
                raise make_http_error(429)
            return "ok"

        env_file.rotate(TEST_GROUP, failing_first)

        seen = []

        def call(slot):
            seen.append(slot.index)
            return "ok-again"

        result = env_file.rotate(TEST_GROUP, call)
        assert seen == [2]
        assert result == "ok-again"

    def test_every_slot_cooling_retries_least_recently_failed(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")
        now = datetime.now(timezone.utc)

        def iso(dt):
            return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        env_file._save_state({
            TEST_GROUP: {
                "1": {"failed_at": iso(now - timedelta(minutes=20)), "reason": "HTTP 429"},
                "2": {"failed_at": iso(now - timedelta(minutes=1)), "reason": "HTTP 429"},
            }
        })

        seen = []

        def call(slot):
            seen.append(slot.index)
            return "ok"

        result = env_file.rotate(TEST_GROUP, call)
        assert result == "ok"
        assert seen == [1]

    def test_missing_group_raises_credentials_missing(self):
        with pytest.raises(env_file.CredentialsMissing):
            env_file.rotate(TEST_GROUP, lambda slot: "unreachable")

    def test_rotatable_error_from_call_switches_slots(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")

        def call(slot):
            if slot.index == 1:
                raise env_file.RotatableError("DataForSEO task status 40100")
            return "ok"

        assert env_file.rotate(TEST_GROUP, call) == "ok"

    def test_custom_classify_overrides_default(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        monkeypatch.setenv(TEST_GROUP_VAR + "_2", "s2")

        def call(slot):
            if slot.index == 1:
                raise ValueError("boom")
            return "ok"

        result = env_file.rotate(TEST_GROUP, call, classify=lambda exc: isinstance(exc, ValueError))
        assert result == "ok"


class TestCooldownState:
    def test_state_file_corrupt_warns_and_is_treated_as_empty(self, enable_state):
        path = env_file.state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {{{", encoding="utf-8")
        assert env_file.slot_is_cooling(TEST_GROUP, 1) is False

    def test_state_file_corrupt_does_not_raise(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        path = env_file.state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json {{{", encoding="utf-8")

        def call(slot):
            return "ok"

        assert env_file.rotate(TEST_GROUP, call) == "ok"

    def test_state_file_mode_is_0600_after_write(self, enable_state):
        env_file.report_slot_failure(TEST_GROUP, 1, "HTTP 429")
        path = env_file.state_file_path()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_claude_env_file_empty_disables_state_file_entirely(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_ENV_FILE", "")
        monkeypatch.delenv("AI_CONTENT_ENV_FILE", raising=False)
        assert env_file.state_file_path() is None
        env_file.report_slot_failure(TEST_GROUP, 1, "HTTP 429")
        assert not (tmp_path / ".config").exists()

    def test_ai_content_env_file_empty_also_disables_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AI_CONTENT_ENV_FILE", "")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "not-loaded.env"))
        assert env_file.state_file_path() is None
        env_file.report_slot_failure(TEST_GROUP, 1, "HTTP 429")
        assert not (tmp_path / ".config").exists()

    def test_successful_call_clears_a_recorded_cooldown(self, monkeypatch, enable_state):
        monkeypatch.setenv(TEST_GROUP_VAR, "s1")
        env_file.report_slot_failure(TEST_GROUP, 1, "HTTP 429")
        assert env_file.slot_is_cooling(TEST_GROUP, 1) is True
        env_file.rotate(TEST_GROUP, lambda slot: "ok")
        assert env_file.slot_is_cooling(TEST_GROUP, 1) is False


class TestCheckWithSlots:
    def test_check_with_two_slots_has_no_key_material_and_names_the_count(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "PEXELS_API_KEY=sentinel-not-a-real-key-1\n"
            "PEXELS_API_KEY_2=sentinel-not-a-real-key-2\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        env = dict(os.environ)
        env["CLAUDE_ENV_FILE"] = str(path)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "env_file.py"), "--check"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert proc.returncode == 0
        combined = proc.stdout + proc.stderr
        assert "sentinel-not-a-real-key-1" not in combined
        assert "sentinel-not-a-real-key-2" not in combined
        assert "2 slots" in proc.stdout

    def test_check_subprocess_never_prints_the_sentinel_secret(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "DATAFORSEO_USERNAME=user@example.invalid\n"
            "DATAFORSEO_PASSWORD=sentinel-super-secret-password\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        env = dict(os.environ)
        env["CLAUDE_ENV_FILE"] = str(path)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "env_file.py"), "--check"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert "sentinel-super-secret-password" not in proc.stdout
        assert "sentinel-super-secret-password" not in proc.stderr

class TestStateScoping:
    """Cooldowns must not cross credential sets.

    Keying state by group and slot index alone let a run against one
    credentials file suppress the same slot number in a different one. Found
    by running the rotation check against a throwaway file and then watching
    `--check` report the real key as cooling.
    """

    def _write_env(self, tmp_path, monkeypatch, name, secret):
        path = tmp_path / name
        path.write_text("{0}={1}\n".format(TEST_GROUP_VAR, secret), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(path))
        return path

    def test_cooldown_does_not_leak_between_credentials_files(self, enable_state, monkeypatch):
        self._write_env(enable_state, monkeypatch, "first.env", "REPLACE_ME_ONE")
        env_file.report_slot_failure(TEST_GROUP, 1, "HTTP 429")
        assert env_file.slot_is_cooling(TEST_GROUP, 1) is True

        self._write_env(enable_state, monkeypatch, "second.env", "REPLACE_ME_TWO")
        assert env_file.slot_is_cooling(TEST_GROUP, 1) is False

    def test_switching_back_still_sees_the_original_cooldown(self, enable_state, monkeypatch):
        first = self._write_env(enable_state, monkeypatch, "first.env", "REPLACE_ME_ONE")
        env_file.report_slot_failure(TEST_GROUP, 1, "HTTP 429")
        self._write_env(enable_state, monkeypatch, "second.env", "REPLACE_ME_TWO")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(first))
        assert env_file.slot_is_cooling(TEST_GROUP, 1) is True

    def test_state_file_holds_no_credential_material(self, enable_state, monkeypatch):
        self._write_env(enable_state, monkeypatch, "first.env", "sentinel-not-a-real-key")
        env_file.report_slot_failure(TEST_GROUP, 1, "HTTP 429")
        raw = env_file.state_file_path().read_text(encoding="utf-8")
        assert "sentinel-not-a-real-key" not in raw
