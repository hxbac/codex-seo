# DataForSEO API Cost Reference

Prices verified as of 2026-09-05. Labs, Keywords Data, Backlinks, Domain
Analytics, On-Page, and Content Analysis rose ~20% on 2026-07-01; Merchant
Amazon rose 50%. SERP was not part of that increase.

## Pricing Tiers (USD per call, standard queue)

| Category | Endpoint | Cost/Call | Notes |
|----------|----------|-----------|-------|
| **SERP** | `serp_*_live_advanced` | $0.002 | Per 100 results |
| **SERP** | `serp_*_live_regular` | $0.001 | Lightweight |
| **SERP Images** | `serp_google_images_live_*` | $0.002 | 5x with site:/filetype: operators |
| **Keywords** | `kw_data_google_ads_search_volume` | $0.06/task + $0.00 | Standard queue; $0.09 on live. Bills per task (batch), not per keyword |
| **Keywords** | `kw_data_google_trends_explore` | $0.012 | Per query |
| **Labs** | `dataforseo_labs_*` (all Labs endpoints) | $0.012/task + $0.00012/item | Per task plus per item -- e.g. 1000 keywords = $0.132, not a flat fee |
| **On-Page** | `on_page_instant_pages` | $0.012 | Quick analysis |
| **On-Page** | `on_page_lighthouse` | $0.024 | Full Lighthouse |
| **Backlinks** | `backlinks_*` | $0.024 | Per sub-call ($0.06 for `backlinks_domain_intersection`) |
| **Content** | `content_analysis_*` | $0.024 | Search, summary, trends |
| **Business** | `business_data_*` | $0.05 | Listings search (not part of the 2026-07-01 increase) |
| **AI/GEO** | `ai_optimization_chat_gpt_scraper`, `ai_opt_llm_ment_*` | $0.05 | ChatGPT scraper, LLM mentions (not part of the 2026-07-01 increase) |
| **Merchant** | `merchant_google_*` | $0.02 | Google Shopping (unaffected) |
| **Merchant** | `merchant_amazon_products_search` | $0.03 | Amazon (+50% on 2026-07-01) |
| **Domain** | `domain_analytics_whois_*` | $0.006 | WHOIS data |
| **Domain** | `domain_analytics_technologies_*` | $0.012 | Tech stack |

## Budget Presets

| Preset | Daily Limit | Threshold | Mode | Best For |
|--------|------------|-----------|------|----------|
| **Conservative** | $2.00 | $0.10 | threshold | Learning, testing |
| **Standard** | $10.00 | $0.50 | threshold | Regular audits |
| **Aggressive** | $50.00 | $2.00 | threshold | Agency bulk work |
| **Unlimited** | $999.00 | -- | none | Trusted pipelines |

Configure with: `python scripts/dataforseo_costs.py config --mode threshold --threshold 0.50 --daily-limit 10.00`

## Cost Reduction Tips

- Use `live_regular` instead of `live_advanced` when full SERP features aren't needed (50% savings)
- Batch keywords into single `search_volume` calls instead of individual SERP lookups
- Use `standard` task queue instead of `live` for non-urgent analysis (60-80% savings)
- Avoid `site:` and `filetype:` operators in image SERP queries (5x cost multiplier)
- Cache session results — don't re-fetch the same keyword/domain within a session

## Approval Flow

Before any DataForSEO MCP call:
1. Run `python scripts/dataforseo_costs.py check <endpoint> [--count N]`
2. If `status: "approved"` → proceed
3. If `status: "needs_approval"` → show cost to user, ask to confirm
4. If `status: "blocked"` → inform user daily limit would be exceeded
5. After call completes, log: `python scripts/dataforseo_costs.py log <endpoint> <cost>`

## Warn Endpoints

These endpoints always require user confirmation regardless of approval mode:
- `backlinks_backlinks` (can generate large result sets)
- `backlinks_domain_intersection` (expensive multi-domain comparison)
- `ai_optimization_chat_gpt_scraper` (ChatGPT web scraping)
- `ai_opt_llm_ment_search` (LLM mention tracking)
- `merchant_amazon_products_search` (Amazon product data)
