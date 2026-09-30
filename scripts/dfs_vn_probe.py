#!/usr/bin/env python3
"""
dfs_vn_probe.py -- Kiem tra tai khoan DataForSEO cho thi truong Viet Nam.

CHI GOI CAC ENDPOINT MIEN PHI ($0.00):
  /v3/appendix/user_data                        -> so du, rate limit, gia thuc te
  /v3/dataforseo_labs/locations_and_languages   -> Labs co ho tro VN/vi khong
  /v3/keywords_data/google_ads/locations        -> Keyword Planner co VN khong
  /v3/keywords_data/google_ads/languages        -> co tieng Viet khong
  /v3/serp/google/organic/locations             -> so location VN cho SERP

Bien moi truong (chap nhan ca 2 kieu ten vi claude-seo va claude-blog dat khac nhau):
  DATAFORSEO_USERNAME / DATAFORSEO_LOGIN
  DATAFORSEO_PASSWORD

Chay:
  python3 dfs_vn_probe.py            # ban tom tat
  python3 dfs_vn_probe.py --json     # JSON de dua vao script khac
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "https://api.dataforseo.com"
VN_CODE = 2704
VN_LANG = "vi"
TIMEOUT = 45

# Gia niem yet DataForSEO sau dot cap nhat 01-07-2026 (USD).
# Dung de doi chieu voi gia thuc te tra ve tu /appendix/user_data (co the khac
# neu tai khoan duoc ap dung bang gia rieng).
REFERENCE_PRICES = {
    "serp_organic_standard": 0.0006,
    "serp_organic_live_advanced": 0.002,
    "kw_google_ads_standard_per_task": 0.06,
    "kw_google_ads_live_per_task": 0.09,
    "labs_per_task": 0.012,
    "labs_per_item": 0.00012,
}


def creds() -> tuple[str, str]:
    user = os.environ.get("DATAFORSEO_USERNAME") or os.environ.get("DATAFORSEO_LOGIN")
    pwd = os.environ.get("DATAFORSEO_PASSWORD")
    if not user or not pwd:
        sys.exit(
            "Thieu credential. Dat bien moi truong roi chay lai:\n"
            "  export DATAFORSEO_USERNAME='email-dang-ky@...'\n"
            "  export DATAFORSEO_PASSWORD='api-password'\n"
            "(claude-blog dung ten DATAFORSEO_LOGIN -- script nay chap nhan ca hai.)"
        )
    return user, pwd


def call(path: str, user: str, pwd: str) -> dict:
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        return {"_error": f"HTTP {exc.code}", "_body": body}
    except Exception as exc:  # noqa: BLE001 - bao cao moi loi mang cho nguoi dung
        return {"_error": type(exc).__name__, "_body": str(exc)[:400]}


def first_result(payload: dict) -> list:
    tasks = payload.get("tasks") or []
    if not tasks:
        return []
    return tasks[0].get("result") or []


def probe(user: str, pwd: str) -> dict:
    out: dict = {"errors": []}

    # 1. So du + rate limit
    data = call("/v3/appendix/user_data", user, pwd)
    if "_error" in data:
        out["errors"].append({"step": "user_data", **data})
    else:
        res = (first_result(data) or [{}])[0]
        out["account"] = {
            "login": res.get("login"),
            "balance_usd": res.get("money", {}).get("balance"),
            "spent_total_usd": res.get("money", {}).get("total"),
            "limit_per_minute": res.get("rates", {}).get("limits", {}).get("minute"),
            "limit_per_day": res.get("rates", {}).get("limits", {}).get("day"),
        }
        # Gia thuc te ap cho tai khoan nay (neu API tra ve)
        prices = res.get("price") or {}
        out["account"]["price_block_present"] = bool(prices)

    # 2. Labs co VN + tieng Viet khong (day la cho hay thieu nhat)
    data = call("/v3/dataforseo_labs/locations_and_languages", user, pwd)
    if "_error" in data:
        out["errors"].append({"step": "labs_locations", **data})
    else:
        rows = first_result(data)
        vn = [r for r in rows if r.get("location_code") == VN_CODE]
        langs = []
        for r in vn:
            for lang in r.get("available_languages") or []:
                langs.append(
                    {
                        "language_code": lang.get("language_code"),
                        "language_name": lang.get("language_name"),
                        "keywords": lang.get("keywords"),
                        "serps": lang.get("serps"),
                    }
                )
        out["labs_vietnam"] = {
            "supported": bool(vn),
            "location_types": sorted({r.get("location_type") for r in vn if r.get("location_type")}),
            "languages": langs,
            "has_vietnamese": any(l["language_code"] == VN_LANG for l in langs),
        }

    # 3. Keyword Planner (Google Ads) co VN khong
    data = call(f"/v3/keywords_data/google_ads/locations/{VN_CODE}", user, pwd)
    if "_error" in data:
        data = call("/v3/keywords_data/google_ads/locations", user, pwd)
    if "_error" in data:
        out["errors"].append({"step": "google_ads_locations", **data})
    else:
        rows = first_result(data)
        vn = [r for r in rows if r.get("location_code") == VN_CODE]
        out["google_ads_vietnam"] = {
            "supported": bool(vn),
            "sample": vn[:1],
            "vn_rows_total": len(vn),
        }

    data = call("/v3/keywords_data/google_ads/languages", user, pwd)
    if "_error" in data:
        out["errors"].append({"step": "google_ads_languages", **data})
    else:
        rows = first_result(data)
        vi = [r for r in rows if r.get("language_code") == VN_LANG]
        out["google_ads_vietnamese"] = {"supported": bool(vi), "sample": vi[:1]}

    # 4. SERP co bao nhieu diem dia ly VN (cho local SEO)
    data = call("/v3/serp/google/organic/locations/vn", user, pwd)
    if "_error" in data:
        out["errors"].append({"step": "serp_locations_vn", **data})
    else:
        rows = first_result(data)
        out["serp_vietnam"] = {
            "total_locations": len(rows),
            "cities_sample": [
                r.get("location_name")
                for r in rows
                if r.get("location_type") in {"City", "Municipality"}
            ][:5],
        }

    return out


def render(out: dict) -> None:
    acc = out.get("account") or {}
    print("=" * 66)
    print("DATAFORSEO -- KIEM TRA SAN SANG CHO THI TRUONG VIET NAM")
    print("=" * 66)
    print(f"  Tai khoan       : {acc.get('login', '?')}")
    bal = acc.get("balance_usd")
    print(f"  So du           : ${bal:.2f}" if isinstance(bal, (int, float)) else "  So du           : ?")
    print(f"  Rate limit      : {acc.get('limit_per_minute', '?')}/phut, {acc.get('limit_per_day', '?')}/ngay")

    labs = out.get("labs_vietnam") or {}
    print("\n-- DataForSEO Labs (keyword ideas, difficulty, competitor) --")
    if labs.get("has_vietnamese"):
        for lang in labs["languages"]:
            if lang["language_code"] == VN_LANG:
                print(f"  OK  vi @ 2704 -- {lang['keywords']:,} keyword / {lang['serps']:,} SERP trong DB")
    else:
        print("  CANH BAO: khong thay tieng Viet trong Labs DB -> dung SERP API thay the")
    types = labs.get("location_types") or []
    if types and types != ["Country"]:
        print(f"  Cap dia ly ho tro: {', '.join(types)}")
    elif types:
        print("  Cap dia ly ho tro: CHI Country (2704) -- khong co cap tinh/thanh")

    ads = out.get("google_ads_vietnam") or {}
    adl = out.get("google_ads_vietnamese") or {}
    print("\n-- Keywords Data / Google Ads (search volume that) --")
    print(f"  Location VN     : {'OK' if ads.get('supported') else 'KHONG THAY'}")
    print(f"  Language vi     : {'OK' if adl.get('supported') else 'KHONG THAY'}")

    serp = out.get("serp_vietnam") or {}
    if serp:
        print("\n-- SERP API (local/maps) --")
        print(f"  So diem dia ly VN: {serp.get('total_locations', '?')}")
        if serp.get("cities_sample"):
            print(f"  Vi du            : {', '.join(serp['cities_sample'])}")

    print("\n-- Uoc tinh chi phi 1 thang (theo gia niem yet 01-07-2026) --")
    p = REFERENCE_PRICES
    plan = [
        ("Search volume 4000 tu khoa (4 task x 1000, standard)", 4 * p["kw_google_ads_standard_per_task"]),
        ("Labs keyword ideas 8 seed (200 item/seed)", 8 * (p["labs_per_task"] + 200 * p["labs_per_item"])),
        ("SERP top-10 cho 60 tu khoa (standard queue)", 60 * p["serp_organic_standard"]),
        ("Bulk keyword difficulty 1000 tu (1 task)", p["labs_per_task"] + 1000 * p["labs_per_item"]),
    ]
    total = 0.0
    for label, cost in plan:
        total += cost
        print(f"  {label:<52} ${cost:>6.3f}")
    print(f"  {'TONG':<52} ${total:>6.3f}")
    if isinstance(bal, (int, float)) and total:
        print(f"  -> So du hien tai du cho ~{int(bal / total)} thang o muc nay")

    if out.get("errors"):
        print("\n-- LOI --")
        for e in out["errors"]:
            print(f"  [{e.get('step')}] {e.get('_error')}: {e.get('_body', '')[:160]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="In JSON thay vi ban tom tat")
    args = ap.parse_args()

    user, pwd = creds()
    out = probe(user, pwd)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        render(out)
    sys.exit(1 if out.get("errors") else 0)


if __name__ == "__main__":
    main()
