# FresherPilot: context for Claude

Job-search app for Indian fresher devs (owner: AI/ML/Python, 2026 B.E. IT). FastAPI + vanilla JS, runs locally: `python main.py` → http://localhost:8000. Windows; use `PYTHONIOENCODING=utf-8`. Read only the files a task needs; don't scan everything. Design doc: `docs/ARCHITECTURE.md`.

## Flow
Fetch (free: LinkedIn public search, Indeed via jobspy, career pages, Telegram, extension Naukri sweep; Apify: Naukri/Internshala/Wellfound + LinkedIn/Indeed fallback) → `jobs_cache.json` → background AI checker (Groq) → UI list/dashboard → apply (extension autofill/auto-apply) / outreach (Gmail, WhatsApp).

## Files (package `fresherpilot/`, imports like `from fresherpilot.core import users`)
- `main.py` (root): thin entry, runs `fresherpilot.server.run()`.
- `server.py`: API, auth middleware (`UserContext`), per-user background fetch/checker (`_T()`), all endpoints. Serves `web/` at `/static`.
- `core/users.py`: accounts, sessions, per-user data dir `users/<id>/`, encrypted keys (`users.key(NAME)`), `users.prefs()` (per-user search settings), `users.thread()` (keeps user context in threads). First account = owner, adopts legacy root files + `.env` keys.
- `core/search_config.py`: board configs (actors/inputs), `TITLE_EXCLUDE`, `DEFAULT_PREFS`, `CITY_ALIASES`, daily Apify budget (`BUDGET_SHARE`), `CAREER_PAGES`.
- `jobs/apify_jobs.py`: runs actors (≤4 concurrent, `_ACTOR_SLOTS`), normalizers per board, LinkedIn = title × city valig searches in 2 waves with `skipJobId`, daily budget caps, merge/dedupe (title|company), `load_cache()`.
- Location: `prefs.locations` (cities + "Remote") go into each actor's input (`_input_for`, `_run_linkedin`); `in_wanted_location()` drops other cities free before the AI check.
- `jobs/ats_jobs.py`: free Greenhouse/Lever/Ashby India jobs (source "Careers"; retries, 90 s read timeout).
- `jobs/free_boards.py`: free LinkedIn guest search (1 req/2.5 s, 429 backoff; known IDs + excluded titles never opened), Indeed via python-jobspy, public Telegram channels (`t.me/s/<name>`, `prefs.telegram_channels`).
- Naukri: Apify runs 2 passes (`NAUKRI_EXPERIENCE_PASSES` 0 and 1, separate incremental state each; min/max range only applies min, measured) so "1-3 yrs" jobs arrive; free route = extension sweep (`/api/naukri/sweep` plan → `background.js runNaukriSweep` reads result pages in a background tab → `/api/import_jobs` → `merge_external`). Never forge Naukri's nkparam/captcha tokens.
- Google Jobs (`litescrape/google-jobs`, source "Google", no description): ≤2 runs/day × 10 searches (free plan: 25/day), city rotates per run (`_GOOGLE_ROTATION`).
- SmartRecruiters career pages (`ats_jobs._smartrecruiters`, India postings, details only for new ones).
- `jobs/missed.py` + `POST /api/missed` + "Missed a job?" button: checks a pasted link, imports it if missing (LinkedIn/ATS free, Naukri via actor `jobIds`), logs to `missed_jobs.jsonl`.
- 1-year jobs ("1-3 yrs", "1+") are never hidden: kept with category "stretch" (or "ok" if internships count). Board data rejects free only from reject_from_years + 1.
- Logins persist across restarts: `users/sessions.json` (token hash → key-unlock secret encrypted with the token), 30 days, renewed by `/api/me`.
- Apify budget comes from Apify's own usage API (counts test copies too); no credit = paid boards skipped, free ones still run.
- `jobs/llm_filter.py`: Groq calls (`_create`: model fallback 120b→20b→qwen, low reasoning, qwen uses plain JSON mode, empty-answer guard), `title_excluded()` (free), `prescreen` (board level + AI title screen), `judge_jobs` (facts) → `decide()` (prefs policy), `judge_intern_credit`, `judge_fit`, `judge_instructions`; cache `llm_verdicts.json`; `cached_verdict()`. AI keys = `_client()` pool: every Groq key in `GROQ_API_KEY` (comma-separated) in order, then `NVIDIA_API_KEY` (OpenAI client, `NVIDIA_MODELS`, plain JSON); daily-limit/401/410 marks that key+model exhausted for 30 min (`_quota_key(model, label)`); `ask_json()` for small free-form calls.
- `jobs/ranker.py` + `jobs/candidate.py`: bge-small embeddings vs resume; skills from resume or `prefs.skills`. `jobs/dashboard.py`: stats. `jobs/visited.py`.
- `apply/applicant.py`: profile (blank `DEFAULT_PROFILE`; data in user's `applicant_profile.json`), form autofill answers, apply kit, applications tracking. `apply/autoapply.py`: auto-apply brain (extension asks next action). `apply/onboarding.py`: resume/about → AI draft profile + prefs.
- `outreach/contacts.py`/`domains.py`/`websearch.py`: recruiter/email/phone search (shared cache `contacts_cache.json` at root). `outreach/messages.py` (imported as `outreach`): templated email (Gmail SMTP) + WhatsApp, caps, log.
- `web/`: `index.html`+`app.js` (jobs), `dashboard.js`, `outreach.js`, `login.html`, `onboarding.html`, `account.html/js/css`, `profile.html`.
- `extension/` (MV3): `content.js` fill, `agent.js` page reading, `background.js` auto-apply + WhatsApp runner, `bridge.js` page↔extension; auth via `X-JF-Token` (`jf-set-token`).
- Tests: `python -m scripts.check_filter` (45 AI filter cases, real Groq), `python -m scripts.recheck "<title>"`.

- `jobs/live_poller.py`: free boards read all the time per user (started by `/api/me` / `/api/jobs`): LinkedIn guest search in turns per title x city with a since-last-read window + `stop_on_known` (saves every 10 jobs via `on_batch`), Indeed/Internshala/Wellfound every `LIVE_EVERY_MIN`; merges with `merge_external(kind="live")` (`via: "live"`, kept by fetches); AI check after 10 new or `LIVE_CHECK_MIN`; triggers the regular fetch every `SCHEDULED_FETCH_HOURS`. `free_boards.internshala()` / `wellfound()` read public pages (Wellfound = `__NEXT_DATA__`, 8 s gap, 403 = 3 h pause).
- Fetch button (`/api/fetch`) = `live_poller.run_now()` (every live board at once, each in its own thread, + a full LinkedIn round shown as `linkedin_round`) + the regular fetch; `/api/status` `running` includes live `busy` boards. LinkedIn opens at most 25 new jobs per search per turn (`max_new`; `li_more` = continue next turn without `stop_on_known`). Poller starts only after onboarding.
- New users: sign-up → `/onboarding` (also a brand-new owner); `_setup_missing()` blocks the AI check without an AI key or resume (message in the check error; `/api/me` `setup` drives the "Finish setup" banner); the jobs list says how many wait for the AI check.
- Server deploy: `deploy/setup_server.sh <TUNNEL_TOKEN>` (Ubuntu/Oracle: venv, systemd `fresherpilot`, cloudflared, daily backups), `deploy/update.sh`; `/api/health` public. `.env` values: never put `# comments` after `KEY=` (dotenv reads them as the value).
- Apify only for `cfg.APIFY_BOARDS` (= Naukri); `LIVE_BOARDS` are skipped in `fetch_latest_jobs`. Daily cap = credit left / days left, so the month stays within $5.
- Local mode (`FP_MODE=local`, default; currently in use): requests from this PC (127.0.0.1, no `cf-connecting-ip`/`x-forwarded-for`) are signed in as the owner automatically (`users.local_token()`, owner's unlock secret wrapped with the server secret as `lwrap`, saved by `remember_local()` on the owner's first password sign-in or valid session). `FP_MODE=cloud` (set by `deploy/setup_server.sh`) turns it off.
- Deploy (docs/DEPLOY.md): private, invite-only, PC + Cloudflare Tunnel (home IP keeps LinkedIn working), app binds 127.0.0.1. Google sign-in (`/api/auth/google`, GIS ID token verified with google-auth, `GOOGLE_CLIENT_ID`): new accounts only for `users.allowed_emails()` (Settings → Invites, `FP_ALLOWED_EMAILS`, `FP_OWNER_EMAIL`); Google users' key-encryption key wrapped with `FP_SECRET_KEY`/`users/server.key` (`gwrap`); password sign-up only for the first account unless `FP_PASSWORD_SIGNUP=1`. Consent recorded per account (`users.TERMS_VERSION`; bump when `web/terms.html`/`privacy.html` change). Data rights: `/api/account/export` (zip), `/api/account/delete`; owner: `/api/admin/invites`, `DELETE /api/admin/users/{uid}`. Secure cookie + HSTS when `X-Forwarded-Proto`/`CF-Visitor` is https; 10 failed sign-ins / 15 min per IP. Extension registers `bridge.js` for a non-localhost server set in its popup.

## Rules / gotchas
- Every data file is per user: use `users.path(name)`, never `ROOT/...`. `ROOT` in every module = project root. Keys only via `users.key()`; non-owners never fall back to `.env`. Never print key values.
- Apify free plan: $5/month, max 5 concurrent runs. Full coverage ≈ $1/day, so spend is capped daily.
- Groq free: 200k tokens/day per model. Prefer free code filters before AI calls.
- Owner wants: full-time, <1 yr experience (1 yr only if internships count), no internships/BPO/sales, own stack only, AI not hardcoded rules for relevance.
- Patch big edits via scripts in the scratchpad; verify with `python -c "import main"` and `node --check`.
- Test servers on port 8001 with a copied project dir (`python -m uvicorn main:app --port 8001`; creating the first account migrates data). Poll status every ≤3s (uvicorn keep-alive is 5s).
- Commit only when asked.
