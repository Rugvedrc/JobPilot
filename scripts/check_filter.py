"""Check the Groq filter against tricky cases. Run: python check_filter.py

Uses real Groq calls (results are cached, so re-runs are free). Add a case here
whenever you spot a wrong decision in the UI, then tweak SYSTEM_PROMPT in
llm_filter.py and bump PROMPT_VERSION.
"""

from __future__ import annotations

import sys

from fresherpilot.jobs import llm_filter

# (expected, title, experience_field, description)
CASES = [
    # Internship experience asked for != internship role
    ("keep", "Python Developer", "0-1 years", "Full-time role. Candidates with 6 months of internship experience preferred. Build Django APIs."),
    ("keep", "Software Engineer", "", "Requirements: 1 year of experience (internships count). Write backend services in Go or Python."),
    ("reject", "Software Engineer", "", "Requirements: 1 year of professional full-time experience. Write backend services in Python."),
    # Role itself is an internship, even though the title doesn't say so
    ("reject", "Python Developer", "", "This is a 6-month internship with a stipend of 15k. Work on Flask APIs."),
    ("reject", "Artificial Intelligence Engineer", "0-1 years", "Internship Duration: 6 Months. Full-Time CTC After Internship: 3-4 LPA. Build ML models."),
    ("reject", "Software Engineer Intern", "", "Work on our web app."),
    # BPO company, developer job
    ("keep", "Python Developer", "", "We are a BPO company building internal tools. You will develop APIs in Django and automate workflows."),
    ("keep", "Voice AI Engineer", "0-4 years", "Freshers welcome. Build speech-to-text and TTS pipelines in Python."),
    # Support/BPO dressed up
    ("reject", "Technical Support Engineer (Python)", "", "Handle customer tickets over phone, rotational shifts, voice process, basic Python scripting knowledge a plus."),
    ("reject", "Associate", "", "Voice process, inbound calls and sales targets. Night shifts. Good English required."),
    ("reject", "Team Leader - Offline Merchant QR", "0-1 years", "Lead a team of field sales executives onboarding merchants."),
    # Experience
    ("reject", "Software Engineer", "", "We are a company with 15 years of experience in fintech. Freshers welcome. Java and Spring."),  # off-stack
    ("reject", "Software Engineer", "", "Freshers or candidates with 2-3 years experience can apply. React + Node."),  # off-stack
    ("reject", "Python Backend Engineer", "", "Looking for 4-10 years of software development experience to design distributed systems."),
    ("reject", "AI Engineer", "0-5 years", "Requirements: minimum 3 years of hands-on experience deploying ML models in production."),
    ("reject", "Senior Python Developer", "", "Build APIs."),
    ("reject", "Software Engineer II", "", "Own services end to end."),
    ("reject", "Generative AI Engineer", "Associate", "We're Hiring | GenAI Agentic Developer | 1–4 Years. Build RAG and agent apps."),
    ("reject", "Associate Software Engineer Dotnet Developer", "0-2 years", "0-2 years experience in software development. 1+ years minimum experience with C# preferred."),  # off-stack
    ("reject", "Data Engineer", "", "2-5 years of experience with Spark and Airflow required."),
    # Trainee is full-time unless it says otherwise
    ("keep", "Digital Specialist Engineer (Trainee)", "0-2 years", "2024/2025/2026 graduates. Good programming competency to design and develop solutions. Salary 7 LPA."),
    ("reject", "Software Engineer Trainee", "", "6-month unpaid training period, stipend after 3 months."),
    # From real runs: requirement buried at the end of a long JD
    ("reject", "AI Engineer - AI & Automation", "Not specified",
     "At eBay, we're more than a global ecommerce leader. " + "Our culture values curiosity, inclusion and impact across teams worldwide. " * 60
     + "\nWhat you bring:\n- 4+ years of experience building ML systems in production\n- Python, PyTorch"),
    ("reject", "LLM Ops Engineer", "Associate", "Experience: 1–2 years. Deploy and monitor LLM services with Python and Kubernetes."),
    ("reject", "AI/ML Engineer", "Not specified", "Requirements: 1+ years of experience with Python and ML libraries."),
    ("reject", "Voice AI Engineer — End-to-End Implementation", "3-5 years", "Build voice agents with Python and Twilio."),
    ("reject", "Solutions Engineer - Commercial Accounts", "Not specified", "Partner with account executives to win deals, run product demos, 5+ years pre-sales."),
    ("reject", "Applied AI Engineer", "Not Applicable", "Qualifications: BS and 5-8 years of experience / MS and 3-6 years / PhD and 0-3 years. Build LLM apps."),
    ("keep", "Digital Specialist Engineer (Trainee)", "0-2 years", "2024 / 2025/2026 UG / PG Graduates. Good programming competency to design and develop solutions. Perform validation activities. Experience - Upto 2 years (0-2 years). Salary: 7 Lacs PA"),
    # Skill fit: only roles that use the candidate's stack
    ("reject", "Embedded Software Engineer (C/C++)", "0-1 years", "2026 graduates. Write firmware drivers in C."),
    ("keep", "Python Developer - Staff Augmentation", "", "Fresher python developer for client projects."),
    ("reject", "Java / Spring Boot Developer", "0-1 years", "0 to 1 years of experience in Java development. Build microservices with Spring Boot and Hibernate."),
    ("reject", "Flutter Developer", "0-1 years", "Freshers welcome. Build cross-platform mobile apps in Flutter and Dart."),
    ("reject", "Frontend Developer", "Entry level", "Fresh graduates. Build UIs in React, TypeScript and Tailwind."),
    ("keep", "Software Engineer Trainee", "0-1 years", "Freshers. Full-time graduate program: you will be trained and work on backend services and APIs; any programming language."),
    ("keep", "Backend Developer", "Entry level", "Recent graduates. Build REST APIs in Python (FastAPI) with PostgreSQL and Docker on AWS."),
    ("keep", "Data Scientist", "Entry level", "Freshers welcome. Build ML models in Python with scikit-learn and pandas."),
    ("keep", "Full Stack Developer", "0-1 years", "Freshers. Backend in Python/Django or Node, frontend in React."),
    ("reject", "Associate Software Engineer", "0-1 years", "Freshers. Build features using Spring Boot microservices and React/Angular frontends."),
    ("reject", "Full Stack Developer", "Entry level", "Fresh graduates. Build web apps with TypeScript, React, Next.js and PostgreSQL."),
    ("reject", "Clinical Data Science Programmer", "Entry level", "Freshers. Write SAS and R programs for clinical trial datasets."),
    # Not coding, despite the title
    ("reject", "Wipro is hiring - Backend Process | Fresher Eligible", "0-1 years", "Non-voice backend process for international clients. Data processing, email support, rotational shifts."),
    ("reject", "Walk-In - Annotation Analyst", "0-3 years", "Freshers eligible. Label and annotate images and text data for AI model training."),
    ("reject", "VMC programer", "Entry level", "Operate VMC/CNC machines, write G-code programs, setup tooling on the shop floor."),
    # Non-software engineering
    ("reject", "Electrical Engineer", "0-5 years", "Design power distribution panels and wiring layouts."),
]


def main() -> int:
    jobs = [{"title": t, "experience": e, "description": d, "company": "TestCo", "source": "Test"} for _, t, e, d in CASES]
    errors = llm_filter.judge_jobs(jobs, progress=lambda d, n: print(f"\rfilter {d}/{n}", end="", flush=True))
    errors.update(llm_filter.judge_intern_credit(jobs))
    errors.update(llm_filter.judge_fit(jobs, progress=lambda d, n: print(f"\rskill fit {d}/{n}", end="", flush=True)))
    print()
    failed = 0
    for (expected, title, _, desc), job in zip(CASES, jobs):
        verdict = llm_filter.cached_verdict(job)
        got = verdict["decision"] if verdict else "ERROR"
        ok = got == expected
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  want={expected:6} got={got:6} {title[:38]:38} | {(verdict or {}).get('fit_reason') or (verdict or {}).get('reason', errors.get(llm_filter.job_key(job), ''))[:90]}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} correct")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
