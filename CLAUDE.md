# FresherPilot: context for Claude

Job-search app for Indian fresher devs (owner: AI/ML/Python, 2026 B.E. IT). FastAPI + vanilla JS, runs locally: `python main.py` → http://localhost:8000. Windows; use `PYTHONIOENCODING=utf-8`. Read only the files a task needs; don't scan everything. Design doc: `docs/ARCHITECTURE.md`.

## Flow
Fetch (Apify + free career pages) → `jobs_cache.json` → background AI checker (Groq) → UI list/dashboard → apply (extension autofill/auto-apply) / outreach (Gmail, WhatsApp).

## Files (package `fresherpilot/`, imports like `from fresherpilot.core import users`)
- `main.py` (root): thin entry, runs `fresherpilot.server.run()`.
- `server.py`: API, auth middleware (`UserContext`), per-user background fetch/checker (`_T()`), all endpoints. Serves `web/` at `/static`.
- `core/users.py`: accounts, sessions, per-user data dir `users/<id>/`, encrypted keys (`users.key(NAME)`), `users.prefs()` (per-user search settings), `users.thread()` (keeps user context in threads). First account = owner, adopts legacy root files + `.env` keys.
- `core/search_config.py`: board configs (actors/inputs), `TITLE_EXCLUDE`, `DEFAULT_PREFS`, `CITY_ALIASES`, daily Apify budget (`BUDGET_SHARE`), `CAREER_PAGES`.
- `jobs/apify_jobs.py`: runs actors (≤4 concurrent, `_ACTOR_SLOTS`), normalizers per board, LinkedIn = title × city valig searches in 2 waves with `skipJobId`, daily budget caps, merge/dedupe (title|company), `load_cache()`.
- Location: `prefs.locations` (cities + "Remote") go into each actor's input (`_input_for`, `_run_linkedin`); `in_wanted_location()` drops other cities free before the AI check.
- `jobs/ats_jobs.py`: free Greenhouse/Lever/Ashby India jobs (source "Careers").
- `jobs/llm_filter.py`: Groq calls (`_create`: model fallback 120b→20b→qwen, low reasoning, qwen uses plain JSON mode, empty-answer guard), `title_excluded()` (free), `prescreen` (board level + AI title screen), `judge_jobs` (facts) → `decide()` (prefs policy), `judge_intern_credit`, `judge_fit`, `judge_instructions`; cache `llm_verdicts.json`; `cached_verdict()`.
- `jobs/ranker.py` + `jobs/candidate.py`: bge-small embeddings vs resume; skills from resume or `prefs.skills`. `jobs/dashboard.py`: stats. `jobs/visited.py`.
- `apply/applicant.py`: profile (blank `DEFAULT_PROFILE`; data in user's `applicant_profile.json`), form autofill answers, apply kit, applications tracking. `apply/autoapply.py`: auto-apply brain (extension asks next action). `apply/onboarding.py`: resume/about → AI draft profile + prefs.
- `outreach/contacts.py`/`domains.py`/`websearch.py`: recruiter/email/phone search (shared cache `contacts_cache.json` at root). `outreach/messages.py` (imported as `outreach`): templated email (Gmail SMTP) + WhatsApp, caps, log.
- `web/`: `index.html`+`app.js` (jobs), `dashboard.js`, `outreach.js`, `login.html`, `onboarding.html`, `account.html/js/css`, `profile.html`.
- `extension/` (MV3): `content.js` fill, `agent.js` page reading, `background.js` auto-apply + WhatsApp runner, `bridge.js` page↔extension; auth via `X-JF-Token` (`jf-set-token`).
- Tests: `python -m scripts.check_filter` (45 AI filter cases, real Groq), `python -m scripts.recheck "<title>"`.

## Rules / gotchas
- Every data file is per user: use `users.path(name)`, never `ROOT/...`. `ROOT` in every module = project root. Keys only via `users.key()`; non-owners never fall back to `.env`. Never print key values.
- Apify free plan: $5/month, max 5 concurrent runs. Full coverage ≈ $1/day, so spend is capped daily.
- Groq free: 200k tokens/day per model. Prefer free code filters before AI calls.
- Owner wants: full-time, <1 yr experience (1 yr only if internships count), no internships/BPO/sales, own stack only, AI not hardcoded rules for relevance.
- Patch big edits via scripts in the scratchpad; verify with `python -c "import main"` and `node --check`.
- Test servers on port 8001 with a copied project dir (`python -m uvicorn main:app --port 8001`; creating the first account migrates data). Poll status every ≤3s (uvicorn keep-alive is 5s).
- Commit only when asked.
