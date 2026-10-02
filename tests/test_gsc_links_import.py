"""Search Console Links export import: kinds, languages, containers, verify chain."""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import gsc_links_import as gli  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "gsc_links"
TARGET = "naneuron.com"


def _report(path, **kw):
    return gli.build_report(Path(path), TARGET, kw.get("verify", False),
                            kw.get("max_verify", 25), kw.get("head_only", False))["data"]


def test_english_files_detect_every_kind():
    d = _report(FIX / "en_latest_links.csv")
    assert [t["kind"] for t in d["tables"]] == ["latest"]
    # javascript: URL, internal link and the duplicate are dropped
    assert [l["source_url"] for l in d["links"]] == [
        "https://blog.example.org/post-1",
        "https://www.news-vn.example/bai-viet?id=2",
    ]
    assert d["counts"]["dropped"] == {"internal": 1, "bad_url": 1, "duplicate": 1}
    assert d["links"][0]["last_crawled"] == "2026-09-20"
    assert d["links"][0]["linking_pages"] == 1
    assert d["completeness"] == "sample"

    s = _report(FIX / "en_top_linking_sites.csv")["referring_domains"]
    assert {r["source_domain"]: r["linking_pages"] for r in s} == {
        "blog.example.org": 12, "news-vn.example": 4}  # own domain dropped, www stripped

    assert _report(FIX / "en_top_linked_pages.csv")["top_pages"][0]["linking_pages"] == 40
    assert _report(FIX / "en_top_linking_text.csv")["anchors"][0]["anchor"] == "Naneuron"


def test_vietnamese_headers_semicolon_bom_and_dmy_dates():
    d = _report(FIX / "vi_latest_links.csv")
    assert d["tables"][0]["kind"] == "latest"
    assert d["links"][0]["last_crawled"] == "2026-09-20"
    assert d["links"][1]["target_url"] == "https://naneuron.com/vi/gia/"

    s = _report(FIX / "vi_top_linking_sites.csv")["referring_domains"]
    assert {r["source_domain"]: r["linking_pages"] for r in s}["news-vn.example"] == 1234
    assert _report(FIX / "vi_top_linked_pages.csv")["top_pages"][0]["linking_pages"] == 8
    assert _report(FIX / "vi_top_linking_text.csv")["anchors"][0]["linking_pages"] == 15


def test_folder_and_zip_with_mixed_languages(tmp_path):
    z = tmp_path / "naneuron.com-Links.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name in ("en_latest_links.csv", "vi_top_linking_sites.csv", "vi_top_linked_pages.csv"):
            zf.write(FIX / name, "../evil/" + name)  # member paths are never trusted
    d = _report(z)
    assert sorted(t["kind"] for t in d["tables"]) == ["latest", "top_pages", "top_sites"]
    folder = _report(FIX)
    assert len(folder["tables"]) == 8


def test_xlsx_google_sheets_style_one_sheet_per_table(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from datetime import datetime

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Đường liên kết mới nhất"
    ws.append(["Trang liên kết", "URL đích", "Lần thu thập dữ liệu gần đây nhất"])
    ws.append(["https://blog.example.org/post-1", "https://naneuron.com/vi/", datetime(2026, 9, 20)])
    ws2 = wb.create_sheet("Top linking sites")
    ws2.append(["Site", "Linking pages", "Target pages"])
    ws2.append(["blog.example.org", 12, 3])
    f = tmp_path / "gsc.xlsx"
    wb.save(f)
    d = _report(f)
    assert sorted(t["kind"] for t in d["tables"]) == ["latest", "top_sites"]
    assert d["links"][0]["last_crawled"] == "2026-09-20"
    assert d["referring_domains"][0]["linking_pages"] == 12


def test_xlsx_without_openpyxl_says_save_as_csv(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    f = tmp_path / "x.xlsx"
    f.write_bytes(b"PK")
    with pytest.raises(gli.ImportProblem) as e:
        _report(f)
    assert "CSV" in str(e.value)


def test_unknown_table_gives_vietnamese_error(tmp_path):
    f = tmp_path / "other.csv"
    f.write_text("Query,Clicks\nfoo,1\n", encoding="utf-8")
    with pytest.raises(gli.ImportProblem) as e:
        _report(f)
    assert "Search Console" in str(e.value)


def test_cli_links_out_chains_into_verify_backlinks(tmp_path, monkeypatch, capsys):
    import verify_backlinks

    out = tmp_path / "links.json"
    monkeypatch.setattr(sys, "argv", ["x", str(FIX / "en_latest_links.csv"),
                                      "--target", TARGET, "--links-out", str(out)])
    assert gli.main() == 0
    links = json.loads(out.read_text(encoding="utf-8"))
    assert all(l["source_url"] for l in links)

    seen = []

    def fake(source_url, target_url, head_only=False, timeout=30):
        seen.append((source_url, target_url))
        return {"source_url": source_url, "target_url": target_url,
                "status": "verified" if "blog" in source_url else "lost", "http_status": 200}

    monkeypatch.setattr(verify_backlinks, "verify_single_backlink", fake)
    res = verify_backlinks.verify_backlinks("https://naneuron.com", links)
    assert res["data"]["summary"]["verified"] == 1 and res["data"]["summary"]["lost"] == 1


def test_verify_flag_is_capped_and_marks_rows(monkeypatch):
    import verify_backlinks

    def fake(source_url, target_url, head_only=False, timeout=30):
        return {"source_url": source_url, "target_url": target_url,
                "status": "lost", "http_status": 404}

    monkeypatch.setattr(verify_backlinks, "verify_single_backlink", fake)
    d = _report(FIX / "en_latest_links.csv", verify=True, max_verify=1)
    assert d["verify"]["checked"] == 1 and d["verify"]["capped"] is True
    assert d["links"][0]["verify_status"] == "lost"
    assert "verify_status" not in d["links"][1]
