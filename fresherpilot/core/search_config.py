"""What to search for on each job board. Edit this file to change coverage.

Searches are specific to the candidate's stack (AI / ML / GenAI / LLM / RAG / data science /
Python / backend / software developer), only jobs posted in the last 3 days, fresher / entry
level where the board supports it, and no result caps: whatever matches comes in. The AI filter
and skill-fit check then decide what is shown.
"""

import json
from urllib.parse import quote_plus

# Merged near-duplicate searches (measured: overlapping searches made Indeed return 37% duplicates
# and Naukri 11%). "ml engineer" is covered by machine learning engineer, "fastapi/django developer"
# by python developer, "software developer" by software engineer, "rag engineer" by llm engineer.
ROLES = [
    "ai engineer", "machine learning engineer", "generative ai engineer", "llm engineer", "nlp engineer",
    "data scientist", "python developer", "python backend developer", "backend developer", "software engineer",
]
ALL_ROLES = ROLES

# How long a job stays in the list after it was first fetched.
KEEP_JOBS_DAYS = 21
POSTED_WITHIN_DAYS = 3
# Wellfound charges ~$0.06 just to start a run: run it at most once per this many hours.
WELLFOUND_MIN_GAP_HOURS = 8  # Wellfound runs with the first fetch of each day (start fee per run)

# Safety caps per run (USD). Not result limits: a cap only stops a run that would cost more than this.
# The Apify free plan has $5/month, so these are set to stop a single run eating most of it.

# Naukri: blackfalcondata/naukri-jobs-feed (~$0.0004/job). Naukri's own department filter keeps only
# Engineering - Software & QA (5) and Data Science & Analytics (3): in a test 88% of results were in
# those departments instead of the doctors / teachers / BPO jobs plain keyword search returned.
NAUKRI = {
    "actor": "blackfalcondata/naukri-jobs-feed",
    "input": {
        "searchQueries": ROLES,
        "experience": "0",          # jobs whose range includes 0 years
        "departmentIds": ["5", "3"],
        "freshness": POSTED_WITHIN_DAYS,
        "sortBy": "date",
        "fetchDetails": True,       # full description + department
        "maxResults": 0,            # no cap
        "descriptionFormat": "text",
        "excludeEmptyFields": True,
        "incremental": True,        # only new / changed jobs after the first run
        "stateKey": "jobfinder-naukri",
    },
    "max_charge_usd": 0.6,
}

# LinkedIn search links posted in the last 3 days (f_TPR in seconds; the actor's own time filter only
# offers 1, 7 or 30 days) and full-time only (f_JT=F). Tested: LinkedIn's entry-level filter (f_E) makes
# this actor return nothing, so experience is left to the AI filter.
_LI_SECONDS = POSTED_WITHIN_DAYS * 24 * 3600
# Most valuable first: searches run in this order while the daily budget lasts. Each title is its
# own search: one combined "A OR B" search returned only ~300 jobs a day vs ~585 for "AI Engineer" alone.
LINKEDIN_TITLES = [
    "AI Engineer", "Machine Learning Engineer", "Data Scientist",
    "Python Developer", "Backend Developer", "Software Engineer",
]
# Title words that never fit (checked on ~4,800 judged jobs: they removed ~50% of them, and only
# 2 of those had been approved). Used twice: LinkedIn drops them before you pay, and every board's
# jobs with them are rejected before the AI check (no tokens). Words are matched inside the title,
# so keep the spaces ("lead " doesn't hit "leading"). Stack words are ignored when the title also
# says Python / AI / ML ("Java, Python, AWS developer" stays).
TITLE_EXCLUDE = {
    "too_senior": ["senior", "sr.", "sr ", "lead ", "team lead", "tech lead", "principal", "staff engineer", "staff software",
                   "staff data", "staff ml", "manager", "director", "head ", "head of", "vp ", "vice president", " ii ", " iii ",
                   " iv ", "sde 2", "sde-2", "sde2", "sde 3", "sde-3", "sde3", "staff ai", "staff machine", "staff backend",
                   "staff python", "smts", "lmts", "pmts"],
    "internship": ["intern ", "internship", "apprentice"],
    "off_stack": ["java ", "java/", "java-", ".net", "dotnet", "c#", "php", "laravel", "salesforce", "sap ", "servicenow",
                  "android", "ios ", "flutter", "angular", "vue", "frontend", "front end", "front-end", "ruby", "golang",
                  "mainframe", "cobol", "wordpress", "shopify"],
    "non_developer": ["sales", "marketing", "recruiter", "talent acquisition", "accountant", "teacher", "faculty", "tutor",
                      "nurse", "doctor", "customer", "voice process", "international voice", "telecaller", "tele caller", "bpo",
                      "counsel", "business development", "data entry", "content writer", "hr executive", "hr generalist",
                      "hr manager", "audit", "legal", "support engineer", "technical support", "helpdesk", "desktop support"],
    "non_software_engineering": ["mechanical", "civil engineer", "electrical"],
}

