"""Everything needed to apply: the applicant profile, form autofill answers, the per-job
"Apply kit" (cover letter, common answers, outreach messages) and application tracking.

Autofill works in two steps for every form field the browser extension sends:
  1. Standard fields (name, email, phone, links, education, CTC, notice period...) are
     filled straight from the profile, no AI needed.
  2. Everything else (free-text questions, dropdowns, radio groups, checkboxes) goes to the
     LLM with the profile, the resume and the job description. Answers are then validated:
     choices must be one of the offered options, text is cut to the field's char/word limit,
     and anything the profile can't truthfully answer is left blank for the user.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
def _upath(name: str) -> Path:
    from fresherpilot.core import users

    return users.path(name)

_lock = threading.Lock()

# ---------------------------------------------------------------- profile

# Blank template: every user's own values live in users/<id>/applicant_profile.json (filled from their
# resume during onboarding and in the Profile editor). No personal data is kept in the code.
DEFAULT_PROFILE: dict[str, Any] = {
    "personal": {
        "first_name": "", "middle_name": "", "last_name": "", "full_name": "", "email": "", "phone": "",
        "phone_country_code": "+91", "city": "", "state": "", "country": "India", "pincode": "", "address": "",
        "date_of_birth": "", "gender": "", "nationality": "",
    },
    "links": {"linkedin": "", "github": "", "portfolio": "", "leetcode": ""},
    "education": [],
    "job_preferences": {
        "total_experience_years": "", "relevant_experience": "", "current_company": "", "current_ctc_lpa": "",
        "expected_ctc_lpa": "", "notice_period": "", "willing_to_relocate": "", "preferred_locations": "",
        "work_mode": "", "authorized_to_work_in_india": "", "requires_visa_sponsorship": "",
        "graduation_year": "", "available_from": "",
    },
    "diversity": {"gender": "", "disability": "", "veteran": "", "hispanic_latino": "", "race_ethnicity": "", "caste_category": ""},
    "custom_answers": {},
    "resume_file": "resume.pdf",
}

# Fields the user should fill in themselves (shown in the profile editor).
NEEDS_USER = ["personal.pincode", "personal.date_of_birth", "personal.gender", "job_preferences.expected_ctc_lpa"]


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_profile() -> dict[str, Any]:
    saved: dict[str, Any] = {}
    if _upath("applicant_profile.json").exists():
        try:
            saved = json.loads(_upath("applicant_profile.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = {}
    profile = _merge(DEFAULT_PROFILE, saved)
    for key in ("disability", "veteran", "hispanic_latino", "race_ethnicity"):
        if not str(profile["diversity"].get(key) or "").strip():
            profile["diversity"][key] = DEFAULT_PROFILE["diversity"][key]
    if not _upath("applicant_profile.json").exists():
        save_profile(profile)
    return profile


def save_profile(profile: dict[str, Any]) -> dict[str, Any]:
    merged = _merge(DEFAULT_PROFILE, profile)
    with _lock:
        _upath("applicant_profile.json").write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


def missing_fields(profile: dict[str, Any]) -> list[str]:
    out = []
    for path in NEEDS_USER:
        section, key = path.split(".")
        if not str(profile.get(section, {}).get(key) or "").strip():
            out.append(path)
    return out


def resume_path(profile: dict[str, Any] | None = None) -> Path:
    from fresherpilot.core import users

    profile = profile or load_profile()
    name = profile.get("resume_file") or "resume.pdf"
    mine = users.path(name)
    if mine.exists() or users.current() != users.owner_id():
        return mine
    return ROOT / name  # the owner's resume from before accounts existed


def _resume_text() -> str:
    from fresherpilot.jobs.candidate import resume_text

    return resume_text()


# ---------------------------------------------------------------- field helpers

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#.%< -]+", " ", (text or "").lower())).strip()


def match_option(value: str, options: list[str]) -> str:
    """The option that best matches value, or "" when nothing matches well enough."""
    if not value or not options:
        return ""
    v = _norm(value)
    norm = {o: _norm(o) for o in options if str(o).strip()}
    for o, n in norm.items():
        if n == v:
            return o
    # yes/no style answers
    if v in {"yes", "true", "y"}:
        for o, n in norm.items():
            if n.startswith("yes") or n in {"y", "true"}:
                return o
    if v in {"no", "false", "n"}:
        for o, n in norm.items():
            if n.startswith("no") or n in {"n", "false"}:
                return o
    # Numbers against range options ("0" -> "0-1 years" / "Fresher" / "Less than 1 year").
    num = re.fullmatch(r"\d+(?:\.\d+)?", v)
    if num:
        x = float(v)
        for o, n in norm.items():
            r = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", n)
            if r and float(r.group(1)) <= x <= float(r.group(2)):
                return o
            if re.search(r"(less than|under|below|<)\s*(\d+)", n) and x < float(re.search(r"(\d+)", n).group(1)):
                return o
            m_plus = re.search(r"(\d+(?:\.\d+)?)\s*\+|(\d+(?:\.\d+)?)\s*(?:or more|and above)", n)
            if m_plus and x >= float(m_plus.group(1) or m_plus.group(2)):
                return o
            if x == 0 and re.search(r"\bfresher\b|\bno experience\b|^0$|\bnone\b", n):
                return o
    for o, n in norm.items():
        if v and (v in n or n in v) and min(len(v), len(n)) >= 2:
            return o
    best = difflib.get_close_matches(v, list(norm.values()), n=1, cutoff=0.72)
    if best:
        return next(o for o, n in norm.items() if n == best[0])
    return ""


def _limits(field: dict[str, Any]) -> tuple[int | None, int | None]:
    """(max chars, max words) from maxlength and from hints like "(max 500 characters)"."""
    max_chars = field.get("maxlength") if isinstance(field.get("maxlength"), int) and field.get("maxlength") > 0 else None
    text = f"{field.get('label', '')} {field.get('hint', '')}"
    m = re.search(r"(\d{2,5})\s*(?:characters|chars|character)", text, re.I)
    if m:
        max_chars = min(max_chars or 10**9, int(m.group(1)))
    w = re.search(r"(\d{1,4})\s*words", text, re.I)
    return max_chars, int(w.group(1)) if w else None


def _fit_length(text: str, max_chars: int | None, max_words: int | None) -> str:
    text = text.strip()
    if max_words:
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
    if max_chars and len(text) > max_chars:
        cut = text[:max_chars]
        # End at the last full sentence if that keeps most of the text.
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > max_chars * 0.6 else cut.rsplit(" ", 1)[0]
    return text


# ---------------------------------------------------------------- deterministic answers

def _profile_rules(profile: dict[str, Any]) -> list[tuple[re.Pattern[str], str]]:
    p, links, prefs = profile["personal"], profile["links"], profile["job_preferences"]
    edu = profile["education"][0] if profile.get("education") else {}
    phone_full = f"{p['phone_country_code']} {p['phone']}".strip()
    rules = [
        (r"^(first|given) ?name|^name \(first\)|first name", p["first_name"]),
        (r"middle name", p.get("middle_name", "")),
        (r"(last|family|sur) ?name", p["last_name"]),
        (r"^(full )?name$|^your (full )?name|^full name|^candidate name|^applicant name|^legal name", p["full_name"]),
        (r"e-?mail", p["email"]),
        (r"country code|dial code", p["phone_country_code"]),
        (r"phone|mobile|contact number|whatsapp|cell", phone_full),
        (r"linked ?in", links["linkedin"]),
        (r"git ?hub", links["github"]),
        (r"portfolio|personal (web)?site|^website|blog|other (url|link)", links["portfolio"]),
        (r"leet ?code", links.get("leetcode", "")),
        (r"^(current )?city|current location|^location$|city of residence|where are you (currently )?(based|located)", p["city"]),
        (r"^state|province", p["state"]),
        (r"^country(?! code)", p["country"]),
        (r"pin ?code|postal|zip", p.get("pincode", "")),
        (r"date of birth|\bdob\b|birth date", p.get("date_of_birth", "")),
        (r"nationality|citizenship", p.get("nationality", "")),
        (r"notice period|joining time|how soon can you join|earliest (start|joining)", prefs["notice_period"]),
        (r"current (ctc|salary|compensation|package)", "CTC:" + str(prefs["current_ctc_lpa"])),
        (r"expected (ctc|salary|compensation|package)|salary expectation", "CTC:" + str(prefs.get("expected_ctc_lpa", "") or "")),
        (r"total (years of )?(work )?experience|years of (professional )?experience|total exp", prefs["total_experience_years"]),
        (r"current (company|employer|organisation|organization)", prefs.get("current_company", "")),
        (r"(cgpa|gpa|cpi)(?!.*(12|xii|10th|x\b))", edu.get("cgpa", "")),
        (r"(year of (passing|graduation)|graduation year|passing year|batch)", prefs["graduation_year"]),
        (r"(college|university|institute|school) name|^(college|university|institution)$", edu.get("institution", "")),
        (r"^(highest )?(degree|qualification|education)$", f"{edu.get('degree', '')} {edu.get('field', '')}".strip()),
        (r"^(field of study|branch|specialization|specialisation|major|stream|discipline)", edu.get("field", "")),
        (r"willing to relocate|open to relocat", prefs["willing_to_relocate"]),
    ]
    return [(re.compile(pat, re.I), val) for pat, val in rules]


def _ctc_for(field: dict[str, Any], raw: str) -> str:
    """Profile CTC (typed as 4, 4.5 LPA or 400000) in the unit the field asks for."""
    m = re.search(r"\d+(?:\.\d+)?", raw.replace(",", ""))
    if not m:
        return ""
    amount = float(m.group(0))
    lpa = amount / 100000 if amount >= 1000 else amount
    hint = f"{field.get('label', '')} {field.get('placeholder', '')} {field.get('hint', '')}".lower()
    if re.search(r"lpa|lakh|lac|l", hint) or (not re.search(r"inr|rupee|₹|per annum|annual|pa|monthly|per month", hint) and lpa < 100):
        value = lpa
    elif re.search(r"monthly|per month", hint):
        value = lpa * 100000 / 12
    else:
        value = lpa * 100000
    return f"{value:g}" if value < 1000 else str(int(round(value)))


def _direct_answer(field: dict[str, Any], rules: list[tuple[re.Pattern[str], str]]) -> str | None:
    """Answer from the profile if the field is a standard one. None = not a standard field."""
    label = _norm(field.get("label") or "")
    if not label or len(label) > 80:  # long labels are real questions, let the AI handle them
        return None
    if field.get("type") in ("checkbox", "file"):  # e.g. "Email me updates" is a choice, not the email address
        return None
    for rx, value in rules:
        if rx.search(label):
            if value.startswith("CTC:"):
                value = _ctc_for(field, value[4:])
            if field.get("type") in ("select", "radio", "combobox", "checkbox_group"):
                if not field.get("options"):
                    return value or ""
                chosen = match_option(value, field.get("options") or [])
                return chosen if chosen else None  # no confident match -> ask the AI
            return value or ""
    return None


# ---------------------------------------------------------------- AI answers

AUTOFILL_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "value": {"type": "string"},
                    "needs_review": {"type": "boolean"},
                },
                "required": ["id", "value", "needs_review"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}

AUTOFILL_RULES = """You fill in a job application form on behalf of the candidate. The candidate reviews and submits it.

