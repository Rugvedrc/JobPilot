"""Rank jobs by embedding similarity to the resume.

Relevance (keep/reject) is decided by llm_filter.py. This module only orders jobs:
each job is split into chunks, compared with each resume section, and scored by how
well it covers the candidate's 3 strongest profile areas. Raw cosine values are
calibrated against anchor JDs (ideal job = 100, unrelated jobs = 0) so the score reads
the same whichever embedding model is used.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np

from fresherpilot.jobs.candidate import load_profile, skill_patterns

ROOT = Path(__file__).resolve().parents[2]
def _vector_path() -> Path:
    from fresherpilot.core import users

    return users.path("embeddings_cache.npz")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# Query instructions each model family was trained with.
QUERY_PREFIXES = {
    "BAAI/bge": "Represent this sentence for searching relevant passages: ",
    "intfloat/e5": "query: ",
    "Qwen/Qwen3-Embedding": "Instruct: Given a section of a candidate's resume, retrieve job descriptions this candidate fits\nQuery: ",
}
DOC_PREFIXES = {"intfloat/e5": "passage: "}

HIGH_ANCHOR = (
    "Associate AI Engineer (Fresher, 0-1 years) | Full-time\n"
    "Build GenAI and RAG applications and Python backend services. Work with LLMs, prompt engineering, "
    "embeddings and vector databases (Qdrant, OpenSearch), LangChain and LangGraph agents. Develop REST APIs "
    "with FastAPI and Pydantic, deploy with Docker on AWS (ECS, S3, Bedrock) and Azure. Apply machine learning "
    "with scikit-learn, Pandas and NumPy. Strong Python, SQL, DSA and OOP fundamentals."
)
LOW_ANCHORS = [
    "Customer Support Executive | Voice Process | BPO\nHandle inbound and outbound calls, resolve customer "
    "queries, meet call handling targets, rotational night shifts, excellent English communication.",
    "Junior Unity Game Developer | C#\nBuild 3D mobile games in Unity, character animation, shaders, level design, "
    "Photoshop and Blender assets, publish to Play Store.",
    "Embedded Firmware Engineer | C, microcontrollers\nWrite firmware for ARM Cortex-M boards, RTOS, SPI/I2C/UART "
    "drivers, PCB bring-up, oscilloscope debugging.",
]

_model = None
_vectors_by_user: dict[str, dict[str, np.ndarray]] = {}
_calibration_by_user: dict[str, tuple[float, float]] = {}
_lock = threading.RLock()  # the page and the background task may embed at the same time


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _prefix(table: dict[str, str]) -> str:
    return next((p for family, p in table.items() if EMBED_MODEL.startswith(family)), "")


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBED_MODEL, device="cpu")
        _model.max_seq_length = min(_model.max_seq_length or 512, 512)
    return _model


def _load_vectors() -> dict[str, np.ndarray]:
    path = _vector_path()
    vectors = _vectors_by_user.get(str(path))
    if vectors is None:
        vectors = {}
        if path.exists():
            try:
                data = np.load(path)
                vectors = dict(zip(data["keys"].tolist(), data["vecs"]))
            except Exception:  # noqa: BLE001 — a corrupt cache is just rebuilt
                vectors = {}
        _vectors_by_user[str(path)] = vectors
    return vectors


def _encode(texts: list[str], *, query: bool) -> np.ndarray:
    # Fast path without the lock: everything already embedded (the page must not wait
    # while the background task embeds a big batch of new jobs).
    vectors = _load_vectors()
    prefix = _prefix(QUERY_PREFIXES if query else DOC_PREFIXES)
    keys = [hashlib.sha256(f"{EMBED_MODEL}\0{prefix}{t}".encode("utf-8")).hexdigest() for t in texts]
    if all(k in vectors for k in keys):
        return np.stack([vectors[k] for k in keys])
    with _lock:
        return _encode_locked(texts, query=query)


def _encode_locked(texts: list[str], *, query: bool) -> np.ndarray:
    vectors = _load_vectors()
    prefix = _prefix(QUERY_PREFIXES if query else DOC_PREFIXES)

    def key(text: str) -> str:
        return hashlib.sha256(f"{EMBED_MODEL}\0{prefix}{text}".encode("utf-8")).hexdigest()

    missing = list(dict.fromkeys(t for t in texts if key(t) not in vectors))
    if missing:
        encoded = _get_model().encode(
            [prefix + t for t in missing], normalize_embeddings=True, convert_to_numpy=True,
            show_progress_bar=False, batch_size=16,
        )
        for text, vec in zip(missing, encoded):
            vectors[key(text)] = vec.astype(np.float32)
        # Only this model's vectors are kept, so all rows have one size.
        keys = [k for k, v in vectors.items() if v.shape == encoded[0].shape]
        np.savez(_vector_path(), keys=np.array(keys), vecs=np.stack([vectors[k] for k in keys]))
    return np.stack([vectors[key(t)] for t in texts])


def _chunks(text: str, size: int = 1400, overlap: int = 200) -> list[str]:
    """The model reads ~512 tokens, so long JDs are split and every part is scored."""
    text = text.strip()
    if len(text) <= size:
        return [text]
    step = size - overlap
    return [text[i : i + size] for i in range(0, len(text), step) if text[i : i + size].strip()]


def _job_text(job: dict[str, Any]) -> str:
    skills = job.get("skills") or []
    skills_text = ", ".join(map(str, skills)) if isinstance(skills, list) else str(skills)
    head = " | ".join(str(v) for v in (job.get("title"), job.get("company"), job.get("experience")) if v)
    return "\n".join(p for p in (head, f"Skills: {skills_text}" if skills_text else "", job.get("description") or "") if p)


def _raw_similarity(texts: list[str]) -> list[float]:
    sections = load_profile().sections
    job_chunks = [_chunks(t) for t in texts]
    flat = [c for chunks in job_chunks for c in chunks]
    matrix = _encode(flat, query=False) @ _encode(sections, query=True).T  # chunks x sections
    scores, offset = [], 0
    for chunks in job_chunks:
        best_per_section = matrix[offset : offset + len(chunks)].max(axis=0)
        offset += len(chunks)
        scores.append(float(np.sort(best_per_section)[-3:].mean()))
    return scores


def _calibrate() -> tuple[float, float]:
    key = str(_vector_path())
    if key not in _calibration_by_user:
        raw = _raw_similarity([HIGH_ANCHOR, *LOW_ANCHORS])
        _calibration_by_user[key] = (float(np.mean(raw[1:])), raw[0])
    return _calibration_by_user[key]


def quick_scores(jobs: list[dict[str, Any]]) -> list[float]:
    """Cheap similarity from title + opening lines (one short text per job). Used only to
    decide which unchecked jobs the AI looks at first."""
    if not jobs:
        return []
    texts = [f"{j.get('title') or ''} | {(j.get('description') or '')[:400]}" for j in jobs]
    matrix = _encode(texts, query=False) @ _encode(load_profile().sections, query=True).T
    return [float(row.max()) for row in matrix]


def matched_skills(job: dict[str, Any]) -> list[str]:
    """Resume skills mentioned in the job. Display only, not part of the score."""
    haystack = f"{job.get('title') or ''}\n{' '.join(map(str, job.get('skills') or []))}\n{job.get('description') or ''}"
    return [skill for skill, rx in skill_patterns().items() if rx.search(haystack)]


def score_jobs(jobs: list[dict[str, Any]]) -> None:
    """Add score (0-100), similarity and matched_skills to each job, in place."""
    if not jobs:
        return
    raw = _raw_similarity([_job_text(j) for j in jobs])
    lo, hi = _calibrate()
    for job, sim in zip(jobs, raw):
        job["similarity"] = round(sim, 4)
        job["score"] = round(max(0.0, min(1.0, (sim - lo) / max(hi - lo, 1e-6))) * 100)
        job["matched_skills"] = matched_skills(job)