# LinkedIn: valig/linkedin-jobs-scraper ($0.0004 per job, vs $0.0015 for the old one, which used 84%
# of the monthly Apify budget). One search for all titles (LinkedIn's OR syntax), full-time, India,
# and the excluded title words are dropped before they are charged.
LINKEDIN = {
    "actor": "valig/linkedin-jobs-scraper",
    "input": {
        "location": "India",
        # newest first, so a run stopped by its spending cap keeps the newest jobs
        "urlParam": [{"key": "f_JT", "value": "F"}, {"key": "f_TPR", "value": f"r{_LI_SECONDS}"}, {"key": "sortBy", "value": "DD"}],
        "titleExclude": sorted({w.strip() for words in TITLE_EXCLUDE.values() for w in words
                                if len(w.strip()) > 3 and w.strip() not in ("java", "head", "intern")}),
        "limit": 1000,
    },
    "max_charge_usd": 0.4,  # per search
}

INDEED = {
    "actor": "deltaspider/indeed-scraper",
    # Last 3 days, full-time, sorted by relevance. Tested: Indeed India ignores its entry-level URL filter,
    # and newest-first sorting returns unrelated senior roles; "<role> fresher" queries are on target.
    "search_url": "https://in.indeed.com/jobs?q={query}&l=India&fromage=" + str(POSTED_WITHIN_DAYS) + "&jt=fulltime",
    # Plain titles: "<role> fresher" searches missed ~95% of jobs (33 vs 571 in a test), including
    # "Associate Software Engineer" / "Software Trainee Engineer". Seniors are dropped by title, free.
    "queries": list(ROLES),
    "input": {"country": "IN", "maxItems": 1000},
    "max_charge_usd": 0.5,
}

WELLFOUND = {
    "actor": "abotapi/wellfound-jobs-scraper",
    "input": {
        "mode": "search",
        "locations": ["bangalore", "hyderabad", "pune", "chennai", "mumbai", "delhi", "gurgaon", "noida", "kolkata", "ahmedabad"],
        "roleKeywords": ["engineer", "developer", "scientist"],
        "keywords": ["python", "machine learning", "ml", "llm", "genai", "generative ai", "rag", "nlp", "data scientist",
                     "data science", "backend", "ai engineer", "artificial intelligence", "fastapi", "django"],
        "jobTypes": ["full-time"],
        "experienceLevel": "entry",  # up to 1 year required
        "includeNoExperience": True,
        "postedWithinDays": POSTED_WITHIN_DAYS,
        "maxItems": 500,
        "sortBy": "newest",
        "incrementalMode": True,  # later runs return only new / updated jobs
    },
    "max_charge_usd": 0.5,
}


# Internshala's full-time "fresher jobs" section (not internships). Incremental mode: after the first
# run only new / updated listings are returned (and charged, ~$0.0004 each).
INTERNSHALA = {
    "actor": "blackfalcondata/internshala-scraper",
    "input": {
        "listingType": "job",
        "query": json.dumps(["python", "machine learning", "data science", "artificial intelligence",
                             "generative ai", "backend development", "software development"]),
        "country": "IN",
        "maxResults": 0,
        "maxPages": 3,
        "includeDetails": True,
        "descriptionFormat": "text",
        "excludeEmptyFields": True,
        "incrementalMode": True,
        "stateKey": "jobfinder-fresher-jobs",
    },
    "max_charge_usd": 0.3,
}

# Daily Apify budget: the credit left this billing cycle / days left (so the free $5 lasts the
# month), shared by all fetches of one day. Set APIFY_USD_PER_DAY to a number to fix it instead.
# Each board gets a share; every search is sorted newest first, so a board that reaches its share
# has already kept the newest jobs. Full coverage of all boards costs roughly $1/day.
APIFY_USD_PER_DAY: float | None = None
BUDGET_SHARE = {"linkedin": 0.45, "naukri": 0.25, "indeed": 0.12, "internshala": 0.10, "wellfound": 0.08}
MIN_RUN_USD = 0.02

