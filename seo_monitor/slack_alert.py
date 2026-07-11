"""
slack_alert.py
--------------
Reads /tmp/analysis.json produced by analyze_rank_drop.py
and posts a richly formatted, actionable alert to Slack using Block Kit.

The message design:
  - Header  : severity emoji + keyword + position drop
  - Context : timestamp, URL, traffic impact
  - Divider
  - AI Diagnosis card (issue type badge + confidence + root cause)
  - Evidence bullets
  - Recommended actions (numbered)
  - Footer  : SERP feature changes + powered-by note
"""

import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests


# ── Config ────────────────────────────────────────────────────────────────────

ANALYSIS_PATH = "/tmp/analysis.json"

ISSUE_TYPE_META = {
    "intent_shift": {
        "label": "Search Intent Shift",
        "emoji": ":arrows_counterclockwise:",
        "colour": "#F4A800",
        "description": "Google changed the preferred content format for this query",
    },
    "technical_decay": {
        "label": "Technical / Content Decay",
        "emoji": ":wrench:",
        "colour": "#E01E5A",
        "description": "Page-level degradation detected",
    },
    "competitor_displacement": {
        "label": "Competitor Displacement",
        "emoji": ":chart_with_upwards_trend:",
        "colour": "#FF6B35",
        "description": "A competitor improved significantly for this keyword",
    },
    "serp_feature_cannibalization": {
        "label": "SERP Feature Cannibalization",
        "emoji": ":robot_face:",
        "colour": "#6B5CFF",
        "description": "AI Overview or rich feature absorbing clicks",
    },
    "algorithm_update": {
        "label": "Algorithm Update",
        "emoji": ":google:",
        "colour": "#4A90D9",
        "description": "Broad ranking signal change detected",
    },
}

URGENCY_META = {
    "critical": {"emoji": ":rotating_light:", "label": "CRITICAL"},
    "high":     {"emoji": ":warning:",         "label": "HIGH"},
    "medium":   {"emoji": ":large_yellow_circle:", "label": "MEDIUM"},
    "low":      {"emoji": ":large_green_circle:",  "label": "LOW"},
}


# ── Block builders ────────────────────────────────────────────────────────────

def header_block(keyword: str, pos_from: int, pos_to: int, urgency: str) -> dict:
    u = URGENCY_META.get(urgency, URGENCY_META["high"])
    return {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"{u['emoji']} [{u['label']}] Ranking Drop Detected — {keyword}",
            "emoji": True,
        },
    }


def position_section(data: dict) -> list:
    pos = data["position_change"]
    traffic = data["traffic_impact"]
    delta = pos["delta"]
    traffic_loss = traffic["loss_percent"]

    return [
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Keyword*\n`{data['keyword']}`",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Position Change*\n~#{pos['yesterday']}~ → *#{pos['today']}*  (`-{delta} positions`)",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Estimated Traffic*\n{traffic['yesterday']:,} → {traffic['today']:,} visits/mo",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Traffic Loss*\n`-{traffic_loss}%` vs yesterday",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":link: *Affected URL*\n{data['our_url']}",
            },
        },
        {"type": "divider"},
    ]


def diagnosis_section(analysis: dict, analysed_at: str = "just now") -> list:
    issue = analysis["issue_type"]
    meta = ISSUE_TYPE_META.get(issue, ISSUE_TYPE_META["intent_shift"])
    confidence_pct = int(analysis["confidence_score"] * 100)
    confidence_bar = "█" * (confidence_pct // 10) + "░" * (10 - confidence_pct // 10)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{meta['emoji']}  *Root Cause: {meta['label']}*\n"
                    f"_{meta['description']}_\n\n"
                    f"{analysis['root_cause']}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*AI Confidence:* `{confidence_bar}` {confidence_pct}%   •   Analysed at {analysed_at}",
                }
            ],
        },
        {"type": "divider"},
    ]
    return blocks


def evidence_section(analysis: dict) -> list:
    evidence = analysis.get("evidence", [])
    if not evidence:
        return []

    bullets = "\n".join(f"• {e}" for e in evidence)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":mag: *Supporting Evidence*\n{bullets}",
            },
        },
        {"type": "divider"},
    ]


