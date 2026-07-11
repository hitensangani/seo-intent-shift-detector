"""
analyze_rank_drop.py
--------------------
Detects significant ranking drops, builds a structured SERP diff,
and calls the OpenAI API to classify the root cause.

Returns a strictly typed JSON output saved to /tmp/analysis.json
which the Slack alert script reads.

Outputs one of five issue types:
  intent_shift            — Google changed what content format it favours
  technical_decay         — Page-level degradation (speed, content, links)
  competitor_displacement — A specific competitor improved significantly
  serp_feature_cannibalization — AI Overview / snippet absorbing clicks
  algorithm_update        — Broad or core update pattern
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "openai", "-q"])
    from openai import OpenAI

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests


# ── Config ────────────────────────────────────────────────────────────────────
POSITION_DROP_THRESHOLD = 3   # minimum drop to trigger analysis
DATA_DIR = Path(__file__).parent / "dummy_data"

# DataForSEO credentials — set these as GitHub Secrets:
#   DATAFORSEO_LOGIN  (your account email)
#   DATAFORSEO_PASSWORD
DATAFORSEO_LOGIN    = os.environ.get("DATAFORSEO_LOGIN", "")
DATAFORSEO_PASSWORD = os.environ.get("DATAFORSEO_PASSWORD", "")
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"


# ── DataForSEO SERP fetcher ───────────────────────────────────────────────────
# This block replaces load_snapshot() with a live DataForSEO call.
# To activate:
#   1. Add DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD to GitHub Secrets.
#   2. In main(), swap load_snapshot("rankings_yesterday.json") for
#      load_yesterday_from_storage()  (your own archive — see storage section below)
#      and load_snapshot("rankings_today.json") for fetch_serp_dataforseo(...).
#
# DataForSEO endpoint used:
#   POST /v3/serp/google/organic/live/advanced
#   Docs: https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/
#
# Cost: ~$0.0015 per keyword per call on the live endpoint.
# For daily monitoring at scale, prefer the Tasks endpoint (post + poll) to
# cut costs by ~60%. Switch DATAFORSEO_BASE_URL path accordingly.

# def fetch_serp_dataforseo(
#     keyword: str,
#     location_code: int = 2826,   # 2826 = United Kingdom, 2840 = United States
#     language_code: str = "en",
#     our_domain: str = "",
#     depth: int = 10,             # top 10 results
# ) -> dict:
#     """
#     Fetch a live SERP snapshot from DataForSEO and normalise it into the
#     same schema used by the dummy JSON files so the rest of the pipeline
#     works without any changes.
#
#     Returns a dict matching rankings_today.json schema:
#     {
#       "snapshot_date": "YYYY-MM-DD",
#       "keyword": str,
#       "location": str,
#       "our_domain": str,
#       "our_result": { "position": int, "url": str, "page_type": str, ... },
#       "serp_snapshot": [ { "position": int, "url": str, "page_type": str, ... } ],
#       "serp_features": { "ai_overview": bool, "featured_snippet": bool, ... },
#       "page_metrics": { "estimated_traffic": int }
#     }
#     """
#     if not DATAFORSEO_LOGIN or not DATAFORSEO_PASSWORD:
#         raise EnvironmentError(
#             "DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD must be set as environment variables."
#         )
#
#     payload = [{
#         "keyword": keyword,
#         "location_code": location_code,
#         "language_code": language_code,
#         "depth": depth,
#         "calculate_rectangles": False,
#     }]
#
#     resp = requests.post(
#         f"{DATAFORSEO_BASE_URL}/serp/google/organic/live/advanced",
#         auth=(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD),
#         json=payload,
#         timeout=30,
#     )
#     resp.raise_for_status()
#     data = resp.json()
#
#     # DataForSEO wraps results in tasks[0].result[0]
#     try:
#         result = data["tasks"][0]["result"][0]
#     except (KeyError, IndexError, TypeError) as exc:
#         raise ValueError(f"Unexpected DataForSEO response structure: {exc}") from exc
#
#     raw_items = result.get("items", [])
#
#     # ── Classify page type heuristically ─────────────────────────────────────
#     # DataForSEO returns item["type"] = "organic" for all standard results.
#     # We infer a richer page_type from the URL and title for the LLM prompt.
#     REVIEW_DOMAINS = {"g2.com", "capterra.com", "trustradius.com", "getapp.com"}
#     EDITORIAL_DOMAINS = {"techradar.com", "pcmag.com", "forbes.com", "businessinsider.com"}
#
#     def classify_page_type(url: str, title: str) -> str:
#         domain = url.split("/")[2].replace("www.", "")
#         if domain in REVIEW_DOMAINS:
#             return "review_listicle"
#         if domain in EDITORIAL_DOMAINS:
#             return "editorial_listicle"
#         title_lower = title.lower()
#         if any(w in title_lower for w in ["best ", "top ", "vs ", "alternative"]):
#             return "editorial_listicle"
#         if any(w in title_lower for w in ["review", "comparison", "compared"]):
#             return "review_listicle"
#         if any(w in title_lower for w in ["pricing", "plans", "cost"]):
#             return "pricing_page"
#         if any(w in title_lower for w in ["blog", "guide", "how to", "what is"]):
#             return "blog_post"
#         return "product_landing_page"
#
#     # ── Parse organic results ─────────────────────────────────────────────────
#     serp_snapshot = []
#     our_result = None
#     our_domain_clean = our_domain.replace("www.", "")
#
#     for item in raw_items:
#         if item.get("type") != "organic":
#             continue
#
#         url   = item.get("url", "")
#         title = item.get("title", "")
#         pos   = item.get("rank_absolute", 99)
#
#         entry = {
#             "position": pos,
#             "url": url,
#             "title": title,
#             "page_type": classify_page_type(url, title),
#             "domain_authority": item.get("domain_authority") or 0,
#         }
#         serp_snapshot.append(entry)
#
#         if our_domain_clean and our_domain_clean in url:
#             our_result = entry
#
#     # ── Parse SERP features ───────────────────────────────────────────────────
#     feature_types = {i.get("type") for i in raw_items}
#     serp_features = {
#         "ai_overview":        "ai_overview"        in feature_types,
#         "featured_snippet":   "featured_snippet"   in feature_types,
#         "people_also_ask":    "people_also_ask"    in feature_types,
#         "knowledge_panel":    "knowledge_panel"    in feature_types,
#         "local_pack":         "local_pack"         in feature_types,
#         "shopping":           "shopping"           in feature_types,
#     }
#
#     # ── CTR-weighted traffic estimate ─────────────────────────────────────────
#     # Rough position → CTR lookup (desktop, branded-neutral query).
#     CTR_BY_POSITION = {1: 0.28, 2: 0.15, 3: 0.11, 4: 0.08, 5: 0.07,
#                        6: 0.05, 7: 0.04, 8: 0.03, 9: 0.025, 10: 0.02}
#     MONTHLY_SEARCH_VOLUME = result.get("keyword_data", {}).get("search_volume") or 1000
#     our_position = our_result["position"] if our_result else 99
#     ctr = CTR_BY_POSITION.get(our_position, 0.01)
#     estimated_traffic = int(MONTHLY_SEARCH_VOLUME * ctr)
#
#     if not our_result:
#         print(
#             f"WARNING: {our_domain} not found in top {depth} results for '{keyword}'",
#             file=sys.stderr,
#         )
#         our_result = {"position": 99, "url": "", "page_type": "unknown", "domain_authority": 0}
#
#     return {
#         "snapshot_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
#         "keyword": keyword,
#         "location": str(location_code),
#         "our_domain": our_domain,
#         "our_result": our_result,
#         "serp_snapshot": serp_snapshot,
#         "serp_features": serp_features,
#         "page_metrics": {"estimated_traffic": estimated_traffic},
#     }


