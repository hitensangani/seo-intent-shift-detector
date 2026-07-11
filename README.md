# SERP Intent-Shift Detector

An automation that detects significant keyword ranking drops, classifies the root cause using an LLM, and delivers a structured alert to Slack.

Built with Python and GitHub Actions. No infrastructure required beyond a GitHub repo, an OpenAI key, and a Slack bot.

---

## What it does

When a landing page drops 3+ positions for a tracked keyword, the system:

1. Computes a structured diff between two SERP snapshots (yesterday vs today)
2. Passes the diff to GPT-4o with a diagnostic prompt that evaluates five root cause hypotheses
3. Validates the LLM output against a strict JSON schema before anything downstream runs
4. Posts a rich Block Kit alert to a Slack channel with the diagnosis, evidence, and prioritised actions

---

## Root cause classification

The LLM classifies drops into one of five categories:

| Type | Description |
|------|-------------|
| `intent_shift` | Google changed the preferred content format (e.g. listicles replaced product pages) |
| `technical_decay` | Page-level degradation — speed, content freshness, or internal links |
| `competitor_displacement` | A specific competitor improved significantly |
| `serp_feature_cannibalization` | AI Overview or featured snippet absorbing clicks |
| `algorithm_update` | Broad core update pattern affecting multiple page types |

---

## Structured output schema

The LLM always returns this exact shape or the run fails:

```json
{
  "issue_type": "intent_shift",
  "confidence_score": 0.91,
  "root_cause": "string",
  "evidence": ["string", "string", "string"],
  "recommended_action": "string",
  "secondary_actions": ["string", "string"],
  "urgency": "critical | high | medium | low",
  "estimated_traffic_recovery_timeline": "string",
  "content_format_recommendation": "string",
  "competitor_to_monitor": "string"
}
```

---

## File structure

```
.github/
  workflows/
    rank-drop-monitor.yml     ← scheduled + manual trigger
seo_monitor/
  dummy_data/
    rankings_yesterday.json   ← SERP snapshot (position 2)
    rankings_today.json       ← SERP snapshot (position 9)
  analyze_rank_drop.py        ← SERP diff + GPT-4o analysis
  slack_alert.py              ← Block Kit Slack formatter
  requirements.txt
```

---

## Setup

### 1. Fork this repo

### 2. Add GitHub Secrets

| Secret | Description |
|--------|-------------|
| `OPENAI_API_KEY` | OpenAI API key |
| `SLACK_BOT_TOKEN` | Slack bot OAuth token (`xoxb-...`) |
| `SLACK_CHANNEL_ID` | Target Slack channel ID |

### 3. Slack bot scopes required

- `chat:write`
- `chat:write.public`

### 4. Run manually

Go to **Actions → SERP Intent-Shift Detector → Run workflow**

- `dry_run: true` — prints Slack blocks to the Actions log without posting
- `force_alert: true` — triggers alert even if drop is below threshold

---

## Reliability

**Hallucination control**
- Prompt constrains the LLM to classify from a fixed taxonomy only
- Temperature set to 0.2 for deterministic outputs
- `validate_output()` checks field presence, types, and enum values before any downstream step runs

**API failure handling**
- All OpenAI calls are wrapped in try/except
- On timeout, rate limit, or parse error: logs to stderr, falls back to a deterministic mock response, Slack alert still fires
- Pipeline failure triggers a separate lightweight Slack notification

**No silent failures**
- If the LLM output fails schema validation, the job exits with code 1
- GitHub Actions marks the run as failed and notifies via email

---

## Extending to a database

The `analyze_rank_drop.py` script outputs a structured dict. To write every run to a database instead of (or alongside) Slack, add a write step after analysis:

```python
# BigQuery
from google.cloud import bigquery
client = bigquery.Client()
client.insert_rows_json("project.dataset.rank_drops", [output])

# Postgres
import psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cursor = conn.cursor()
cursor.execute("INSERT INTO rank_drops (...) VALUES (...)", output)

# Airtable / Notion API
# POST the output dict to any REST endpoint
```

The GitHub Actions workflow passes `DATABASE_URL` or service account credentials as secrets the same way Slack credentials are handled.

---

## Tech stack

- **Python 3.12** — analysis and alerting scripts
- **GitHub Actions** — scheduling, orchestration, secrets management
- **OpenAI GPT-4o** — root cause classification with JSON mode
- **Slack Block Kit** — structured alert formatting
- **lxml** — HTML parsing (for live crawl extension)
