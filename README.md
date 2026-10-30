# FresherPilot

**An AI job-search copilot for Indian fresher developers.** It pulls fresh openings from LinkedIn,
Naukri, Indeed, Internshala, Wellfound and company career pages, uses an LLM to keep only the
**full-time, 0–1 year roles that fit your stack**, ranks them against your resume, finds recruiters
to contact, and helps you apply: autofill or a supervised auto-apply in your own browser.

Runs locally on your computer. Free tiers are enough: Apify ($5/month credit) and Groq (free API).

```
Job boards ──▶ fetch (Apify + free career pages) ──▶ AI filter (Groq) ──▶ rank (local embeddings)
                                                                              │
                     Browser extension ◀── apply kit / autofill / auto-apply ◀┤
                     Gmail · WhatsApp  ◀── recruiter outreach  ◀── contacts ◀─┘
```

> Architecture, data flow and module map: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

---

## Features

- **Multi-board fetching** with a daily Apify spend cap, so the free $5 lasts the whole month.
  - LinkedIn, Naukri, Indeed, Internshala and Wellfound go through Apify actors.
  - About 35 company career pages (Greenhouse / Lever / Ashby) are fetched for free.
  - Only jobs posted since the last fetch are requested, so nothing is paid for twice.
- **Location filter**: pick cities (e.g. `Pune, Mumbai, Bengaluru, Hyderabad, Remote`). They are
  sent into each board's search, so jobs elsewhere are never fetched or charged.
- **AI relevance filter**: free title rules run first, then an LLM pass.
  - The LLM extracts facts: employment type, minimum years, seniority, role type, and whether
    internships count as experience.
  - Your preferences are applied to those facts in code. 45 test cases cover tricky postings.
- **Resume-based ranking** with local `bge-small` embeddings and a skill-fit check (strong / partial).
- **Apply kit** for each job: cover letter, "why hire you", project answer, LinkedIn note, cold email.
  It also flags special "how to apply" instructions (Google Forms, email subjects).
- **Contacts**: recruiters and hiring leaders on LinkedIn, public HR emails and phone numbers, and
  likely inboxes checked against the domain's MX record.
- **Browser extension (MV3)**:
  - Autofills application forms from your profile, and the AI answers the custom questions.
  - Auto-apply clicks through applications, and pauses for you on sign-in, CAPTCHA or unknown questions.
- **Outreach**: templated emails through Gmail and WhatsApp Web messages, with daily caps and a send log.
- **Dashboard**: applications per day, pipeline (applied → interview → offer), outreach stats.
- **Multi-user**: separate accounts on one machine, each with its own data folder. API keys are
  encrypted with the user's password.

## Tech stack

| Layer | Tools |
|---|---|
| Backend | Python 3.10+, FastAPI, Uvicorn, background threads |
| AI | Groq (`gpt-oss-120b` → `gpt-oss-20b` → `qwen` fallback), sentence-transformers (`BAAI/bge-small-en-v1.5`) |
| Scraping | Apify actors, Greenhouse / Lever / Ashby public APIs, `ddgs` web search |
| Frontend | Vanilla HTML / CSS / JS (no build step) |
| Extension | Chrome / Edge Manifest V3 |
| Storage | JSON files per user (no database) |

---

## Getting started

### 1. Install

```bash
git clone https://github.com/<your-username>/FresherPilot.git
cd FresherPilot
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

### 2. Run

```bash
python main.py                  # Windows: set PYTHONIOENCODING=utf-8 if the console shows encoding errors
```

Open **http://localhost:8000** and keep the terminal open while you use the app.

### 3. Create your account

1. **Create an account.** The first account becomes the owner.
2. Add your keys in **Settings**:
   - [Apify](https://console.apify.com/account/integrations) API token
   - [Groq](https://console.groq.com/keys) API key
   - Optional: [Serper](https://serper.dev) for Google search, and a Gmail
     [app password](https://myaccount.google.com/apppasswords) for email outreach
3. **Upload your resume** (or write a few lines about yourself). The AI drafts your profile and search
   settings: roles, skills, stacks to skip. Check them, add your cities in **Settings**, and save.
4. Fill the remaining fields in **Profile** (expected CTC, pincode, date of birth…).

Keys can also go in a `.env` file for the owner account. See [.env.example](.env.example).

### 4. Install the browser extension (for autofill / auto-apply)

1. Open `chrome://extensions` or `edge://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and select the [`extension/`](extension/) folder.
3. Refresh the app page while logged in. The extension then works as your account.

---

## Daily use