def actions_section(analysis: dict) -> list:
    primary = analysis.get("recommended_action", "")
    secondary = analysis.get("secondary_actions", [])
    timeline = analysis.get("estimated_traffic_recovery_timeline", "")
    content_rec = analysis.get("content_format_recommendation", "")

    lines = [f":one:  *{primary}*"]
    for i, action in enumerate(secondary[:2], 2):
        lines.append(f":{['one','two','three'][i-1]}:  {action}")

    text = ":dart: *Recommended Actions*\n" + "\n\n".join(lines)

    if timeline:
        text += f"\n\n:hourglass_flowing_sand: *Recovery Timeline:* {timeline}"

    if content_rec:
        text += f"\n\n:pencil: *Content Format:* {content_rec}"

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }
    ]


def footer_section(data: dict, analysis: dict) -> list:
    feature_changes = data.get("serp_feature_changes", {})
    competitor = analysis.get("competitor_to_monitor", "")

    context_items = []

    if feature_changes:
        changes = []
        for feature, change in feature_changes.items():
            direction = "appeared" if change["now"] else "disappeared"
            changes.append(f"{feature.replace('_', ' ').title()} {direction}")
        context_items.append(f":bar_chart: *SERP Changes:* {' · '.join(changes)}")

    if competitor:
        context_items.append(f":eyes: *Monitor:* {competitor}")

    context_items.append(":robot_face: Analysis powered by SEO Intent-Shift Monitor + GPT-4o")

    return [
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": item}
                for item in context_items
            ],
        },
    ]


def build_blocks(data: dict) -> list:
    analysis = data["analysis"]
    blocks = []
    blocks.append(header_block(
        data["keyword"],
        data["position_change"]["yesterday"],
        data["position_change"]["today"],
        analysis["urgency"],
    ))
    blocks.extend(position_section(data))
    blocks.extend(diagnosis_section(analysis, data.get("analysed_at", "just now")))
    blocks.extend(evidence_section(analysis))
    blocks.extend(actions_section(analysis))
    blocks.extend(footer_section(data, analysis))
    return blocks


# ── Slack post ─────────────────────────────────────────────────────────────────

def post_to_slack(blocks: list, fallback_text: str):
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL_ID")

    if not token or not channel:
        print("No Slack credentials — printing blocks to stdout instead.", file=sys.stderr)
        print(json.dumps({"blocks": blocks}, indent=2))
        return

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "channel": channel,
            "text": fallback_text,
            "blocks": blocks,
            "unfurl_links": False,
        },
        timeout=15,
    )
    result = resp.json()
    if result.get("ok"):
        print(f"Slack message sent to {channel}", file=sys.stderr)
    else:
        print(f"Slack error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    path = Path(ANALYSIS_PATH)
    if not path.exists():
        print(f"ERROR: {ANALYSIS_PATH} not found. Run analyze_rank_drop.py first.", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    if not data.get("triggered"):
        print("No significant drop detected. No alert sent.", file=sys.stderr)
        return

    analysis = data["analysis"]
    issue_meta = ISSUE_TYPE_META.get(analysis["issue_type"], {})
    urgency = URGENCY_META.get(analysis["urgency"], URGENCY_META["high"])

    fallback = (
        f"{urgency['emoji']} [{urgency['label']}] "
        f"'{data['keyword']}' dropped from #{data['position_change']['yesterday']} "
        f"to #{data['position_change']['today']}. "
        f"Diagnosis: {issue_meta.get('label', analysis['issue_type'])}. "
        f"Confidence: {int(analysis['confidence_score'] * 100)}%."
    )

    print("Building Slack blocks...", file=sys.stderr)
    blocks = build_blocks(data)

    print("Sending alert...", file=sys.stderr)
    post_to_slack(blocks, fallback)
    print(json.dumps({"sent": True, "urgency": analysis["urgency"], "issue_type": analysis["issue_type"]}))


if __name__ == "__main__":
    main()
