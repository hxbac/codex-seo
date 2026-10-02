#!/usr/bin/env python3
"""
Import a Google Search Console "Links" report export for Codex SEO.

The Search Console API has no Links endpoint, so the Links report is available
only as a manual export (Links > Export external links). That export is a
SAMPLE chosen by Google, never the complete link profile, and every output of
this script says so.

Accepted inputs (a file, a folder, or a mix inside a folder):
    (a) the zip that "Download" produces, holding one CSV per table
    (b) one CSV (or XLSX) per table
    (c) a Google Sheets export saved as XLSX, one worksheet per table
XLSX needs openpyxl (listed in requirements.txt, installed by the managed
runtime). When openpyxl is missing the script says so in Vietnamese and asks
the user to save as CSV instead.

Table kinds, detected from the HEADERS (never from the file name):
    latest       a linking page per row       -> links[]   (feeds verify_backlinks.py)
    top_sites    a linking site per row       -> referring_domains[]
    top_pages    a target page per row        -> top_pages[]
    top_text     an anchor text per row       -> anchors[]

Column names. VERIFIED means seen in Google's own help page or in a real export
quoted by a third party on 2026-10-02; GUESSED means inferred and kept as a
tolerant alias, so a wrong guess costs nothing but a "table not recognised"
message.

    English
      VERIFIED  "Site" (Top linking sites), "Linking pages", "Target pages"
                (counts in Top linking sites), "Linking page", "Target URL"
                (help page wording), table names "Top linked pages",
                "Top linking sites", "Top linking text", "Latest links".
      GUESSED   "Last crawled", "Target page", "Incoming links",
                "External links", "Text", "Link text".
    Vietnamese (Search Console in vi)
      VERIFIED  table names "Các trang được liên kết hàng đầu",
                "Các trang web liên kết hàng đầu", "Văn bản liên kết hàng đầu",
                "Đường liên kết mới nhất"; headers "Trang liên kết",
                "URL đích", "Văn bản liên kết" (support.google.com, hl=vi).
      GUESSED   every other Vietnamese header below ("Trang web",
                "Các trang liên kết", "Các trang đích", "Trang đích",
                "Lần thu thập dữ liệu gần đây nhất", ...).
Headers are compared lower-cased with accents removed, so "Đường liên kết"
and "duong lien ket" are the same key.

Usage:
    python gsc_links_import.py EXPORT --target example.vn --json
    python gsc_links_import.py EXPORT --target example.vn --links-out links.json
    python verify_backlinks.py --target https://example.vn --links links.json --json
    python gsc_links_import.py EXPORT --target example.vn --verify --max-verify 25

Everything read from the export, and everything fetched by --verify, is
untrusted data: nothing in it is executed or followed as an instruction, URLs
must be http(s), and --verify goes through verify_backlinks.py, which applies
the SSRF checks and the polite per-domain delay.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 50
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILES = 200
MAX_ROWS_PER_TABLE = 100_000  # Google caps "Latest links" at 100,000
DEFAULT_MAX_VERIFY = 25

SAMPLE_NOTE_VI = (
    "Dữ liệu từ Search Console chỉ là một mẫu do Google chọn, không đầy đủ. "
    "Không dùng nó để kết luận tổng số backlink hay tên miền trỏ về."
)

SOURCE_LABEL = "gsc_links_export"


def fold(text: Any) -> str:
    """Lower-case, strip accents and punctuation, collapse spaces."""
    s = str(text if text is not None else "").replace("﻿", "")
    s = s.replace("đ", "d").replace("Đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


# concept -> folded header aliases (see the module docstring for what is verified)
ALIASES: dict[str, tuple[str, ...]] = {
    "site": ("site", "linking site", "referring domain", "domain", "trang web",
             "trang web lien ket", "ten mien"),
    "linking_page": ("linking page", "linking url", "source url", "trang lien ket",
                     "url lien ket"),
    "linking_pages": ("linking pages", "cac trang lien ket", "so trang lien ket",
                      "trang lien ket den"),
    "target": ("target url", "target page", "url dich", "trang dich",
               "trang duoc lien ket", "url duoc lien ket"),
    "target_pages": ("target pages", "cac trang dich", "so trang dich"),
    "incoming": ("incoming links", "external links", "links", "duong lien ket den",
                 "duong lien ket ngoai", "lien ket den", "duong lien ket"),
    "last_crawled": ("last crawled", "last crawl", "last crawled date",
                     "lan thu thap du lieu gan day nhat", "lan thu thap du lieu gan nhat", "lan thu thap gan nhat",
                     "ngay thu thap du lieu gan nhat", "lan thu thap cuoi cung",
                     "thu thap lan cuoi"),
    "anchor": ("link text", "text", "anchor", "anchor text", "van ban lien ket",
               "van ban"),
}
_ALIAS_LOOKUP = {a: k for k, v in ALIASES.items() for a in v}


def map_headers(headers: list[Any]) -> dict[str, int]:
    """Return concept -> column index for the first column matching each concept."""
    found: dict[str, int] = {}
    for i, h in enumerate(headers):
        key = fold(h)
        concept = _ALIAS_LOOKUP.get(key)
        if concept is None and ("thu thap" in key or "crawled" in key):
            concept = "last_crawled"  # tolerant: any "crawled" / "thu thập" header
        if concept and concept not in found:
            found[concept] = i
    return found


def detect_kind(cols: dict[str, int]) -> str | None:
    if "linking_page" in cols:
        return "latest"
    if "site" in cols:
        return "top_sites"
    if "anchor" in cols and "target" not in cols:
        return "top_text"
    if "target" in cols:
        return "top_pages"
    return None


# ----------------------------------------------------------------- reading

class ImportProblem(Exception):
    """A user-facing problem, message already in Vietnamese."""


def decode_bytes(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def read_csv_text(text: str) -> list[list[str]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample.strip() else ""
        delim = max(",;\t", key=first.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    return [r for r in rows if any(c.strip() for c in r)]


def read_xlsx_bytes(raw: bytes, label: str) -> list[tuple[str, list[list[Any]]]]:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        raise ImportProblem(
            f"Chưa đọc được file Excel ({label}) vì thiếu thư viện openpyxl. "
            "Hãy mở file, chọn Tệp > Tải xuống > CSV (hoặc xuất CSV từng bảng "
            "ngay trong Search Console) rồi nhập lại file CSV."
        )
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # corrupt or password protected file
        raise ImportProblem(f"Không mở được file Excel {label}: {type(exc).__name__}. "
                            "Hãy lưu lại dưới dạng CSV.")
    out = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            if len(rows) >= MAX_ROWS_PER_TABLE + 1:
                break
            if any(c not in (None, "") for c in row):
                rows.append(list(row))
        out.append((f"{label}:{ws.title}", rows))
    wb.close()
    return out


def iter_tables(path: Path, notes: list[str]) -> Iterable[tuple[str, list[list[Any]]]]:
    """Yield (label, rows) for every table found in a file, folder or zip."""
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())[:MAX_FILES]
        for p in files:
            if p.suffix.lower() in (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".zip"):
                yield from iter_tables(p, notes)
        return
    suffix = path.suffix.lower()
    if path.stat().st_size > MAX_FILE_BYTES:
        notes.append(f"Bỏ qua {path.name}: file lớn hơn 50 MB.")
        return
    if suffix == ".zip":
        yield from _iter_zip(path, notes)
    elif suffix in (".xlsx", ".xlsm"):
        yield from read_xlsx_bytes(path.read_bytes(), path.name)
    elif suffix in (".csv", ".tsv", ".txt"):
        yield path.name, read_csv_text(decode_bytes(path.read_bytes()))
    else:
        notes.append(f"Bỏ qua {path.name}: không phải CSV, XLSX hoặc ZIP.")


def _iter_zip(path: Path, notes: list[str]) -> Iterable[tuple[str, list[list[Any]]]]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        notes.append(f"Bỏ qua {path.name}: file zip bị lỗi.")
        return
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_MEMBERS or sum(i.file_size for i in infos) > MAX_ZIP_TOTAL_BYTES:
            notes.append(f"Bỏ qua {path.name}: zip có quá nhiều file hoặc quá lớn.")
            return
        for info in infos:
            name = Path(info.filename).name  # never use the member path to touch disk
            low = name.lower()
            if info.file_size > MAX_FILE_BYTES:
                notes.append(f"Bỏ qua {name} trong zip: lớn hơn 50 MB.")
                continue
            raw = zf.read(info)
            label = f"{path.name}/{name}"
            if low.endswith((".csv", ".tsv", ".txt")):
                yield label, read_csv_text(decode_bytes(raw))
            elif low.endswith((".xlsx", ".xlsm")):
                yield from read_xlsx_bytes(raw, label)


# ------------------------------------------------------------ normalising

def cell(row: list[Any], idx: int | None) -> str:
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    v = row[idx]
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return re.sub(r"[\x00-\x1f\x7f]", "", str(v)).strip()


def to_int(text: str) -> int | None:
    digits = re.sub(r"[.,\s ]", "", text)
    return int(digits) if digits.isdigit() else None


def to_iso_date(text: str) -> str | None:
    """ISO date when parseable. GUESSED formats: ISO, d/m/Y (vi), m/d/Y only when unambiguous."""
    t = text.strip()
    if not t:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$", t)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 and b <= 12:
            d, mo = a, b
        elif b > 12 and a <= 12:
            d, mo = b, a
        else:
            d, mo = a, b  # ambiguous: Vietnamese UI uses day first
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return t  # keep the raw text rather than lose it


def clean_url(text: str) -> str | None:
    t = text.strip()
    if not t or re.search(r"\s", t):
        return None
    p = urlparse(t)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return t


def host_of(text: str) -> str | None:
    t = text.strip().lower()
    if not t or re.search(r"\s", t):
        return None
    if "://" not in t:
        t = "http://" + t
    host = urlparse(t).hostname
    if not host or "." not in host:
        return None
    return host[4:] if host.startswith("www.") else host


def same_site(host: str, target_domain: str) -> bool:
    return host == target_domain or host.endswith("." + target_domain)


def normalise_target_domain(value: str) -> str:
    host = host_of(value.replace("sc-domain:", ""))
    if not host:
        raise ImportProblem(f"--target không hợp lệ: {value!r}. Ví dụ: example.vn")
    return host


def import_tables(path: Path, target: str, notes: list[str]) -> dict[str, Any]:
    target_domain = normalise_target_domain(target)
    default_target = f"https://{target_domain}/"
    out: dict[str, Any] = {
        "links": [], "referring_domains": [], "top_pages": [], "anchors": [],
    }
    seen_links: set[tuple[str, str]] = set()
    tables: list[dict[str, Any]] = []
    dropped = {"internal": 0, "bad_url": 0, "duplicate": 0}

    for label, rows in iter_tables(path, notes):
        if not rows:
            continue
        # header row = first row that maps to a known table kind (skips title rows)
        header_idx, cols, kind = None, {}, None
        for i, r in enumerate(rows[:5]):
            cols = map_headers(r)
            kind = detect_kind(cols)
            if kind:
                header_idx = i
                break
        if kind is None or header_idx is None:
            notes.append(f"Không nhận ra bảng trong {label}: tiêu đề cột lạ "
                         f"({', '.join(str(c) for c in rows[0][:4])}).")
            continue
        body = rows[header_idx + 1:MAX_ROWS_PER_TABLE + header_idx + 1]
        count = 0
        for r in body:
            if kind == "latest":
                src = clean_url(cell(r, cols.get("linking_page")))
                if not src:
                    dropped["bad_url"] += 1
                    continue
                host = host_of(src)
                if host and same_site(host, target_domain):
                    dropped["internal"] += 1
                    continue
                tgt = clean_url(cell(r, cols.get("target"))) or default_target
                key = (src, tgt)
                if key in seen_links:
                    dropped["duplicate"] += 1
                    continue
                seen_links.add(key)
                out["links"].append({
                    "source_url": src,
                    "source_domain": host,
                    "target_url": tgt,
                    "linking_pages": 1,
                    "last_crawled": to_iso_date(cell(r, cols.get("last_crawled"))),
                })
            elif kind == "top_sites":
                host = host_of(cell(r, cols["site"]))
                if not host:
                    dropped["bad_url"] += 1
                    continue
                if same_site(host, target_domain):
                    dropped["internal"] += 1
                    continue
                out["referring_domains"].append({
                    "source_domain": host,
                    "linking_pages": to_int(cell(r, cols.get("linking_pages"))),
                    "target_pages": to_int(cell(r, cols.get("target_pages"))),
                })
            elif kind == "top_pages":
                tgt = clean_url(cell(r, cols["target"]))
                if not tgt:
                    dropped["bad_url"] += 1
                    continue
                out["top_pages"].append({
                    "target_url": tgt,
                    "linking_pages": to_int(cell(r, cols.get("incoming")))
                    if "incoming" in cols else to_int(cell(r, cols.get("linking_pages"))),
                })
            elif kind == "top_text":
                text = cell(r, cols["anchor"])
                if not text:
                    continue
                out["anchors"].append({
                    "anchor": text[:200],
                    "linking_pages": to_int(cell(r, cols.get("linking_pages")))
                    if "linking_pages" in cols else to_int(cell(r, cols.get("incoming"))),
                })
            count += 1
        tables.append({"file": label, "kind": kind, "rows": count})

    out["tables"] = tables
    out["dropped"] = dropped
    out["target_domain"] = target_domain
    out["links"].sort(key=lambda x: x.get("last_crawled") or "", reverse=True)
    return out


# --------------------------------------------------------------- verifying

def verify_links(links: list[dict], max_verify: int, head_only: bool) -> dict[str, Any]:
    """Run verify_backlinks over the newest `max_verify` links, grouped by target."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import verify_backlinks  # lazy: needs requests

    chosen = links[:max_verify]
    groups: dict[str, list[dict]] = {}
    for item in chosen:
        groups.setdefault(item["target_url"], []).append({"source_url": item["source_url"]})
    summary: dict[str, int] = {}
    results: list[dict] = []
    for tgt, grp in groups.items():
        res = verify_backlinks.verify_backlinks(tgt, grp, head_only=head_only)
        results.extend(res["data"]["results"])
        for k, v in res["data"]["summary"].items():
            summary[k] = summary.get(k, 0) + v
    status_by_key = {(r["source_url"], r["target_url"]): r for r in results}
    for item in chosen:
        r = status_by_key.get((item["source_url"], item["target_url"]))
        if r:
            item["verify_status"] = r["status"]
            item["verify_http_status"] = r.get("http_status")
    return {
        "checked": len(chosen), "available": len(links),
        "capped": len(links) > len(chosen),
        "head_only": head_only, "summary": summary, "results": results,
    }


