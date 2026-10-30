"""Re-check specific jobs with the AI without re-checking (and paying tokens for) everything.

Usage:
  python recheck.py "Trainee"            # jobs whose title contains "Trainee"
  python recheck.py "Applied AI" "LLM"   # several patterns
  python recheck.py --all                # everything in the latest fetch

It clears the saved verdicts for matching jobs in the latest fetch and checks them again.
Bumping PROMPT_VERSION in llm_filter.py does the same for every job, which costs far more tokens.
"""

from __future__ import annotations

import sys

from fresherpilot.jobs import llm_filter
from fresherpilot.jobs.apify_jobs import load_cache
from fresherpilot.server import _normalized_jobs


def main(patterns: list[str]) -> int:
    if not patterns:
        print(__doc__)
        return 1
    jobs = _normalized_jobs(load_cache())
    if patterns != ["--all"]:
        lowered = [p.lower() for p in patterns]
        jobs = [j for j in jobs if any(p in j["title"].lower() for p in lowered)]
    llm_filter.forget(jobs)
    print(f"Re-checking {len(jobs)} job(s)…")
    errors = llm_filter.judge_jobs(jobs)
    for job in jobs:
        v = llm_filter.cached_verdict(job) or {}
        print(f"  {v.get('decision', 'ERROR'):6} {job['title'][:55]:55} | {v.get('category', '')} | {v.get('experience_evidence', '')[:60]}")
    if errors:
        print(f"{len(errors)} failed; they will be retried on the next run.")
    print("Reload the page to see the changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