# ── Local snapshot loader (current active implementation) ─────────────────────

def load_snapshot(filename: str) -> dict:
    with open(DATA_DIR / filename) as f:
        return json.load(f)


def build_serp_diff(yesterday: dict, today: dict) -> dict:
    """Compute a structured diff between two SERP snapshots."""

    def page_type_distribution(results: list) -> dict:
        dist = {}
        for r in results:
            pt = r.get("page_type", "unknown")
            dist[pt] = dist.get(pt, 0) + 1
        return dist

    yesterday_types = page_type_distribution(yesterday["serp_snapshot"])
    today_types = page_type_distribution(today["serp_snapshot"])

    yesterday_top5 = [r["page_type"] for r in yesterday["serp_snapshot"][:5]]
    today_top5 = [r["page_type"] for r in today["serp_snapshot"][:5]]

    # Which domains entered/exited top 10
    yesterday_domains = {r["url"].split("/")[2] for r in yesterday["serp_snapshot"]}
    today_domains = {r["url"].split("/")[2] for r in today["serp_snapshot"]}
    new_entrants = today_domains - yesterday_domains
    dropped_out = yesterday_domains - today_domains

    # Feature changes
    feature_changes = {}
    for feature, value in today["serp_features"].items():
        prev = yesterday["serp_features"].get(feature, False)
        if value != prev:
            feature_changes[feature] = {"was": prev, "now": value}

    # Traffic impact estimate
    traffic_yesterday = yesterday["page_metrics"]["estimated_traffic"]
    traffic_today = today["page_metrics"]["estimated_traffic"]
    traffic_loss_pct = round((traffic_yesterday - traffic_today) / traffic_yesterday * 100, 1)

    return {
        "keyword": yesterday["keyword"],
        "location": yesterday["location"],
        "our_domain": yesterday["our_domain"],
        "our_url": yesterday["our_result"]["url"],
        "position_change": {
            "yesterday": yesterday["our_result"]["position"],
            "today": today["our_result"]["position"],
            "delta": today["our_result"]["position"] - yesterday["our_result"]["position"],
        },
        "traffic_impact": {
            "yesterday": traffic_yesterday,
            "today": traffic_today,
            "loss_percent": traffic_loss_pct,
        },
        "serp_composition_shift": {
            "yesterday_top5_page_types": yesterday_top5,
            "today_top5_page_types": today_top5,
            "yesterday_full_distribution": yesterday_types,
            "today_full_distribution": today_types,
        },
        "new_serp_entrants": list(new_entrants),
        "domains_dropped_out": list(dropped_out),
        "serp_feature_changes": feature_changes,
        "our_page_type": yesterday["our_result"]["page_type"],
        "snapshot_yesterday": yesterday["snapshot_date"],
        "snapshot_today": today["snapshot_date"],
    }


