"""Phase C: key rotation wiring in scripts/dataforseo_merchant.py.

Every test mocks requests.post/requests.get directly on the imported
module. No test in this file performs a real network call, and no
credential value here is real; every one is a sentinel.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import dataforseo_merchant as dfm  # noqa: E402


def _fake_response(payload):
    return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)


def _product_item():
    return {
        "title": "Water filter",
        "price": "129.99",
        "seller": "Shop A",
        "rating": {"value": 4.5},
        "reviews_count": 10,
        "url": "https://shop.example/a",
        "availability": "in_stock",
    }


@pytest.fixture(autouse=True)
def _clean_dataforseo_vars(monkeypatch):
    prefixes = ("DATAFORSEO_USERNAME", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD")
    for name in list(os.environ):
        if name.startswith(prefixes):
            monkeypatch.delenv(name, raising=False)


def _search_args(**overrides):
    base = dict(
        keyword="water filter", location=2840, language="en", marketplace="google",
        depth=100, sort_by=None, price_min=None, price_max=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRotation:
    def test_slot1_task_40200_slot2_succeeds(self, monkeypatch, capsys):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "user-1")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw-1")
        monkeypatch.setenv("DATAFORSEO_USERNAME_2", "user-2")
        monkeypatch.setenv("DATAFORSEO_PASSWORD_2", "sentinel-pw-2")
        monkeypatch.setattr(dfm.time, "sleep", lambda *_a: None)

        rejected_post = _fake_response(
            {"status_code": 20000, "tasks": [{"status_code": 40200, "status_message": "Auth error"}]}
        )
        ok_post = _fake_response(
            {"status_code": 20000, "tasks": [{"id": "task-2", "status_code": 20000, "result": []}]}
        )
        get_response = _fake_response(
            {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": [_product_item()]}]}]}
        )
        posts = [rejected_post, ok_post]
        monkeypatch.setattr(dfm.requests, "post", lambda *a, **k: posts.pop(0))
        monkeypatch.setattr(dfm.requests, "get", lambda *a, **k: get_response)

        dfm.cmd_search(_search_args())

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "success"
        assert out["total_results"] == 1

    def test_slot1_40501_does_not_rotate(self, monkeypatch, capsys):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "user-1")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw-1")
        monkeypatch.setenv("DATAFORSEO_USERNAME_2", "user-2")
        monkeypatch.setenv("DATAFORSEO_PASSWORD_2", "sentinel-pw-2")

        post_response = _fake_response(
            {"status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
        )
        calls = []
        monkeypatch.setattr(dfm.requests, "post", lambda *a, **k: calls.append(1) or post_response)

        dfm.cmd_search(_search_args())

        out = json.loads(capsys.readouterr().out)
        assert "error" in out
        assert out["error"] != "all_slots_failed"
        assert len(calls) == 1

    def test_slot1_40400_does_not_rotate(self, monkeypatch, capsys):
        """40400 (Invalid Path) is a request bug too: never rotate on it,
        even with a second slot configured."""
        monkeypatch.setenv("DATAFORSEO_USERNAME", "user-1")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw-1")
        monkeypatch.setenv("DATAFORSEO_USERNAME_2", "user-2")
        monkeypatch.setenv("DATAFORSEO_PASSWORD_2", "sentinel-pw-2")

        post_response = _fake_response(
            {"status_code": 20000, "tasks": [{"status_code": 40400, "status_message": "Invalid Path"}]}
        )
        calls = []
        monkeypatch.setattr(dfm.requests, "post", lambda *a, **k: calls.append(1) or post_response)

        dfm.cmd_search(_search_args())

        out = json.loads(capsys.readouterr().out)
        assert "error" in out
        assert out["error"] != "all_slots_failed"
        assert len(calls) == 1

    def test_slot1_http_500_raises_does_not_rotate(self, monkeypatch):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "user-1")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw-1")
        monkeypatch.setenv("DATAFORSEO_USERNAME_2", "user-2")
        monkeypatch.setenv("DATAFORSEO_PASSWORD_2", "sentinel-pw-2")

        import requests as real_requests

        def raise_500():
            raise real_requests.exceptions.HTTPError(
                "500 server error", response=SimpleNamespace(status_code=500)
            )

        error_response = SimpleNamespace(json=lambda: {}, raise_for_status=raise_500)
        calls = []
        monkeypatch.setattr(dfm.requests, "post", lambda *a, **k: calls.append(1) or error_response)

        with pytest.raises(real_requests.exceptions.HTTPError):
            dfm.cmd_search(_search_args())
        assert len(calls) == 1

    def test_only_one_slot_429_all_slots_failed(self, monkeypatch, capsys):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "user-1")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "sentinel-pw-1")

        import requests as real_requests

        def raise_429():
            raise real_requests.exceptions.HTTPError(
                "429 rejected", response=SimpleNamespace(status_code=429)
            )

        error_response = SimpleNamespace(json=lambda: {}, raise_for_status=raise_429)
        monkeypatch.setattr(dfm.requests, "post", lambda *a, **k: error_response)

        dfm.cmd_search(_search_args())

        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "all_slots_failed"
        assert "dataforseo" in out["message"]

    def test_no_slot_configured_credentials_missing_message_preserved(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            dfm.cmd_search(_search_args())
        assert exc_info.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "error": "missing_credentials",
            "message": (
                "DataForSEO credentials not found. Set DATAFORSEO_USERNAME and "
                "DATAFORSEO_PASSWORD environment variables."
            ),
        }


class TestSingleKeyRegression:
    def test_single_key_search_unchanged(self, monkeypatch, capsys):
        monkeypatch.setenv("DATAFORSEO_USERNAME", "u")
        monkeypatch.setenv("DATAFORSEO_PASSWORD", "p")
        monkeypatch.setattr(dfm.time, "sleep", lambda *_a: None)

        post_response = _fake_response(
            {"status_code": 20000, "tasks": [{"id": "task-42", "status_code": 20000, "result": []}]}
        )
        get_response = _fake_response(
            {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"items": [_product_item()]}]}]}
        )
        monkeypatch.setattr(dfm.requests, "post", lambda *a, **k: post_response)
        monkeypatch.setattr(dfm.requests, "get", lambda *a, **k: get_response)

        dfm.cmd_search(_search_args())

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "success"
        assert out["total_results"] == 1
        assert out["products"][0]["title"] == "Water filter"
