"""Candidate profile built from the resume PDF.

Everything the ranker knows about the candidate comes from here, so updating the
resume PDF updates skills, search text and target roles automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Roles the candidate is targeting. A job title hitting one of these is a strong signal.
TARGET_ROLE_TERMS = [
    "ai", "artificial intelligence", "ml", "machine learning", "genai", "gen ai", "generative ai", "llm", "nlp",
    "rag", "agentic", "python", "backend", "back end", "back-end", "fastapi",
    "data scientist", "data science", "mlops", "applied scientist", "deep learning",
]
# Generic developer titles: fine, but weaker than a direct AI/Python match.
GENERIC_DEV_TERMS = [
    "software engineer", "software developer", "sde", "developer", "programmer",
    "associate engineer", "graduate engineer", "engineer trainee", "trainee engineer",
    "full stack", "full-stack", "forward deployed",
]

# Skills that appear in experience/projects but not in the SKILLS section.
EXTRA_SKILLS = [
    "Machine Learning", "Deep Learning", "Agentic AI", "Qdrant", "SentenceTransformers",
    "Ollama", "REST API", "NLP", "Semantic Search", "AI Agents", "MCP",
]

# Different ways the same skill is written in job descriptions.
ALIASES: dict[str, list[str]] = {
    "machine learning": ["ml", "machine-learning"],
    "genai": ["gen ai", "generative ai", "gen-ai"],
    "llms": ["llm", "large language model", "large language models"],
    "rag": ["retrieval augmented", "retrieval-augmented"],
    "embeddings": ["embedding", "vector embedding"],
    "vector databases": ["vector database", "vector db", "vector store", "pinecone", "weaviate", "faiss", "chroma", "milvus", "pgvector"],
    "postgresql": ["postgres"],
    "scikit-learn": ["sklearn", "scikit learn"],
    "aws": ["amazon web services"],
    "rest api": ["rest apis", "restful", "rest"],
    "agentic ai": ["ai agents", "agentic", "multi-agent", "autogen", "crewai"],
    "deep learning": ["neural network", "neural networks", "tensorflow", "pytorch"],
    "nlp": ["natural language processing"],
    "dsa": ["data structures", "algorithms"],
    "oop": ["object oriented", "object-oriented", "oops"],
    "docker": ["containers", "kubernetes"],
    "prompt engineering": ["prompting", "prompt design"],
}

# Skills worth extra weight because they define the candidate's profile.
CORE_SKILLS = {"python", "llms", "rag", "genai", "machine learning", "fastapi", "langchain", "embeddings", "agentic ai"}


@dataclass
class Profile:
    text: str
    sections: list[str]
    skills: list[str] = field(default_factory=list)


def _resume_path() -> Path:
    from fresherpilot.apply import applicant

    return applicant.resume_path()


def resume_text() -> str:
    path = _resume_path()
    if not path.exists():
        raise FileNotFoundError(f"Resume PDF not found: {path}")
    return _resume_text_at(str(path), path.stat().st_mtime)


@lru_cache(maxsize=16)
def _resume_text_at(path_str: str, _mtime: float) -> str:
    if path_str.lower().endswith(".txt"):  # "about me" text instead of a PDF
        return Path(path_str).read_text(encoding="utf-8")
    import fitz  # PyMuPDF

    with fitz.open(path_str) as document:
        text = "\n".join(page.get_text() for page in document)
    # Strip invisible PDF characters (word joiner, non-breaking space).
    text = text.replace("⁠", "").replace(" ", " ")
    if not text.strip():
        raise RuntimeError("The resume PDF has no extractable text")
    return text


def _section(text: str, start: str, end: str | None) -> str:
    pattern = rf"^{start}\s*$(.*?)" + (rf"^{end}\s*$" if end else r"\Z")
    match = re.search(pattern, text, re.S | re.M)
    return match.group(1).strip() if match else ""


def _parse_skills(skills_block: str) -> list[str]:
    skills: list[str] = []
    for line in skills_block.splitlines():
        if ":" in line:
            line = line.split(":", 1)[1]
        # "Vector Databases (Qdrant, OpenSearch)" -> Vector Databases, Qdrant, OpenSearch
        line = line.replace("(", ",").replace(")", ",")
        for part in line.split(","):
            part = part.strip(" •\t")
            if 1 < len(part) < 40:
                skills.append(part)
    return skills


def load_profile() -> Profile:
    from fresherpilot.core import users

    path = _resume_path()
    p = users.prefs()
    return _load_profile(str(path), path.stat().st_mtime if path.exists() else 0.0, tuple(p.get("skills") or []))


@lru_cache(maxsize=16)
def _load_profile(_path: str, _mtime: float, pref_skills: tuple[str, ...]) -> Profile:
    text = resume_text()
    if pref_skills:  # a user onboarded from any resume layout: skills come from their saved list
        chunks = [text[i:i + 1400] for i in range(0, len(text), 1200)] or [text]
        return Profile(text=text, sections=chunks, skills=list(dict.fromkeys(pref_skills)))
    # Drop contact lines and school marks; they only add noise to the embedding.
    body = text.split("\nPROFILE", 1)[-1]
    body = body.split("\nEDUCATION", 1)[0]

    headline = "\n".join(text.splitlines()[:2])
    profile_block = _section(text, "PROFILE", "SKILLS")
    skills_block = _section(text, "SKILLS", "PROFESSIONAL EXPERIENCE")
    experience = _section(text, "PROFESSIONAL EXPERIENCE", "TECHNICAL PROJECTS")
    projects = _section(text, "TECHNICAL PROJECTS", "TECHNICAL ACHIEVEMENTS")

    sections = [s for s in (
        f"{headline}\n{profile_block}",
        f"Skills: {skills_block}",
        *[chunk for chunk in re.split(r"\n(?=[A-Z][^\n]*\n\d{2}/\d{4})", experience) if chunk.strip()],
        *[chunk for chunk in re.split(r"\n(?=[^\n]*Tech:)", projects) if chunk.strip()],
    ) if s.strip()]

    seen: set[str] = set()
    skills: list[str] = []
    for skill in _parse_skills(skills_block) + EXTRA_SKILLS:
        key = skill.lower()
        if key not in seen:
            seen.add(key)
            skills.append(skill)
    return Profile(text=f"{headline}\n{body}", sections=sections or [text], skills=skills)


def skill_patterns() -> dict[str, re.Pattern[str]]:
    """Map each resume skill to a regex that matches it (and its aliases) as a whole word."""
    patterns: dict[str, re.Pattern[str]] = {}
    for skill in load_profile().skills:
        key = skill.lower()
        variants = [key, *ALIASES.get(key, [])]
        alt = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
        patterns[skill] = re.compile(rf"(?<![a-z0-9]){alt}(?![a-z0-9])", re.I)
    return patterns