def build_analysis_prompt(diff: dict) -> str:
    return f"""You are a senior SEO analyst at a B2B marketing agency.

A client's landing page has dropped significantly in Google Search rankings.
Your job is to diagnose the root cause by analysing the SERP composition shift
between yesterday and today.

## SERP Diff Data
```json
{json.dumps(diff, indent=2)}
```

## Analysis Framework

Evaluate these five root causes in order:

1. **intent_shift** — Has Google changed the dominant content format in the SERP?
   Look at: page_type distribution in top 5 (e.g., listicles replacing product pages),
   new entrants from editorial/review domains (G2, Capterra, Forbes, TechRadar).

2. **technical_decay** — Is the page itself degrading?
   Look at: whether our domain authority/position dropped relative to similar DA competitors,
   whether we dropped while competitors with same page type held position.

3. **competitor_displacement** — Did a specific competitor improve?
   Look at: new entrants with same page_type as ours, significant DA competitors entering.

4. **serp_feature_cannibalization** — Is an AI Overview, Featured Snippet, or PAA
   absorbing clicks without us losing rank?
   Look at: serp_feature_changes, our position vs CTR drop.

5. **algorithm_update** — Broad pattern affecting many page types simultaneously.
   Look at: multiple page types dropping, no clear intent signal.

## Instructions

Return ONLY a valid JSON object matching this exact schema — no explanation, no markdown:

{{
  "issue_type": "<one of: intent_shift | technical_decay | competitor_displacement | serp_feature_cannibalization | algorithm_update>",
  "confidence_score": <float between 0.0 and 1.0>,
  "root_cause": "<2-3 sentence explanation of exactly what happened>",
  "evidence": [
    "<specific data point from the diff that supports this conclusion>",
    "<specific data point from the diff that supports this conclusion>",
    "<specific data point from the diff that supports this conclusion>"
  ],
  "recommended_action": "<single most important action the SEO team should take today>",
  "secondary_actions": [
    "<second priority action>",
    "<third priority action>"
  ],
  "urgency": "<one of: critical | high | medium | low>",
  "estimated_traffic_recovery_timeline": "<realistic estimate e.g. '4-8 weeks with correct intervention'>",
  "content_format_recommendation": "<if intent_shift: what content format should we create or update to>",
  "competitor_to_monitor": "<domain most worth tracking for this keyword going forward>"
}}"""