1. **Fetch new jobs.** This takes a few minutes. New jobs show as *Pending AI check*, and the AI check
   runs in the background, best matches first.
2. **Open a job** to see:
   - the match score, fit, required experience and the AI's reasoning
   - the apply kit and contacts
3. **Apply**:
   - open the job page and press **Alt+Shift+F** to autofill, or
   - use **Auto-apply** (one job) / **Auto-apply all**.
4. **Reach out** with **Send to all** (email + WhatsApp, from your templates, no AI tokens).
5. **Track** status (Applied / Interview / Offer / Not selected) on the dashboard.

## Configuration

| What | Where |
|---|---|
| Roles, cities, skills, stacks to skip, years of experience | **Settings** page (per user) |
| Boards, actor inputs, title exclude words, daily Apify budget split | [`core/search_config.py`](fresherpilot/core/search_config.py) |
| Company career pages | `CAREER_PAGES` in [`core/search_config.py`](fresherpilot/core/search_config.py) |
| AI prompts and model fallback order | [`jobs/llm_filter.py`](fresherpilot/jobs/llm_filter.py) |
| Auto-apply pacing | `SETTINGS` in [`apply/autoapply.py`](fresherpilot/apply/autoapply.py) |
| Outreach daily caps | `DAILY_*_CAP` in [`outreach/messages.py`](fresherpilot/outreach/messages.py) |

## Costs and limits

| Service | Free tier | Usage |
|---|---|---|
| Apify | $5 / month, 5 concurrent runs | Full coverage ≈ $1/day, so spend is capped to *credit left ÷ days left* |
| Groq | 200k tokens / day per model | ~35 tokens per job for the title screen, ~275 for the full check |

When Groq's daily quota runs out, jobs stay *Pending AI check* and are checked automatically after the reset.

## Testing

```bash
python -m scripts.check_filter          # 45 hard cases for the AI filter (uses real Groq calls)
python -m scripts.recheck "<job title>"  # re-judge specific cached jobs after a prompt change
```

## Project structure

```
FresherPilot/
├── main.py                    entry point: python main.py → http://localhost:8000
├── fresherpilot/              backend (Python package)
│   ├── server.py              FastAPI app, auth middleware, API endpoints, background fetch/check threads
│   ├── core/
│   │   ├── users.py           accounts, sessions, per-user data dirs, encrypted keys, search prefs
│   │   └── search_config.py   board configs, title rules, default prefs, Apify budget, career pages
│   ├── jobs/                  job pipeline
│   │   ├── apify_jobs.py      Apify runs, per-board normalizers, location filter, merge/dedupe, cache
│   │   ├── ats_jobs.py        free Greenhouse / Lever / Ashby career pages
│   │   ├── llm_filter.py      Groq client + model fallback, pre-screen, fact extraction, decide(), fit
│   │   ├── ranker.py          embedding ranking against the resume
│   │   ├── candidate.py       resume parsing (sections, skills)
│   │   ├── dashboard.py       dashboard statistics
│   │   └── visited.py         visited-job tracking
│   ├── apply/                 applying
│   │   ├── onboarding.py      resume / about text → AI-drafted profile and search prefs
│   │   ├── applicant.py       applicant profile, form answers, apply kit, application tracking
│   │   └── autoapply.py       auto-apply decision loop (next action for the extension)
│   └── outreach/              reaching recruiters
│       ├── contacts.py        recruiter / email / phone discovery
│       ├── domains.py         company domain + MX check + likely inboxes
│       ├── websearch.py       free web search (ddgs) or Serper
│       └── messages.py        email (Gmail SMTP) + WhatsApp templates, caps, send log
├── web/                       UI served at /static (jobs, dashboard, outreach, settings, onboarding, login)
├── extension/                 MV3 browser extension (autofill, auto-apply, WhatsApp runner)
├── scripts/                   check_filter.py (AI filter tests), recheck.py (re-judge cached jobs)
├── docs/ARCHITECTURE.md       design and data flow
└── users/                     runtime data per account (git-ignored, created on first run)
```

## Privacy

Everything runs on your computer. Your resume, jobs, applications and keys are stored under
`users/<id>/`, which git ignores. Data leaves your machine only through the services you configure:
Apify, Groq, Serper, Gmail and WhatsApp Web.

## Disclaimer

LinkedIn, Naukri and Indeed don't allow automated applying in their terms of service. Use auto-apply
at your own risk and at modest volume. Autofill (you review and submit) is the low-risk option.
WhatsApp may restrict accounts that message many strangers, so keep outreach volume low.

## License

[MIT](LICENSE)