Use ONLY facts from the PROFILE and RESUME. Never invent experience, employers, numbers, skills or credentials,
and never add specifics the resume doesn't state (model versions, team sizes, metrics, tools).
Name tools exactly as the resume does (if it says "GPT", write "GPT", not "GPT-4").

For each field return {id, value, needs_review}:
- Free text (text/textarea): answer in first person, specific and genuine, using the candidate's real projects and
  experience that relate to THIS job. Short single-line fields get short answers (a few words or a number).
  Stay safely under max_chars / max_words when given (aim for ~80% of the limit so the answer is never cut off).
- select / radio / combobox with options: value must be copied EXACTLY from the options list. Pick the truthful,
  best answer (e.g. experience "0-1 years", "Bachelor's", "Yes" for authorised to work in India).
- checkbox_group: the chosen option texts, copied exactly, separated by " || ".
- checkbox (single): "true" or "false". Answer truthfully for factual statements. For legal consent, terms,
  privacy, background-check or data-sharing agreements, return "" and needs_review=true: the candidate decides.
  Marketing / newsletter opt-ins: "false".
- number: digits only (e.g. years of experience "0", CTC in LPA if the field asks LPA, else in rupees per year).
- date: YYYY-MM-DD.
- OBVIOUS FACTS (use them even if the profile doesn't spell them out): the candidate lives in India, is an Indian
  citizen, phone country code +91 (India), currency INR, timezone IST (UTC+5:30), English proficiency fluent /
  professional. Authorised to work in India without sponsorship; for any other country NOT authorised and WOULD
  need visa sponsorship. Not related to any employee unless stated. Willing to relocate within India.
- Demographics: use the profile's diversity values (disability "No", veteran "not a protected veteran",
  Hispanic/Latino "No", race "Asian"). Gender, caste, religion: profile value if present, otherwise the
  "prefer not to say"/"decline" option if one exists, otherwise "".