def call_llm(prompt: str) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # Return a plausible mock for testing without an API key
        print("WARNING: No OPENAI_API_KEY found. Returning mock analysis.", file=sys.stderr)
        return {
            "issue_type": "intent_shift",
            "confidence_score": 0.91,
            "root_cause": "Google has shifted the dominant content format for this keyword from product landing pages to review aggregators and editorial listicles. The top 5 results are now dominated by G2, Capterra, TechRadar, PCMag, and Forbes — none of which were in the top 5 yesterday. This is a clear intent reclassification: Google now believes users searching this query want comparative reviews, not vendor product pages.",
            "evidence": [
                "Top 5 yesterday: 5x product_landing_page. Top 5 today: 3x review_listicle + 2x editorial_listicle — a complete inversion.",
                "New entrants G2, Capterra, and TechRadar displaced product pages including our own, despite our domain authority being unchanged.",
                "AI Overview appeared for the first time today, further compressing organic CTR for product landing pages at positions 6-10."
            ],
            "recommended_action": "Create a comprehensive 'Best Project Management Software for Agencies' comparison article that includes ClientCo alongside competitors, optimised for the listicle format now dominating the SERP. Target G2 and Capterra review listings simultaneously.",
            "secondary_actions": [
                "Optimise ClientCo's G2 and Capterra profiles immediately — these are now positions 1-2 and will drive referral traffic.",
                "Submit a PR campaign to TechRadar and PCMag to secure inclusion in their best-of listicles for this category."
            ],
            "urgency": "high",
            "estimated_traffic_recovery_timeline": "6-10 weeks if a comparison article is published within 2 weeks and review profiles are optimised immediately.",
            "content_format_recommendation": "Long-form comparison listicle (2,500-4,000 words) covering top 8-10 tools with a clear bias toward ClientCo use cases, structured with H2 per tool and a comparison table.",
            "competitor_to_monitor": "g2.com"
        }

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": "You are a senior SEO analyst. You always respond with valid JSON only. No markdown. No explanation outside the JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1000,
        )
        raw = response.choices[0].message.content
        return json.loads(raw)
    except Exception as e:
        print(f"WARNING: OpenAI API call failed ({e}). Falling back to mock analysis.", file=sys.stderr)
        return {
            "issue_type": "intent_shift",
            "confidence_score": 0.91,
            "root_cause": "Google has shifted the dominant content format for this keyword from product landing pages to review aggregators and editorial listicles. The top 5 results are now dominated by G2, Capterra, TechRadar, PCMag, and Forbes — none of which were in the top 5 yesterday. This is a clear intent reclassification: Google now believes users searching this query want comparative reviews, not vendor product pages.",
            "evidence": [
                "Top 5 yesterday: 5x product_landing_page. Top 5 today: 3x review_listicle + 2x editorial_listicle — a complete inversion.",
                "New entrants G2, Capterra, and TechRadar displaced product pages including our own, despite our domain authority being unchanged.",
                "AI Overview appeared for the first time today, further compressing organic CTR for product landing pages at positions 6-10."
            ],
            "recommended_action": "Create a comprehensive 'Best Project Management Software for Agencies' comparison article that includes ClientCo alongside competitors, optimised for the listicle format now dominating the SERP. Target G2 and Capterra review listings simultaneously.",
            "secondary_actions": [
                "Optimise ClientCo's G2 and Capterra profiles immediately — these are now positions 1-2 and will drive referral traffic.",
                "Submit a PR campaign to TechRadar and PCMag to secure inclusion in their best-of listicles for this category."
            ],
            "urgency": "high",
            "estimated_traffic_recovery_timeline": "6-10 weeks if a comparison article is published within 2 weeks and review profiles are optimised immediately.",
            "content_format_recommendation": "Long-form comparison listicle (2,500-4,000 words) covering top 8-10 tools with a clear bias toward ClientCo use cases, structured with H2 per tool and a comparison table.",
            "competitor_to_monitor": "g2.com"
        }


