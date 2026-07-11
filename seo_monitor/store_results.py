"""
store_results.py
----------------
Reads /tmp/analysis.json (produced by analyze_rank_drop.py) and commits it
to the repository under:

    data/YYYY-MM-DD/<keyword-slug>.json

This gives you a free, version-controlled, audit-trailed data store backed
by Git. Each daily run appends one file; the full history is queryable with
standard JSON tooling (jq, Python, etc.).

Scalability note
----------------
This approach is practical for:
  - ≤ ~50 keywords monitored daily
  - ≤ 2-3 years of history
  - Read patterns that are per-keyword or per-date (not cross-keyword aggregates)

When you outgrow it, the same JSON schema drops straight into BigQuery
(via bq load) or Postgres (via COPY) without any transformation.

GitHub Actions usage
--------------------
The workflow must set:
  - GIT_USER_EMAIL  (e.g. actions@github.com)
  - GIT_USER_NAME   (e.g. github-actions[bot])
and have write access to the repository (default GITHUB_TOKEN is sufficient
if "Read and write permissions" is enabled in repo Settings → Actions).
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ANALYSIS_PATH   = Path("/tmp/analysis.json")
DATA_DIR        = Path("data")      # relative to repo root; Actions cwd = repo root
GIT_USER_EMAIL  = os.environ.get("GIT_USER_EMAIL", "actions@github.com")
GIT_USER_NAME   = os.environ.get("GIT_USER_NAME",  "github-actions[bot]")


def slugify(text: str) -> str:
    """Convert a keyword to a safe filename, e.g. 'Best CRM Software' → 'best-crm-software'."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_]+", "-", text)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def git_configure():
    run(["git", "config", "user.email", GIT_USER_EMAIL])
    run(["git", "config", "user.name",  GIT_USER_NAME])


def store_analysis(data: dict) -> Path:
    """Write analysis to data/YYYY-MM-DD/<keyword-slug>.json and return the path."""
    date_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    keyword     = data.get("keyword", "unknown-keyword")
    filename    = f"{slugify(keyword)}.json"
    output_dir  = DATA_DIR / date_str
    output_path = output_dir / filename

    output_dir.mkdir(parents=True, exist_ok=True)

    # Enrich with storage metadata
    record = {
        "stored_at":  datetime.now(timezone.utc).isoformat(),
        "run_date":   date_str,
        **data,
    }

    with open(output_path, "w") as f:
        json.dump(record, f, indent=2)

    print(f"Stored analysis at {output_path}", file=sys.stderr)
    return output_path


def git_commit(output_path: Path, data: dict):
    """Stage and commit the new file. No-ops gracefully if nothing changed."""
    git_configure()

    # Pull latest to avoid conflicts on concurrent runs
    run(["git", "pull", "--rebase", "--autostash"], check=False)

    run(["git", "add", str(output_path)])

    status = run(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        print("Nothing to commit — file unchanged since last run.", file=sys.stderr)
        return

    keyword    = data.get("keyword", "unknown")
    issue_type = data.get("analysis", {}).get("issue_type", "no-drop")
    pos_from   = data.get("position_change", {}).get("yesterday", "?")
    pos_to     = data.get("position_change", {}).get("today", "?")

    message = (
        f"data: '{keyword}' — {issue_type} "
        f"(#{pos_from} → #{pos_to}) [{output_path.parent.name}]"
    )
    run(["git", "commit", "-m", message])

    push_result = run(["git", "push"], check=False)
    if push_result.returncode != 0:
        print(f"WARNING: git push failed:\n{push_result.stderr}", file=sys.stderr)
        # Non-fatal: the file is committed locally; a retry will push it.
    else:
        print("Committed and pushed.", file=sys.stderr)


def load_history(keyword: str, last_n_days: int = 30) -> list[dict]:
    """
    Utility: load the last N days of stored records for a given keyword.
    Call this from a separate reporting script or notebook.

    Example:
        records = load_history("best project management software", last_n_days=90)
        issue_counts = Counter(r["analysis"]["issue_type"] for r in records)
    """
    slug    = slugify(keyword)
    records = []

    if not DATA_DIR.exists():
        return records

    date_dirs = sorted(DATA_DIR.iterdir(), reverse=True)[:last_n_days]
    for date_dir in date_dirs:
        candidate = date_dir / f"{slug}.json"
        if candidate.exists():
            with open(candidate) as f:
                records.append(json.load(f))

    return records


def main():
    if not ANALYSIS_PATH.exists():
        print(
            f"ERROR: {ANALYSIS_PATH} not found. Run analyze_rank_drop.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(ANALYSIS_PATH) as f:
        data = json.load(f)

    # Always store — even non-triggered runs are useful for the baseline history.
    output_path = store_analysis(data)
    git_commit(output_path, data)

    print(json.dumps({"stored": True, "path": str(output_path)}))


if __name__ == "__main__":
    main()