def build_report(path: Path, target: str, verify: bool, max_verify: int,
                 head_only: bool) -> dict[str, Any]:
    notes: list[str] = []
    imp = import_tables(path, target, notes)
    if not imp["tables"]:
        raise ImportProblem(
            "Không tìm thấy bảng nào trong Search Console Links. Hãy xuất từ "
            "Search Console > Đường liên kết > Xuất đường liên kết ngoài, rồi "
            "nhập file CSV, XLSX hoặc zip tải về. " + " ".join(notes)
        )
    verify_data = None
    if verify:
        if imp["links"]:
            verify_data = verify_links(imp["links"], max_verify, head_only)
        else:
            notes.append("Không có bảng 'Đường liên kết mới nhất' nên chưa có link nào để kiểm tra.")
    domains = {l["source_domain"] for l in imp["links"] if l.get("source_domain")}
    domains |= {d["source_domain"] for d in imp["referring_domains"]}
    return {
        "status": "success",
        "data": {
            "target_domain": imp["target_domain"],
            "completeness": "sample",
            "sample_note": SAMPLE_NOTE_VI,
            "tables": imp["tables"],
            "counts": {
                "links": len(imp["links"]),
                "referring_domains_in_sample": len(domains),
                "top_pages": len(imp["top_pages"]),
                "anchors": len(imp["anchors"]),
                "dropped": imp["dropped"],
            },
            "links": imp["links"],
            "referring_domains": imp["referring_domains"],
            "top_pages": imp["top_pages"],
            "anchors": imp["anchors"],
            "verify": verify_data,
            "notes": notes,
        },
        "error": None,
        "metadata": {
            "source": SOURCE_LABEL,
            "confidence": "sample",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }


def print_text(report: dict[str, Any]) -> None:
    d = report["data"]
    c = d["counts"]
    print(f"Nhập Search Console Links cho {d['target_domain']}")
    print(f"  {d['sample_note']}")
    for t in d["tables"]:
        print(f"  Bảng {t['kind']}: {t['rows']} dòng ({t['file']})")
    print(f"  Link mới nhất: {c['links']}, tên miền trong mẫu: {c['referring_domains_in_sample']}, "
          f"trang được liên kết: {c['top_pages']}, anchor: {c['anchors']}")
    dr = c["dropped"]
    if any(dr.values()):
        print(f"  Đã bỏ: {dr['internal']} link nội bộ, {dr['bad_url']} URL lỗi, "
              f"{dr['duplicate']} trùng")
    v = d.get("verify")
    if v:
        s = v["summary"]
        print(f"  Kiểm tra {v['checked']}/{v['available']} link: "
              f"còn {s.get('verified', 0)}, mất {s.get('lost', 0)}, "
              f"đã gỡ link {s.get('link_removed', 0)}, chuyển hướng {s.get('moved', 0)}, "
              f"không chắc (JS) {s.get('unverifiable_js', 0)}, lỗi {s.get('error', 0)}")
        if v["capped"]:
            print("  Chỉ kiểm tra các link mới nhất; chạy lại với --max-verify lớn hơn nếu cần.")
    for n in d["notes"]:
        print(f"  Lưu ý: {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a Search Console Links export")
    ap.add_argument("path", help="CSV, XLSX, zip, or a folder holding them")
    ap.add_argument("--target", required=True, help="your site's domain, e.g. example.vn")
    ap.add_argument("--out", help="write the full JSON report to this file")
    ap.add_argument("--links-out", help="write the bare link list that "
                    "verify_backlinks.py --links reads")
    ap.add_argument("--verify", action="store_true",
                    help="fetch the newest links to see which still exist (polite, capped)")
    ap.add_argument("--max-verify", type=int, default=DEFAULT_MAX_VERIFY)
    ap.add_argument("--head-only", action="store_true",
                    help="with --verify, only check that the linking page exists")
    ap.add_argument("--json", action="store_true", help="print the JSON report")
    args = ap.parse_args()

    p = Path(args.path).expanduser()
    try:
        if not p.exists():
            raise ImportProblem(f"Không thấy file hoặc thư mục: {args.path}")
        report = build_report(p, args.target, args.verify,
                              max(1, min(args.max_verify, 500)), args.head_only)
    except ImportProblem as exc:
        err = {"status": "error", "data": None, "error": str(exc),
               "metadata": {"source": SOURCE_LABEL}}
        if args.json:
            print(json.dumps(err, ensure_ascii=False, indent=2))
        else:
            print(f"Lỗi: {exc}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.links_out:
        bare = [{k: v for k, v in l.items() if k in
                 ("source_url", "source_domain", "target_url", "linking_pages", "last_crawled")}
                for l in report["data"]["links"]]
        Path(args.links_out).write_text(json.dumps(bare, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