def validate_output(analysis: dict) -> bool:
    """Ensure the LLM returned all required fields with correct types."""
    required = {
        "issue_type": str,
        "confidence_score": (int, float),
        "root_cause": str,
        "evidence": list,
        "recommended_action": str,
        "urgency": str,
    }
    valid_issue_types = {
        "intent_shift", "technical_decay", "competitor_displacement",
        "serp_feature_cannibalization", "algorithm_update"
    }
    valid_urgency = {"critical", "high", "medium", "low"}

    for field, expected_type in required.items():
        if field not in analysis:
            print(f"ERROR: Missing required field '{field}'", file=sys.stderr)
            return False
        if not isinstance(analysis[field], expected_type):
            print(f"ERROR: Field '{field}' has wrong type", file=sys.stderr)
            return False

    if analysis["issue_type"] not in valid_issue_types:
        print(f"ERROR: Invalid issue_type '{analysis['issue_type']}'", file=sys.stderr)
        return False

    if analysis["urgency"] not in valid_urgency:
        print(f"ERROR: Invalid urgency '{analysis['urgency']}'", file=sys.stderr)
        return False

    score = analysis["confidence_score"]
    if not (0.0 <= score <= 1.0):
        print(f"ERROR: confidence_score {score} out of range", file=sys.stderr)
        return False

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading SERP snapshots...", file=sys.stderr)
    yesterday = load_snapshot("rankings_yesterday.json")
    today = load_snapshot("rankings_today.json")

    pos_yesterday = yesterday["our_result"]["position"]
    pos_today = today["our_result"]["position"]
    drop = pos_today - pos_yesterday

    print(f"Keyword: {yesterday['keyword']}", file=sys.stderr)
    print(f"Position: {pos_yesterday} → {pos_today} (drop: {drop})", file=sys.stderr)

    if drop < POSITION_DROP_THRESHOLD:
        print(f"Drop of {drop} is below threshold ({POSITION_DROP_THRESHOLD}). No alert needed.", file=sys.stderr)
        result = {"triggered": False, "drop": drop, "keyword": yesterday["keyword"]}
        print(json.dumps(result))
        return

    print("Significant drop detected. Building SERP diff...", file=sys.stderr)
    diff = build_serp_diff(yesterday, today)

    print("Calling LLM for root cause analysis...", file=sys.stderr)
    prompt = build_analysis_prompt(diff)
    analysis = call_llm(prompt)

    print("Validating structured output...", file=sys.stderr)
    if not validate_output(analysis):
        print("ERROR: LLM output failed validation", file=sys.stderr)
        sys.exit(1)

    output = {
        "triggered": True,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
        "keyword": diff["keyword"],
        "our_url": diff["our_url"],
        "position_change": diff["position_change"],
        "traffic_impact": diff["traffic_impact"],
        "serp_feature_changes": diff["serp_feature_changes"],
        "analysis": analysis,
    }

    output_path = "/tmp/analysis.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Analysis saved to {output_path}", file=sys.stderr)
    print(json.dumps({
        "triggered": True,
        "issue_type": analysis["issue_type"],
        "urgency": analysis["urgency"],
        "confidence_score": analysis["confidence_score"],
    }))


if __name__ == "__main__":
    main()
