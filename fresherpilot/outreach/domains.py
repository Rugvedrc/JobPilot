"""Find a company's email domain and guess likely inboxes, using free sources only.

Domain, from the first source that works:
  1. an email address written in the job description (not gmail/yahoo...)
  2. the apply link, when it points at the company's own site (not a job board / ATS)
  3. Clearbit's free company autocomplete (name -> domain, no key)
  4. Wikidata's "official website" for the company
  5. a guess like <name>.com / .in / .co.in / .io / .ai, accepted only if the homepage mentions the company

Then the domain's MX records are checked (does it receive email at all?) and common
recruiting inboxes are suggested. Guesses are labelled as such; a mailbox can't be
confirmed without sending mail, so the UI says "not verified".
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (job-finder personal tool)"}
FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "rediffmail.com", "live.com", "icloud.com", "proton.me", "protonmail.com"}
NOT_COMPANY = (
    "naukri.com", "linkedin.com", "indeed.com", "glassdoor", "wellfound.com", "cutshort.io", "instahyre.com", "foundit.in",
    "monster", "shine.com", "timesjobs", "apna.co", "workindia", "internshala", "hirist", "iimjobs", "angel.co",
    # Applicant-tracking systems host many companies; their domain is not the employer's.
    "lever.co", "greenhouse.io", "myworkdayjobs.com", "workday.com", "smartrecruiters.com", "darwinbox", "zohorecruit",
    "keka.com", "freshteam.com", "recruitee.com", "ashbyhq.com", "breezy.hr", "bamboohr.com", "jobvite.com", "icims.com",
    "successfactors", "taleo.net", "google.com", "forms.gle", "bit.ly",
)
# Common recruiting inboxes, most likely first.
GENERIC_INBOXES = ["careers", "hr", "jobs", "recruitment", "talent", "hiring"]
NOISE = {
    "pvt", "private", "ltd", "limited", "llp", "inc", "llc", "corp", "corporation", "co", "company", "the", "india",
    "technologies", "technology", "tech", "solutions", "services", "software", "systems", "labs", "global", "group",
    "consulting", "consultancy", "international", "infotech", "it", "and", "of",
}


def _tokens(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in NOISE and len(w) > 1]


def _host(url: str) -> str:
    host = urlparse(url if "//" in url else "//" + url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _registrable(host: str) -> str:
    """careers.acme.co.in -> acme.co.in"""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# Jobs are in India: a company domain under another country's TLD (meril.co.jp) is a different company.
OK_TLDS = {"com", "in", "io", "ai", "net", "org", "co", "tech", "dev", "app", "cloud", "software", "global", "biz", "info", "us", "uk", "xyz"}


def _is_company_host(host: str) -> bool:
    return bool(host) and not any(bad in host for bad in NOT_COMPANY) and host.rsplit(".", 1)[-1] in OK_TLDS


def _name_matches(company: str, other: str) -> bool:
    a, b = set(_tokens(company)), set(_tokens(other))
    return bool(a) and (a <= b or b <= a)


def has_mx(domain: str) -> bool:
    try:
        import dns.resolver

        return bool(dns.resolver.resolve(domain, "MX", lifetime=6))
    except Exception:  # noqa: BLE001
        try:  # DNS-over-HTTPS fallback (some networks block plain DNS from Python)
            r = requests.get("https://cloudflare-dns.com/dns-query", params={"name": domain, "type": "MX"},
                             headers={"accept": "application/dns-json"}, timeout=8)
            return bool(r.json().get("Answer"))
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------- sources

def _from_description(job: dict[str, Any]) -> str:
    for email in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", job.get("description") or ""):
        dom = email.lower().rstrip(".")
        if dom not in FREE_MAIL and _is_company_host(dom):
            return _registrable(dom)
    return ""


def _from_apply_url(job: dict[str, Any]) -> str:
    host = _host(job.get("url") or "")
    return _registrable(host) if _is_company_host(host) else ""


def _from_clearbit(company: str) -> str:
    try:
        r = requests.get("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": company}, headers=UA, timeout=8)
        for item in r.json() or []:
            if item.get("domain") and _name_matches(company, item.get("name") or "") and _is_company_host(item["domain"].lower()):
                return item["domain"].lower()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _from_wikidata(company: str) -> str:
    try:
        api = "https://www.wikidata.org/w/api.php"
        found = requests.get(api, params={"action": "wbsearchentities", "search": company, "language": "en", "format": "json", "limit": 3},
                             headers=UA, timeout=10).json().get("search") or []
        for item in found:
            label = (item.get("display", {}).get("label", {}) or {}).get("value") or item.get("label") or ""
            if not _name_matches(company, label):
                continue
            ent = requests.get(api, params={"action": "wbgetentities", "ids": item["id"], "props": "claims", "format": "json"},
                               headers=UA, timeout=10).json()
            for claim in ent["entities"][item["id"]]["claims"].get("P856", []):  # P856 = official website
                url = claim["mainsnak"]["datavalue"]["value"]
                host = _host(url)
                if _is_company_host(host):
                    return _registrable(host)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _from_guess(company: str) -> str:
    tokens = _tokens(company)
    if not tokens:
        return ""
    slugs = list(dict.fromkeys(["".join(tokens), "".join(re.findall(r"[a-z0-9]+", company.lower()))]))
    for slug in slugs:
        for tld in ("com", "in", "co.in", "ai"):
            domain = f"{slug}.{tld}"
            try:
                r = requests.get(f"https://{domain}", headers=UA, timeout=4, allow_redirects=True)
            except Exception:  # noqa: BLE001
                continue
            page = r.text[:20000].lower()
            # Accept only if the homepage talks about this company.
            final = _host(r.url) or domain
            if r.ok and all(t in page for t in tokens) and _is_company_host(final):
                return _registrable(final)
    return ""


def find_domain(company: str, job: dict[str, Any] | None = None, known_website: str = "") -> dict[str, str]:
    """Best guess of the company's email domain, with where it came from."""
    job = job or {}
    candidates = [
        ("job description", lambda: _from_description(job)),
        ("web search", lambda: _registrable(_host(known_website)) if known_website else ""),
        ("apply link", lambda: _from_apply_url(job)),
        ("Clearbit", lambda: _from_clearbit(company)),
        ("Wikidata", lambda: _from_wikidata(company)),
        ("name guess", lambda: _from_guess(company)),
    ]
    for source, get in candidates:
        domain = get()
        if domain:
            return {"domain": domain, "source": source}
    return {"domain": "", "source": ""}


def guess_contacts(domain: str, people: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Likely inboxes at the domain (only if it receives email at all)."""
    if not domain:
        return {"mx": False, "emails": [], "people": []}
    if not has_mx(domain):
        return {"mx": False, "emails": [], "people": []}
    guesses = [f"{box}@{domain}" for box in GENERIC_INBOXES]
    person_guesses = []
    for p in (people or [])[:4]:
        if p.get("role") not in ("recruiter", "leader"):
            continue
        parts = [w for w in re.findall(r"[a-z]+", (p.get("name") or "").lower()) if len(w) > 1]
        if not parts:
            continue
        first, last = parts[0], parts[-1] if len(parts) > 1 else ""
        formats = [f"{first}.{last}@{domain}", f"{first}@{domain}"] if last else [f"{first}@{domain}"]
        person_guesses.append({"name": p.get("name"), "url": p.get("url"), "emails": formats})
    return {"mx": True, "emails": guesses, "people": person_guesses}
