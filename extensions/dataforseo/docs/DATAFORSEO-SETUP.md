# DataForSEO Account Setup

Step-by-step guide to getting DataForSEO API credentials for the Codex SEO extension.

## 1. Create Account

1. Go to [app.dataforseo.com/register](https://app.dataforseo.com/register)
2. Sign up with your email address
3. Verify your email

New accounts include a free trial balance for testing.

## 2. Find API Credentials

1. Log in to [app.dataforseo.com](https://app.dataforseo.com)
2. Go to **API Access** in the left sidebar
3. Your credentials are:
   - **Username**: Your registered email address
   - **Password**: Your API password (set during registration)

These are the values you'll enter when running the extension installer.

## 3. Understanding Credits

DataForSEO uses a credit-based system:

- Each API call costs a small number of credits
- Different endpoints have different costs
- Credits are purchased in advance
- Monitor usage at [app.dataforseo.com/dashboard](https://app.dataforseo.com/dashboard)

**Typical costs per call, verified as of 2026-09-05** (Labs, Keywords Data,
Backlinks, Domain Analytics, On-Page, and Content Analysis rose ~20% on
2026-07-01; Merchant Amazon rose 50%; SERP was unaffected):

| Endpoint Type | Approximate Cost |
|--------------|-----------------|
| SERP (single query) | $0.001-0.003 |
| Keyword volume (per task/batch, not per keyword) | $0.06 standard / $0.09 live |
| DataForSEO Labs (per task + per item) | $0.012/task + $0.00012/item |
| Backlink summary | $0.024 |
| Backlink list | $0.024 (domain intersection: $0.06) |
| On-page crawl (per page) | $0.012-0.024 |
| AI optimization (per call) | $0.05 |

See `../../../scripts/dataforseo_costs.py` for the exact per-endpoint model and
`python scripts/dataforseo_costs.py estimate <endpoint> --count N` to check a
specific call before spending. Free readiness/probe endpoints (`appendix/user_data`,
`*/locations`, `*/languages`) never cost anything -- see step 6 below.

## 4. Manual MCP Configuration

If the installer's auto-configuration fails, add this to `~/.codex/settings.json`:

```json
{
  "mcpServers": {
    "dataforseo": {
      "command": "npx",
      "args": ["-y", "dataforseo-mcp-server"],
      "env": {
        "DATAFORSEO_USERNAME": "your-email@example.com",
        "DATAFORSEO_PASSWORD": "your-api-password",
        "ENABLED_MODULES": "SERP,KEYWORDS_DATA,DATAFORSEO_LABS,ONPAGE",
        "FIELD_CONFIG_PATH": "/home/youruser/.codex/skills/seo/dataforseo-field-config.json"
      }
    }
  }
}
```

Replace the username, password, and FIELD_CONFIG_PATH with your actual values.

**`ENABLED_MODULES` defaults to four modules** (`SERP`, `KEYWORDS_DATA`,
`DATAFORSEO_LABS`, `ONPAGE`) -- the ones content work actually uses. The other
five (`BACKLINKS`, `DOMAIN_ANALYTICS`, `BUSINESS_DATA`, `CONTENT_ANALYSIS`,
`AI_OPTIMIZATION`) are the most expensive per call and every enabled module
also loads its tool definitions into context, so they are off by default. To
re-enable a module -- for example `BACKLINKS` when running an actual backlink
audit -- add it to the comma-separated list and restart Claude Code:

```json
"ENABLED_MODULES": "SERP,KEYWORDS_DATA,DATAFORSEO_LABS,ONPAGE,BACKLINKS"
```

## 5. Verify Installation

After installing, start Codex and run:

```
/seo dataforseo serp test query
```

If you see search results, the extension is working correctly.

## 6. Check Account and Market Readiness (Free)

`dfs_vn_probe.py` calls only free DataForSEO endpoints (`appendix/user_data`,
`*/locations`, `*/languages`) -- it costs $0.00 and can be run as often as you
like. Use it to confirm your credentials work and that the Vietnamese market
(`location_code=2704`, `language_code="vi"`) is available on your account
before running anything that spends credit:

```bash
export DATAFORSEO_USERNAME='...' DATAFORSEO_PASSWORD='...'
python scripts/dfs_vn_probe.py
```

It never prints a credential, even on error.
