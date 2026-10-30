"""New-user setup: read the resume (or an "about me" text) and draft the profile + search settings.

One AI call (the user's own Groq key) turns the resume into: contact details, education, skills,
years of experience, the job titles to search for, and the stacks they don't work with. The user
reviews and edits the draft before it is saved.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from fresherpilot.apply import applicant
from fresherpilot.jobs import llm_filter
from fresherpilot.core import users

_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        **{k: _STR for k in ("first_name", "last_name", "full_name", "email", "phone", "city", "state", "country",
                             "linkedin", "github", "portfolio", "relevant_experience", "headline", "summary", "target_area")},
        "education": {"type": "array", "items": {"type": "object", "properties": {
            k: _STR for k in ("degree", "field", "institution", "end_year", "cgpa", "percentage")},
            "required": ["degree", "field", "institution", "end_year", "cgpa", "percentage"], "additionalProperties": False}},
        "experience_years": {"type": "number"},
        "skills": {"type": "array", "items": _STR},
        "roles": {"type": "array", "items": _STR},
        "off_stack": {"type": "array", "items": _STR},
        "on_stack_words": {"type": "array", "items": _STR},
        "is_software": {"type": "boolean"},
    },
    "additionalProperties": False,
}
SCHEMA["required"] = list(SCHEMA["properties"])

PROMPT = """Read this resume / self-description and fill the fields. Use ONLY what is written; "" when unknown.
experience_years: years of FULL-TIME work (internships, projects and freelancing don't count; a student = 0).
relevant_experience: one line, e.g. "6 months of ML internships".
headline: max 8 words, e.g. "2026 B.Tech CSE graduate" or "Backend developer, 2 years".
summary: one sentence (max 35 words): their main area and main technologies.
target_area: the kinds of jobs that fit them, max 12 words (e.g. "Java backend, Spring Boot, microservices").
skills: up to 40 technical skills / tools exactly as written.
roles: 6-10 job titles to search job boards for, best first, matching their level (no "Senior" for freshers).
off_stack: 8-20 technologies that appear in job TITLES and that they do NOT work with (e.g. for a Python/ML person:
 java, .net, php, android, ios, flutter, angular, salesforce, sap). Never list anything they use.
on_stack_words: 5-12 lowercase words that, in a job title, mean the job uses their stack (e.g. python, ml, ai, data).
is_software: true if they are looking for software / data / IT jobs."""


def _pdf_text(data: bytes) -> str:
    import fitz  # PyMuPDF

    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc).replace("⁠", "").replace("\xa0", " ")


def analyse(data: bytes, filename: str, about: str) -> dict[str, Any]:
    profile = applicant.load_profile()
    if data:
        if not data.startswith(b"%PDF"):
            raise ValueError("Please upload the resume as a PDF.")
        text = _pdf_text(data)
        if not text.strip():
            raise ValueError("That PDF has no text (is it a scanned image?). Paste an 'about me' text instead.")
        users.path("resume.pdf").write_bytes(data)
        profile["resume_file"] = "resume.pdf"
    elif about.strip():
        text = about.strip()
        users.path("resume.txt").write_text(text, encoding="utf-8")
        if not users.path("resume.pdf").exists():
            profile["resume_file"] = "resume.txt"
    else:
        raise ValueError("Upload your resume (PDF) or write a few lines about yourself.")
    applicant.save_profile(profile)
    if about.strip() and data:
        text += "\n\nAbout me:\n" + about.strip()

    content, _ = llm_filter._create(llm_filter._client(), [
        {"role": "system", "content": PROMPT}, {"role": "user", "content": text[:12000]}], schema=SCHEMA, name="onboarding")
    r = json.loads(content)
    return {"profile": _profile_from(r, profile), "prefs": _prefs_from(r)}


def _profile_from(r: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    p = json.loads(json.dumps(current))
    for k in ("first_name", "last_name", "full_name", "email", "phone", "city", "state", "country"):
        if r.get(k) and not p["personal"].get(k):
            p["personal"][k] = r[k]
    phone = re.sub(r"[^\d+]", "", p["personal"].get("phone") or "")
    if phone.startswith("+91"):
        p["personal"]["phone"], p["personal"]["phone_country_code"] = phone[3:], "+91"
    for k in ("linkedin", "github", "portfolio"):
        if r.get(k) and not p["links"].get(k):
            p["links"][k] = r[k] if r[k].startswith("http") else "https://" + r[k]
    if r.get("education") and not p.get("education"):
        p["education"] = [{**e, "cgpa_scale": "10" if e.get("cgpa") else ""} for e in r["education"]]
    jp = p["job_preferences"]
    jp["total_experience_years"] = jp.get("total_experience_years") or str(r.get("experience_years") or 0)
    jp["relevant_experience"] = jp.get("relevant_experience") or r.get("relevant_experience", "")
    return p


def _title_word(w: str) -> str:
    w = w.strip().lower()
    return w + " " if re.fullmatch(r"[a-z]+", w) else w  # "java " must not hit "javascript"


def _prefs_from(r: dict[str, Any]) -> dict[str, Any]:
    years = float(r.get("experience_years") or 0)
    roles = [x.strip() for x in r.get("roles") or [] if x.strip()][:10]
    skills = [x.strip() for x in r.get("skills") or [] if x.strip()][:40]
    on = [x.strip().lower() for x in r.get("on_stack_words") or [] if x.strip()][:12]
    return {
        "headline": r.get("headline", ""),
        "summary": r.get("summary", ""),
        "target_area": r.get("target_area", ""),
        "experience_years": years,
        "reject_from_years": max(1, math.ceil(years) + 1) if years else 1,
        "roles": [x.lower() for x in roles],
        "linkedin_titles": roles[:6],
        "off_stack": sorted({_title_word(w) for w in r.get("off_stack") or [] if w.strip() and w.strip().lower() not in on}),
        "on_stack_words": on,
        "wellfound_keywords": list(dict.fromkeys([s.lower() for s in skills[:10]] + [x.lower() for x in roles[:5]])),
        "internshala_queries": [x.lower() for x in roles[:6]],
        "naukri_departments": ["5", "3"] if r.get("is_software", True) else [],
        "skills": skills,
    }


def save(profile: dict[str, Any], prefs: dict[str, Any]) -> dict[str, Any]:
    saved_profile = applicant.save_profile({**applicant.load_profile(), **profile}) if profile else applicant.load_profile()
    for k in ("roles", "linkedin_titles", "skills", "off_stack", "on_stack_words", "wellfound_keywords", "internshala_queries"):
        if isinstance(prefs.get(k), str):
            prefs[k] = [x.strip() for x in prefs[k].split(",") if x.strip()]
    saved_prefs = users.save_prefs({**prefs, "onboarded": True})
    return {"profile": saved_profile, "prefs": saved_prefs}