# ---------------------------------------------------------------- company career pages (free)
# Public job boards of companies (Greenhouse / Lever / Ashby). Only India-based openings are used.
# Add a company: ("greenhouse" | "lever" | "ashby", "<board name in its jobs URL>", "<Company>").
# e.g. jobs.lever.co/cred -> ("lever", "cred", "CRED"); jobs.ashbyhq.com/sarvam -> ("ashby", "sarvam", "Sarvam AI")
CAREERS = {"runner": "careers"}
CAREERS_FIRST_DAYS = 21  # the first fetch takes openings from the last 3 weeks; later fetches only new ones
CAREER_PAGES = [
    # AI-first
    ("ashby", "sarvam", "Sarvam AI"),
    ("ashby", "composio", "Composio"),
    ("ashby", "atlan", "Atlan"),
    ("greenhouse", "observeai", "Observe.AI"),
    ("lever", "netomi", "Netomi"),
    ("greenhouse", "sigmoid", "Sigmoid"),
    ("greenhouse", "turing", "Turing"),
    ("ashby", "openai", "OpenAI"),
    ("greenhouse", "anthropic", "Anthropic"),
    ("greenhouse", "scaleai", "Scale AI"),
    ("ashby", "writer", "Writer"),
    ("greenhouse", "databricks", "Databricks"),
    ("ashby", "snowflake", "Snowflake"),
    # Indian product companies
    ("greenhouse", "groww", "Groww"),
    ("lever", "cred", "CRED"),
    ("lever", "meesho", "Meesho"),
    ("lever", "zeta", "Zeta"),
    ("lever", "paytm", "Paytm"),
    ("greenhouse", "inmobi", "InMobi"),
    ("greenhouse", "highradius", "HighRadius"),
    ("lever", "mindtickle", "Mindtickle"),
    ("lever", "hevodata", "Hevo Data"),
    ("greenhouse", "druva", "Druva"),
    ("greenhouse", "hackerrank", "HackerRank"),
    ("lever", "pocketfm", "Pocket FM"),
    ("greenhouse", "zetaglobal", "Zeta Global"),
    # Global companies with India teams
    ("greenhouse", "mongodb", "MongoDB"),
    ("greenhouse", "elastic", "Elastic"),
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "stripe", "Stripe"),
    ("greenhouse", "airbnb", "Airbnb"),
    ("greenhouse", "coinbase", "Coinbase"),
    ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "figma", "Figma"),
    ("ashby", "notion", "Notion"),
]


# ---------------------------------------------------------------- per-user search preferences
# Defaults = the original owner's setup. Each user's own values (made from their resume during
# onboarding, editable in Settings) are stored in users/<id>/search_prefs.json and override these.
DEFAULT_PREFS = {
    "onboarded": True,
    "headline": "2026 B.E. Information Technology graduate",
    "summary": "Python developer: AI/ML, GenAI (LLMs, RAG, agents), data science, Python backend/REST APIs "
               "(FastAPI/Flask/Django), Docker, AWS/Azure.",
    "target_area": "AI/ML, GenAI/LLM, data science, Python or backend development",
    "experience_years": 0,
    "reject_from_years": 1,           # jobs needing this many years or more are rejected
    "roles": ROLES,                   # Naukri / Indeed searches
    "linkedin_titles": LINKEDIN_TITLES,
    "off_stack": TITLE_EXCLUDE["off_stack"],     # stacks this person doesn't work with (title words)
    "on_stack_words": ["python", "ai", "ml", "machine learning", "data scien", "llm", "genai", "gen ai", "nlp"],
    "wellfound_keywords": WELLFOUND["input"]["keywords"],
    "internshala_queries": json.loads(INTERNSHALA["input"]["query"]),
    "naukri_departments": NAUKRI["input"]["departmentIds"],
    "skills": [],                     # empty = read from the resume's SKILLS section
    # Cities to search (e.g. ["Pune", "Bangalore", "Remote"]). Empty = all of India. Sent to every
    # board's search, so Apify only returns (and charges for) jobs there; "Remote" adds remote jobs.
    "locations": [],
}

# Other spellings of a city, used when checking a job's location text.
CITY_ALIASES = {
    "bangalore": ["bangalore", "bengaluru"], "bengaluru": ["bangalore", "bengaluru"],
    "gurgaon": ["gurgaon", "gurugram"], "gurugram": ["gurgaon", "gurugram"],
    "delhi": ["delhi", "ncr"], "new delhi": ["delhi", "ncr"], "ncr": ["delhi", "ncr", "noida", "gurgaon", "gurugram"],
    "mumbai": ["mumbai", "bombay", "thane"], "chennai": ["chennai", "madras"],
    "kolkata": ["kolkata", "calcutta"], "trivandrum": ["trivandrum", "thiruvananthapuram"],
}