- If the true answer is not in the profile/resume (e.g. expected salary not set, unknown personal details),
  return "" with needs_review=true. Do NOT guess.
- needs_review=true also for anything you are unsure about.
"""


def compact_profile(profile: dict[str, Any]) -> str:
    """Profile as short "key: value" lines (about half the tokens of JSON), empty values left out."""
    lines = []
    for section, values in profile.items():
        if section in ("resume_file", "custom_answers"):
            continue
        if isinstance(values, dict):
            kv = [f"{k}: {v}" for k, v in values.items() if str(v).strip()]
            if kv:
                lines.append(f"{section}: " + "; ".join(kv))
        elif isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    lines.append(f"{section}: " + ", ".join(f"{k} {v}" for k, v in item.items() if str(v).strip()))
    skills = ""
    try:
        from fresherpilot.jobs.candidate import load_profile as _cand

        skills = ", ".join(_cand().skills)
    except Exception:  # noqa: BLE001
        pass
    if skills:
        lines.append(f"skills: {skills}")
    return "\n".join(lines)


def _resume_body() -> str:
    """Resume without the contact header and school marks (already in the profile)."""
    text = _resume_text()
    body = text.split("\nPROFILE", 1)[-1].split("\nEDUCATION", 1)[0]
    return re.sub(r"[ \t]+", " ", body).strip()[:4500]


def _is_essay(field: dict[str, Any]) -> bool:
    max_chars, max_words = _limits(field)
    label = (field.get("label") or "").lower()
    return (field.get("type") == "textarea" or (max_chars or 0) >= 150 or bool(max_words)
            or bool(re.search(r"\bwhy\b|describe|tell us|explain|about yourself|cover letter|motivat|interest", label)))


def _llm_answers(fields: list[dict[str, Any]], profile: dict[str, Any], job_context: str) -> dict[str, dict[str, Any]]:
    from fresherpilot.jobs import llm_filter

    if not fields:
        return {}
    profile_text = compact_profile(profile)
    out: dict[str, dict[str, Any]] = {}
    client = llm_filter._client()
    for start in range(0, len(fields), 20):
        batch = fields[start : start + 20]
        # One line per field (cheaper than JSON): id | type | question | limits | options.
        lines = []
        for f in batch:
            max_chars, max_words = _limits(f)
            bits = [f["id"], f.get("type") or "text", (f.get("label") or "")[:300]]
            extra = " ".join(str(f.get(k))[:150] for k in ("hint", "placeholder") if f.get(k))
            if extra:
                bits.append(f"hint: {extra}")
            if max_chars:
                bits.append(f"max {max_chars} chars")
            if max_words:
                bits.append(f"max {max_words} words")
            if f.get("required"):
                bits.append("required")
            if f.get("options"):
                bits.append("options: " + " / ".join(str(o)[:100] for o in f["options"][:60]))
            lines.append(" | ".join(bits))
        # The resume and job description only matter for written answers; short factual
        # fields are answered from the profile alone.
        essay = any(_is_essay(f) for f in batch)
        user = f"PROFILE:\n{profile_text}\n\n"
        if essay:
            user += f"RESUME:\n{_resume_body()}\n\nJOB:\n{job_context[:2500] or '(unknown)'}\n\n"
        user += "FIELDS (id | type | question | ...):\n" + "\n".join(lines) + "\n\nOne answer per field id."
        content, _model = llm_filter._create(
            client, [{"role": "system", "content": AUTOFILL_RULES}, {"role": "user", "content": user}],
            schema=AUTOFILL_SCHEMA, name="form_answers",
        )
        for a in json.loads(content).get("answers", []):
            if isinstance(a, dict) and a.get("id"):
                out[str(a["id"])] = a
    return out


def _clean(field: dict[str, Any], value: str) -> str:
    """Make an answer valid for the field (option text, limits, number format)."""
    ftype = field.get("type")
    value = str(value or "").strip()
    if not value:
        return ""
    options = field.get("options") or []
    if ftype in ("select", "radio") or (ftype == "combobox" and options):
        return match_option(value, options)
    if ftype == "checkbox_group":
        picked = [match_option(v, options) for v in value.split("||")]
        return " || ".join(dict.fromkeys(p for p in picked if p))
    if ftype == "checkbox":
        return "true" if value.lower() in {"true", "yes", "1", "checked"} else "false"
    if ftype == "number":
        m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        return m.group(0) if m else ""
    if ftype == "email":
        m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", value)
        return m.group(0) if m else ""
    max_chars, max_words = _limits(field)
    return _fit_length(value, max_chars, max_words)


# Agreements the user must accept personally; never ticked automatically.
CONSENT = re.compile(
    r"\b(i agree|agree to|accept|consent|terms|privacy|policy|declar|certify|confirm that|acknowledge|authori[sz]e .*(check|verif)|background (check|verification))",
    re.I,
)


def _phone_for(field: dict[str, Any], profile: dict[str, Any]) -> str:
    """Full "+91 98..." unless the field only fits the 10-digit number."""
    p = profile["personal"]
    full = f"{p['phone_country_code']} {p['phone']}".strip()
    hint = f"{field.get('label', '')} {field.get('placeholder', '')} {field.get('hint', '')}"
    small = isinstance(field.get("maxlength"), int) and field["maxlength"] < len(full)
    if small or re.search(r"10[ -]?digit|without (country )?code|^\d{5}\s?\d{5}$", hint, re.I) or field.get("type") == "number":
        return p["phone"]
    return full


def autofill(fields: list[dict[str, Any]], job_context: str = "") -> list[dict[str, Any]]:
    """Answers for a list of form fields: [{id, value, source, needs_review}]."""
    answers_out = _autofill(fields, job_context)
    by_id = {f["id"]: f for f in fields}
    profile = load_profile()
    decline = re.compile(r"prefer not|decline|don.?t wish|do not wish|not (to )?(say|disclose|answer)|rather not", re.I)
    for a in answers_out:
        f = by_id.get(a["id"], {})
        # Never guess personal identity: unset gender/caste/religion -> "prefer not to say" (or blank).
        for key, pattern in (("gender", r"gender|sex"), ("caste_category", r"caste|category.*(sc|st|obc|general)"), ("religion", r"religion")):
            set_value = str(profile.get("diversity", {}).get(key) or profile.get("personal", {}).get(key) or "").strip()
            if re.search(pattern, f.get("label", ""), re.I) and not set_value:
                opt = next((o for o in (f.get("options") or []) if decline.search(o)), "")
                a.update(value=opt, needs_review=not opt, source="profile")
        label = f"{f.get('label', '')} {' '.join(f.get('options') or [])}"
        if f.get("type") == "checkbox" and CONSENT.search(label) and not re.search(r"newsletter|marketing|promotional|job alerts", label, re.I):
            a.update(value="", needs_review=True, note="agreement: decide yourself")
        label_n = _norm(f.get("label", ""))
        if (a.get("source") == "profile" and re.search(r"phone|mobile|contact number|whatsapp|cell", label_n)
                and not re.search(r"code|extension|type", label_n) and f.get("type") not in ("select", "radio", "combobox")):
            a["value"] = _phone_for(f, profile)
    return answers_out


def _autofill(fields: list[dict[str, Any]], job_context: str = "") -> list[dict[str, Any]]:
    profile = load_profile()
    rules = _profile_rules(profile)
    answers: dict[str, dict[str, Any]] = {}
    for_ai = []
    for f in fields:
        if f.get("type") == "file":
            continue
        custom = next((v for q, v in profile.get("custom_answers", {}).items() if _norm(q) and _norm(q) == _norm(f.get("label", ""))), None)
        direct = custom if custom is not None else _direct_answer(f, rules)
        if direct is None:
            for_ai.append(f)
        else:
            value = _clean(f, direct) if direct else ""
            answers[f["id"]] = {"id": f["id"], "value": value, "source": "profile", "needs_review": not value}
    try:
        ai = _llm_answers(for_ai, profile, job_context)
    except Exception as exc:  # noqa: BLE001 — profile answers are still useful without the AI
        ai = {}
        for f in for_ai:
            answers[f["id"]] = {"id": f["id"], "value": "", "source": "ai", "needs_review": True, "error": str(exc)[:200]}
    for f in for_ai:
        if f["id"] in answers:
            continue
        a = ai.get(f["id"], {})
        value = _clean(f, a.get("value", ""))
        answers[f["id"]] = {"id": f["id"], "value": value, "source": "ai", "needs_review": bool(a.get("needs_review")) or not value}
    return [answers[f["id"]] for f in fields if f["id"] in answers]


# ---------------------------------------------------------------- job matching for a page

def find_job(page_url: str, page_text: str, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The saved job a page belongs to (by URL, else by title + company appearing on the page)."""
    url = (page_url or "").split("#")[0].rstrip("/")
    for job in jobs:
        ju = (job.get("url") or "").split("?")[0].rstrip("/")
        if ju and (url.startswith(ju) or ju in url):
            return job
    text = _norm(page_text[:20000])
    best, best_score = None, 0
    for job in jobs:
        title, company = _norm(job.get("title", "")), _norm(job.get("employer") or job.get("company", ""))
        if not title or len(title) < 6:
            continue
        score = (2 if title in text else 0) + (1 if company and company in text else 0)
        if score > best_score:
            best, best_score = job, score
    return best if best_score >= 3 else None


