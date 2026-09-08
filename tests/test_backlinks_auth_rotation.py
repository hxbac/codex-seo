"""Phase C: backlinks_auth.py reports MOZ_API_KEY rotation slot counts.

backlinks_auth.py is a credential reporter, not a caller: it must never
call the Moz API or rotate a key itself, only report how many slots are
configured. No credential value here is real; every one is a sentinel.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import backlinks_auth  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_moz_vars(monkeypatch):
    for name in list(os.environ):
        if name.startswith("MOZ_API_KEY"):
            monkeypatch.delenv(name, raising=False)


def test_check_reports_zero_slots_when_only_config_file_is_set():
    with patch.object(backlinks_auth, "load_config", return_value={"moz_api_key": "mozscape-key"}):
        result = backlinks_auth.check_credentials("moz")
    assert result["slots"] == 0
    assert result["available"] is True


def test_check_reports_slot_count_from_env(monkeypatch):
    monkeypatch.setenv("MOZ_API_KEY", "sentinel-not-a-real-key-1")
    monkeypatch.setenv("MOZ_API_KEY_2", "sentinel-not-a-real-key-2")
    with patch.object(backlinks_auth, "load_config", return_value={"moz_api_key": "sentinel-not-a-real-key-1"}):
        result = backlinks_auth.check_credentials("moz")
    assert result["slots"] == 2


def test_check_no_key_at_all_reports_zero_slots():
    with patch.object(backlinks_auth, "load_config", return_value={"moz_api_key": None}):
        result = backlinks_auth.check_credentials("moz")
    assert result["slots"] == 0
    assert result["available"] is False
