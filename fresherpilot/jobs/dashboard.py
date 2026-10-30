"""Numbers for the dashboard: applications, outreach and new matches per day, plus the pipeline."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from fresherpilot.apply import applicant
from fresherpilot.jobs import llm_filter
from fresherpilot.outreach import messages as outreach
from fresherpilot.jobs import visited

DAYS = 14


def _day(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().date()  # your local day (IST)
    except ValueError:
        return None


def build(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    today = datetime.now().astimezone().date()
    days = [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    by_uid = {visited.uid(j): j for j in jobs}

    apps = applicant.applications()
    applied = Counter(_day(a.get("applied_at")) for a in apps.values())

    sent = [e for e in outreach.log() if e.get("ok")]
    emails = Counter(_day(e["at"]) for e in sent if e["channel"] == "email")
    whatsapp = Counter(_day(e["at"]) for e in sent if e["channel"] == "whatsapp")

    seen = visited.load_all()
    reviewed = Counter(_day(at) for key, at in seen.items() if not key.startswith("tc:"))

    keep = [j for j in jobs if (llm_filter.cached_verdict(j) or {}).get("decision") == "keep"]
    matches = Counter(_day(j.get("first_seen")) for j in keep)
    pending = sum(1 for j in jobs if llm_filter.cached_verdict(j) is None)

    series = [
        {"date": d.isoformat(), "applied": applied[d], "emails": emails[d], "whatsapp": whatsapp[d],
         "reviewed": reviewed[d], "matches": matches[d]}
        for d in days
    ]

    def total(counter: Counter, n: int) -> int:
        return sum(counter[today - timedelta(days=i)] for i in range(n))

    statuses = Counter(a.get("status") for a in apps.values())
    sources = Counter((by_uid.get(uid) or {}).get("source") or uid.split(":")[0] for uid in apps)
    recent = []
    for uid, a in sorted(apps.items(), key=lambda kv: kv[1].get("applied_at", ""), reverse=True)[:12]:
        job = by_uid.get(uid) or {}
        recent.append({
            "uid": uid, "title": job.get("title") or uid, "company": job.get("company") or "",
            "source": job.get("source") or uid.split(":")[0], "status": a.get("status"),
            "applied_at": a.get("applied_at"), "note": a.get("note", ""), "url": job.get("url", ""),
            "outreach": outreach.for_job(uid),
        })
    usage = llm_filter.usage_today()
    return {
        "today": today.isoformat(),
        "series": series,
        "kpi": {
            "applied_today": applied[today],
            "applied_yesterday": applied[today - timedelta(days=1)],
            "applied_week": total(applied, 7),
            "applied_total": len(apps),
            "emails_today": emails[today],
            "whatsapp_today": whatsapp[today],
            "outreach_week": total(emails, 7) + total(whatsapp, 7),
            "outreach_total": len(sent),
            "reviewed_today": reviewed[today],
            "new_matches": sum(1 for j in keep if not visited.visited_at(j, seen)),
            "pending_ai": pending,
            "interviews": statuses["interview"],
            "offers": statuses["offer"],
            "rejected": statuses["rejected"],
        },
        "pipeline": {s: statuses[s] for s in applicant.STATUSES},
        "sources": dict(sources.most_common()),
        "recent": recent,
        "ai_tokens_today": sum((usage.get("models") or {}).values()) if isinstance(usage, dict) else 0,
        "caps": {"email": outreach.DAILY_EMAIL_CAP, "whatsapp": outreach.DAILY_WHATSAPP_CAP},
    }