# ---------------------------------------------------------------- apply kit

KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "cover_letter": {"type": "string"},
        "why_hire_me": {"type": "string"},
        "about_me": {"type": "string"},
        "relevant_project": {"type": "string"},
        "why_this_company": {"type": "string"},
        "linkedin_note": {"type": "string"},
        "email_subject": {"type": "string"},
        "email_body": {"type": "string"},
    },
    "required": ["cover_letter", "why_hire_me", "about_me", "relevant_project", "why_this_company",
                 "linkedin_note", "email_subject", "email_body"],
    "additionalProperties": False,
}

KIT_RULES = """You write job application material for the candidate, tailored to ONE job.
Use ONLY facts from the resume and profile; never invent experience, numbers or skills. Mention a technology only if
the resume lists it, and only credit it to the job/project where the resume says it was used. Name tools exactly as
the resume does ("GPT", not "GPT-4"). Pick the 1-2 most relevant
projects or internships for this job and mention concrete results from the resume. Plain, confident, specific; no
clichés ("I am writing to express", "passionate", "synergy"). Indian English is fine.

- cover_letter: 150-220 words, 3 short paragraphs, addressed "Dear Hiring Team,", signed with the candidate's name.
- why_hire_me: ~100 words.
- about_me: ~80 words ("Tell us about yourself").
- relevant_project: ~100 words on the single most relevant project/internship for this job.
- why_this_company: ~60 words, based only on what the job description says about the company/role; if it says
  nothing about the company, focus on the role and team work described.
- linkedin_note: connection request to a recruiter/engineer at the company, MAX 280 characters, mention the role.
- email_subject: short, includes the role title.
- email_body: 90-130 word cold email to a recruiter/hiring manager, ends with a line offering the resume and links."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError:
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    with _lock:
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def apply_kit(job: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    from fresherpilot.jobs import llm_filter

    key = f"{job.get('source')}:{job.get('id')}"
    cache = _load_json(_upath("applykit_cache.json"))
    if key in cache and not refresh:
        return cache[key]
    profile = load_profile()
    job_text = (
        f"Title: {job.get('title')}\nCompany: {job.get('employer') or job.get('company')}\nLocation: {job.get('location')}\n"
        f"Description:\n{(job.get('description') or '')[:4000]}"
    )
    user = f"PROFILE:\n{compact_profile(profile)}\n\nRESUME:\n{_resume_body()}\n\nJOB:\n{job_text}"
    messages = [{"role": "system", "content": KIT_RULES}, {"role": "user", "content": user}]
    client = llm_filter._client()
    content, model = llm_filter._create(client, messages, schema=KIT_SCHEMA, name="apply_kit")
    kit = json.loads(content)
    # Safety net: if the text names well-known tools that aren't on the resume, rewrite once without them.
    invented = unsupported_terms(" ".join(str(v) for v in kit.values()))
    if invented:
        messages += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": f"These are NOT in the resume, remove them everywhere: {', '.join(invented)}. "
                                        "Return the full corrected JSON."},
        ]
        content, model = llm_filter._create(client, messages, schema=KIT_SCHEMA, name="apply_kit")
        kit = json.loads(content)
    kit["linkedin_note"] = _fit_length(kit.get("linkedin_note", ""), 290, None)
    kit.update(generated_at=datetime.now(timezone.utc).isoformat(), model=model)
    cache[key] = kit
    _save_json(_upath("applykit_cache.json"), cache)
    return kit


# Well-known tools an LLM likes to add. Any of these in generated text must also appear in the resume.
KNOWN_TECH = [
    "GPT-4", "GPT-4o", "GPT-3.5", "Claude", "Gemini", "Llama", "Mistral", "FAISS", "Pinecone", "Weaviate", "Chroma",
    "Milvus", "pgvector", "Elasticsearch", "PyTorch", "TensorFlow", "Hugging Face", "Transformers", "spaCy", "NLTK",
    "OpenCV", "XGBoost", "LightGBM", "Spark", "PySpark", "Kafka", "Airflow", "Kubernetes", "Terraform", "Jenkins",
    "GCP", "BigQuery", "Snowflake", "Databricks", "MongoDB", "Redis", "Celery", "GraphQL", "React", "Node.js",
    "TypeScript", "Java", "Spring", "Go", "Rust", "C++", "MLflow", "Kubeflow", "SageMaker", "Vertex AI", "Streamlit",
    "Gradio", "LlamaIndex", "CrewAI", "AutoGen", "DSPy", "vLLM", "Ray",
]


def unsupported_terms(text: str) -> list[str]:
    resume = _resume_text().lower()
    found = []
    for term in KNOWN_TECH:
        pattern = rf"(?<![\w.+-]){re.escape(term)}(?![\w+-])"
        if re.search(pattern, text, re.I) and not re.search(pattern, resume, re.I):
            found.append(term)
    return found


def cached_kit(job: dict[str, Any]) -> dict[str, Any] | None:
    return _load_json(_upath("applykit_cache.json")).get(f"{job.get('source')}:{job.get('id')}")


# ---------------------------------------------------------------- application tracking

STATUSES = ["applied", "interview", "offer", "rejected"]


def applications() -> dict[str, Any]:
    return _load_json(_upath("applications.json"))


def set_application(uid: str, status: str | None, note: str | None = None) -> dict[str, Any]:
    data = applications()
    if not status:
        data.pop(uid, None)
    else:
        entry = data.get(uid, {})
        entry.setdefault("applied_at", datetime.now(timezone.utc).isoformat())
        entry["status"] = status
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        if note is not None:
            entry["note"] = note
        data[uid] = entry
    _save_json(_upath("applications.json"), data)
    return data.get(uid, {})
