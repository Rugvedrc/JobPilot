const $ = (id) => document.getElementById(id);

// Logged out (or the session expired): go to the sign-in page.
const _rawFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _rawFetch(...args);
  if (res.status === 401 && !String(args[0]).includes("/api/auth")) location.href = "/login";
  return res;
};
const fetchBtn = $("fetchBtn");
const statusEl = $("status");
const listEl = $("list");
const detailEl = $("detail");

const CATEGORY_LABELS = {
  internship: "Internship",
  not_full_time: "Not full-time",
  too_senior: "Too senior",
  needs_experience: "Needs 1+ years",
  non_developer: "Not a developer role",
  non_software_engineering: "Non-software engineering",
  off_stack: "Different tech stack",
  other: "Not relevant",
};
const ROLE_LABELS = { recruiter: "Recruiter / HR", leader: "Hiring leader", employee: "Works there" };

const ICON = {
  external: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
  mail: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
  phone: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/></svg>',
  person: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
  copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/></svg>',
  globe: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5-7 7 7 7"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>',
};

const state = {
  data: null,
  sort: "match",
  search: "",
  days: "",
  sources: new Set(), // empty = all
  showRejected: false,
  showUnchecked: true, // show jobs as soon as they're fetched, marked 'Pending AI check'
  hideVisited: false,
  selected: null,
  contacts: {}, // uid -> {loading, data}
  kits: {}, // uid -> {loading, data, error}
  hideApplied: false,
};

// Remember UI preferences per browser; ignore storage failures.
const PREF_KEY = "jobfinder.prefs";
try {
  const saved = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
  if (saved.sort) state.sort = saved.sort;
  if (saved.days !== undefined) state.days = saved.days;
  if (typeof saved.showUnchecked === "boolean") state.showUnchecked = saved.showUnchecked;
  state.hideVisited = !!saved.hideVisited;
} catch {}
function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({ sort: state.sort, days: state.days, hideVisited: state.hideVisited, showUnchecked: state.showUnchecked }));
  } catch {}
}

// ---------------------------------------------------------------- helpers

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "");

function formatWhen(iso) {
  if (!iso) return "–";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "–";
  const mins = Math.round((Date.now() - date.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  return date.toLocaleDateString();
}

function postedLabel(job) {
  const d = job.posted_days;
  if (d === null || d === undefined) return job.posted || "";
  if (d === 0) return "Today";
  if (d === 1) return "1 day ago";
  return `${d} days ago`;
}

function tier(score) {
  if (score >= 65) return "strong";
  if (score >= 40) return "good";
  return "weak";
}

const STATUS_LABELS = { applied: "Applied", interview: "Interview", offer: "Offer", rejected: "Not selected" };

function statusSelect(job) {
  const cur = job.application?.status || "";
  const opts = [["", "Not applied"], ...Object.entries(STATUS_LABELS)]
    .map(([v, l]) => `<option value="${v}" ${v === cur ? "selected" : ""}>${l}</option>`)
    .join("");
  return `<select class="status-select" data-status="${escapeHtml(job.uid)}" aria-label="Application status">${opts}</select>`;
}

const KIT_PARTS = [
  ["cover_letter", "Cover letter"],
  ["why_hire_me", "Why should we hire you?"],
  ["about_me", "Tell us about yourself"],
  ["relevant_project", "Most relevant project"],
  ["why_this_company", "Why this company / role?"],
  ["linkedin_note", "LinkedIn connection note"],
  ["email_subject", "Cold email: subject"],
  ["email_body", "Cold email: body"],
];

function kitHtml(job) {
  if (job.status === "reject") return "";
  const k = state.kits[job.uid] || {};
  const btn = `<button type="button" class="link-btn" data-kit="${escapeHtml(job.uid)}" ${k.loading ? "disabled" : ""}>${ICON.refresh} ${k.data ? "Regenerate" : "Generate"}</button>`;
  let body;
  if (k.loading) body = `<p class="muted loading">Writing your cover letter and answers for this job… (10–30 seconds)</p>`;
  else if (k.error) body = `<p class="notice warn">${escapeHtml(k.error)}</p>`;
  else if (!k.data) body = `<p class="muted">Cover letter, answers to common questions ("why should we hire you", "about yourself"…) and outreach messages, written from your resume for this job. The browser extension uses the same profile to fill application forms.</p>`;
  else
    body = KIT_PARTS.filter(([key]) => k.data[key])
      .map(([key, title]) => `<details class="kit-part" ${key === "cover_letter" ? "open" : ""}>
          <summary>${title} <span class="muted small">${k.data[key].length} chars</span></summary>
          <div class="kit-text">${escapeHtml(k.data[key])}</div>
          <button type="button" class="btn small" data-copy="${escapeHtml(k.data[key])}">${ICON.copy} Copy</button>
        </details>`)
      .join("");
  return `<section class="card"><div class="card-head"><h3>Apply kit</h3>${btn}</div>${body}</section>`;
}

async function loadKit(uid, generate) {
  const job = findJob(uid);
  if (!job || job.status === "reject") return;
  const cur = state.kits[uid];
  if (cur && (cur.loading || (cur.data && !generate))) return;
  state.kits[uid] = { ...(cur || {}), loading: !!generate, error: null };
  if (state.selected === uid) renderDetail();
  try {
    const q = generate ? (cur?.data ? "&refresh=true" : "") : "&generate=false";
    const res = await fetch(`/api/applykit?uid=${encodeURIComponent(uid)}${q}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Couldn't write the apply kit");
    state.kits[uid] = { loading: false, data: data.kit };
  } catch (err) {
    state.kits[uid] = { loading: false, data: cur?.data, error: err.message };
  }
  if (state.selected === uid) renderDetail();
}

async function setStatus(uid, status) {
  const job = findJob(uid);
  if (job) {
    job.application = status ? { ...(job.application || {}), status } : null;
    if (status) job.visited_at = job.visited_at || new Date().toISOString();
    renderList();
    renderDetail();
  }
  try {
    const res = await fetch("/api/application", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid, status: status || null }),
    });
    if (!res.ok) throw new Error();
    if (typeof refreshDashboard === "function") refreshDashboard();
  } catch {
    statusEl.textContent = "Could not save the application status. Is the server running?";
  }
}

function instructionsHtml(job) {
  const ins = job.apply_instructions;
  if (!ins) return "";
  const links = (ins.links || [])
    .map((u) => {
      const href = /^https?:\/\//i.test(u) ? u : `https://${u}`;
      return `<li>${ICON.external}<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(u)}</a></li>`;
    })
    .join("");
  const emails = (ins.emails || [])
    .map((e) => {
      const subject = ins.subject_line ? `?subject=${encodeURIComponent(ins.subject_line)}` : "";
      return `<li>${ICON.mail}<a href="mailto:${escapeHtml(e)}${subject}">${escapeHtml(e)}</a>${copyBtn(e)}</li>`;
    })
    .join("");
  return `<div class="howto-banner" role="note">
      <strong>⚠ Special application instructions</strong>
      <p>${escapeHtml(ins.summary || "")}</p>
      ${ins.subject_line ? `<p class="small">Required subject: <code>${escapeHtml(ins.subject_line)}</code> ${copyBtn(ins.subject_line)}</p>` : ""}
      ${links || emails ? `<ul class="contact-list">${links}${emails}</ul>` : ""}
      <p class="small muted">Easy Apply alone may not be enough. Check the description below for details.</p>
    </div>`;
}

function fitChip(job) {
  if (job.ai_fit === "strong") return `<span class="chip hit" title="${escapeHtml(job.ai_fit_reason || "")}">Strong fit</span>`;
  if (job.ai_fit === "partial") return `<span class="chip" title="${escapeHtml(job.ai_fit_reason || "")}">Partial fit</span>`;
  return "";
}

function yearsChip(job) {
  if (job.ai_category === "stretch")
    return `<span class="chip warn" title="${escapeHtml(job.ai_evidence || "Asks for about 1 year; freshers often still get calls")}">Stretch · asks 1 yr</span>`;
  if (job.ai_freshers) return `<span class="chip hit">Freshers welcome</span>`;
  if (job.ai_intern_credit) return `<span class="chip warn" title="${escapeHtml(job.ai_intern_credit)}">1 yr · internships count</span>`;
  if (typeof job.ai_years === "number") {
    const yrs = job.ai_years < 1 ? `${Math.round(job.ai_years * 12)} months` : `${job.ai_years}+ yr`;
    return `<span class="chip ${job.ai_years >= 1 ? "bad" : "warn"}" title="${escapeHtml(job.ai_evidence || "")}">Needs ${yrs}</span>`;
  }
  return job.status === "unchecked" ? "" : `<span class="chip">No experience stated</span>`;
}

const employer = (job) => job.employer || "Company not disclosed";

function jobs() {
  return state.data?.jobs || [];
}
function findJob(uid) {
  return jobs().find((j) => j.uid === uid);
}

// ---------------------------------------------------------------- list

function visibleJobs() {
  const q = state.search.trim().toLowerCase();
  const maxDays = state.days ? Number(state.days) : null;
  const filtered = jobs().filter((job) => {
    if (job.status === "reject" && !state.showRejected) return false;
    if (job.status === "unchecked" && !state.showUnchecked) return false;
    if (state.hideVisited && job.visited_at) return false;
    if (state.hideApplied && job.application) return false;
    if (state.sources.size && !state.sources.has(job.source)) return false;
    if (maxDays !== null && (job.posted_days ?? 999) > maxDays) return false;
    if (q) {
      const hay = [job.title, job.employer, job.company, job.location, ...(job.skills || []), ...(job.matched_skills || [])]
        .join(" ")
        .toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  const days = (j) => j.posted_days ?? 999;
  // AI-approved first, then pending AI check, then (if shown) filtered-out.
  const rank = (j) => (j.status === "keep" ? 0 : j.status === "unchecked" ? 1 : 2);
  const rejected = rank;
  filtered.sort((a, b) => {
    if (rejected(a) !== rejected(b)) return rejected(a) - rejected(b);
    if (state.sort === "fresh") return days(a) - days(b) || (b.score ?? 0) - (a.score ?? 0);
    return (b.score ?? 0) - (a.score ?? 0) || days(a) - days(b);
  });
  return filtered;
}

function contactBadges(job) {
  const s = job.contacts_summary;
  if (!s) return "";
  const parts = [];
  if (s.people) parts.push(`<span title="${s.people} people found">${ICON.person}${s.people}</span>`);
  if (s.emails) parts.push(`<span title="${s.emails} emails found">${ICON.mail}${s.emails}</span>`);
  if (s.phones) parts.push(`<span title="${s.phones} phone numbers found">${ICON.phone}${s.phones}</span>`);
  return parts.length ? `<span class="contact-badges">${parts.join("")}</span>` : "";
}

function itemHtml(job) {
  const isRejected = job.status === "reject";
  const isVisited = !!job.visited_at;
  const score = isRejected
    ? `<span class="score rejected" title="Filtered out">✕</span>`
    : job.score === null || job.score === undefined
      ? `<span class="score weak">–</span>`
      : `<span class="score ${tier(job.score)}" title="Similarity to your resume">${job.score}</span>`;
  const cls = ["job", isRejected && "is-rejected", isVisited && "is-visited", state.selected === job.uid && "is-selected"]
    .filter(Boolean)
    .join(" ");
  return `
    <li class="${cls}" data-uid="${escapeHtml(job.uid)}">
      ${score}
      <div class="job-main">
        <p class="job-title">${!isVisited && !isRejected ? '<span class="dot" title="New"></span>' : ""}${escapeHtml(job.title)}</p>
        <p class="job-meta">${escapeHtml(employer(job))}${job.location ? " · " + escapeHtml(job.location) : ""}</p>
        <div class="chips small">
          <span class="chip source">${escapeHtml(job.source)}</span>
          ${job.apply_instructions ? `<span class="chip howto" title="${escapeHtml(job.apply_instructions.summary || "")}">⚠ How to apply</span>` : ""}
          ${job.application && job.application.status !== "applied" ? `<span class="chip applied">${escapeHtml(STATUS_LABELS[job.application.status] || "Applied")}</span>` : ""}
          ${job.outreach ? `<span class="chip sent" title="Messages sent">✓ Contacted</span>` : ""}
          ${postedLabel(job) ? `<span class="chip">${escapeHtml(postedLabel(job))}</span>` : ""}
          ${job.status === "unchecked" ? `<span class="chip warn">Pending AI check</span>` : isRejected ? `<span class="chip bad">${escapeHtml(CATEGORY_LABELS[job.ai_category] || "Filtered")}</span>` : fitChip(job) + yearsChip(job)}
          ${contactBadges(job)}
        </div>
      </div>
      <div class="job-side">
        <label class="visit" title="Mark as visited"><input type="checkbox" data-visit="${escapeHtml(job.uid)}" ${isVisited ? "checked" : ""} /><span>Visited</span></label>
        ${isRejected ? "" : `<button type="button" class="quick-apply ${job.application ? "on" : ""}" data-quickapply="${escapeHtml(job.uid)}" title="${job.application ? "Click to undo" : "Mark as applied (counts on the dashboard)"}">${job.application ? "✓ Applied" : "Mark applied"}</button>`}
      </div>
    </li>`;
}

function renderList() {
  const shown = visibleJobs();
  $("resultCount").textContent = `${shown.length} job${shown.length === 1 ? "" : "s"}`;
  if (!jobs().length) {
    listEl.innerHTML = `<li class="empty">No jobs yet. Click <strong>Fetch new jobs</strong>.</li>`;
    return;
  }
  const pending = jobs().filter((j) => j.status === "unchecked").length;
  if (!shown.length && pending && !state.showUnchecked) {
    listEl.innerHTML = `<li class="empty">${pending} job${pending === 1 ? " is" : "s are"} waiting for the AI check${
      state.setupMissing ? ` (it starts once you <a href="/onboarding">finish setup</a>)` : ""}.<br>
      <button type="button" class="btn small" id="showPendingBtn">Show them now</button></li>`;
    $("showPendingBtn").onclick = () => { state.showUnchecked = true; savePrefs(); syncControls(); renderList(); };
    return;
  }
  listEl.innerHTML = shown.length
    ? shown.map(itemHtml).join("")
    : `<li class="empty">Nothing matches these filters.${state.hideVisited ? " Try turning off “Hide visited”." : ""}</li>`;
}

function renderSources() {
  const counts = {};
  for (const job of jobs()) if (job.status !== "reject") counts[job.source] = (counts[job.source] || 0) + 1;
  $("sources").innerHTML = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([src, n]) => `<button type="button" class="chip-btn ${state.sources.has(src) ? "on" : ""}" data-src="${escapeHtml(src)}">${escapeHtml(src)} <span>${n}</span></button>`)
    .join("");
}

// ---------------------------------------------------------------- detail

function copyBtn(value) {
  return `<button type="button" class="icon-btn" data-copy="${escapeHtml(value)}" title="Copy">${ICON.copy}</button>`;
}

function contactsHtml(job) {
  if (job.status === "reject") return "";
  const entry = state.contacts[job.uid];
  const head = `<div class="card-head"><h3>Reach out</h3>
    <button type="button" class="link-btn" data-refresh="${escapeHtml(job.uid)}" ${entry?.loading ? "disabled" : ""}>${ICON.refresh} Search again</button></div>`;
  if (!entry || entry.loading) {
    return `<section class="card">${head}<p class="muted loading">Searching for recruiters and contacts at ${escapeHtml(employer(job))}… (about 20 seconds)</p></section>`;
  }
  const d = entry.data || {};
  const jd = d.from_description || { emails: [], phones: [], linkedin: [] };
  const c = d.company || {};
  const blocks = [];

  const jdItems = [
    ...jd.emails.map((e) => emailRow(job, e, "listed")),
    ...jd.phones.map((p) => phoneRow(job, p, "listed")),
    ...jd.linkedin.map((u) => `<li>${ICON.person}<a href="${escapeHtml(u)}" target="_blank" rel="noopener">${escapeHtml(u.replace(/^https?:\/\/(www\.)?/, ""))}</a></li>`),
  ];
  if (jdItems.length) {
    blocks.push(`<div class="contact-group highlight"><h4>Listed in the job post</h4><ul class="contact-list">${jdItems.join("")}</ul></div>`);
  }

  if ((c.people || []).length) {
    blocks.push(`<div class="contact-group"><h4>People at ${escapeHtml(c.company || employer(job))}</h4><ul class="people">
      ${c.people
        .map(
          (p) => `<li class="person">
            <span class="avatar">${escapeHtml((p.name || "?").trim().charAt(0).toUpperCase())}</span>
            <div class="person-main">
              <p class="person-name">${escapeHtml(p.name)} <span class="role-tag ${escapeHtml(p.role)}">${escapeHtml(ROLE_LABELS[p.role] || p.role)}</span></p>
              <p class="person-headline">${escapeHtml(p.headline)}</p>
            </div>
            <a class="btn small" href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">LinkedIn ${ICON.external}</a>
          </li>`
        )
        .join("")}
    </ul></div>`);
  }

  const reach = [
    ...(c.emails || []).map((e) => emailRow(job, e.value, "public", e.source)),
    ...(c.phones || []).map((p) => phoneRow(job, p.value, "public", p.source)),
  ];
  const g = d.guessed || {};
  const website = c.website || (g.domain ? `https://${g.domain}` : "");
  if (website) reach.unshift(`<li>${ICON.globe}<a href="${escapeHtml(safeUrl(website))}" target="_blank" rel="noopener">${escapeHtml(website.replace(/^https?:\/\//, ""))}</a></li>`);
  if (reach.length) blocks.push(`<div class="contact-group"><h4>Company contacts</h4><ul class="contact-list">${reach.join("")}</ul></div>`);

  // Guessed inboxes at the company's domain (used when nothing public is found, or as extra options).
  if (g.domain && g.mx && ((g.emails || []).length || (g.people || []).length)) {
    const generic = (g.emails || [])
      .map((e) => emailRow(job, e, "guessed"))
      .join("");
    const people = (g.people || [])
      .map((p) => `<li>${ICON.person}<span class="guess-name">${escapeHtml(p.name)}:</span> ${p.emails
        .map((e) => `<span>${escapeHtml(e)}</span>${copyBtn(e)}${gmailBtn(job, e, p.name)}`)
        .join(" ")}</li>`)
      .join("");
    blocks.push(`<div class="contact-group guessed">
      <h4>Likely email addresses <span class="badge guess">⚠ Guessed · needs verification</span></h4>
      <ul class="contact-list">${generic}${people}</ul>
      <p class="muted small">${escapeHtml(g.domain)} (found via ${escapeHtml(g.source)}) accepts email, but these exact inboxes aren't verified. careers@ and hr@ are the most common.</p>
    </div>`);
  } else if (g.domain && !g.mx) {
    blocks.push(`<p class="muted small">${escapeHtml(g.domain)} (found via ${escapeHtml(g.source)}) doesn't receive email, so no addresses were guessed.</p>`);
  }

  let message = "";
  if (d.status === "no_company") {
    message = `<p class="notice">${escapeHtml(job.source)} hides the employer, and a web search for this posting didn't reveal who posted it${d.search_error ? " (search was blocked)" : ""}. Any contacts written in the job post are shown below.</p>`;
  } else if (d.error) {
    message = `<p class="notice warn">${escapeHtml(d.error)}</p>`;
  } else {
    if (d.search_error) message += `<p class="notice warn">${escapeHtml(d.search_error)}</p>`;
    if (!blocks.length) message += `<p class="notice">No contacts found for ${escapeHtml(employer(job))}, and its email domain couldn't be found either.</p>`;
  }
  if (d.employer_evidence && d.employer) {
    message = `<p class="notice">${escapeHtml(job.source)} hid the employer. Identified as <strong>${escapeHtml(d.employer)}</strong> from <a href="${escapeHtml(safeUrl(d.employer_evidence))}" target="_blank" rel="noopener">this listing</a>.</p>` + message;
  }
  const manual = (c.searches || []).length
    ? `<p class="muted small">Search yourself: ${c.searches.map((u, i) => `<a href="${escapeHtml(safeUrl(u))}" target="_blank" rel="noopener">${i ? "leaders" : "recruiters"}</a>`).join(" · ")}</p>`
    : "";
  return `<section class="card">${head}${message}${draftHtml(job)}${blocks.join("")}
    ${(c.people || []).length + (c.emails || []).length + (c.phones || []).length ? `<p class="muted small">People and contacts above were found in public ${escapeHtml(c.provider || d.provider || "web")} search results. Check the person still works there before reaching out.</p>` : ""}${manual}</section>`;
}

function renderDetail() {
  const job = state.selected && findJob(state.selected);
  document.body.classList.toggle("show-detail", !!job);
  if (!job) {
    detailEl.innerHTML = `<div class="placeholder"><p>Select a job to see details and people to reach out to.</p></div>`;
    return;
  }
  const url = safeUrl(job.url);
  const isRejected = job.status === "reject";
  const meta = [job.location, job.experience, job.salary, postedLabel(job)]
    .filter((x) => x && x !== "Not disclosed" && x !== "Not specified")
    .map(escapeHtml)
    .join(" · ");
  const verdict = job.status === "unchecked"
    ? `<p class="notice warn">Not checked by AI yet.</p>`
    : `<p class="verdict ${isRejected ? "bad" : ""}">${isRejected ? `<strong>${escapeHtml(CATEGORY_LABELS[job.ai_category] || "Filtered out")}.</strong> ` : ""}${escapeHtml(job.ai_reason || "")}</p>
       ${job.ai_fit_reason ? `<p class="verdict"><strong>Skill fit:</strong> ${escapeHtml(job.ai_fit_reason)}</p>` : ""}
       ${job.ai_evidence ? `<p class="quote">“${escapeHtml(job.ai_evidence)}”</p>` : ""}`;
  const skills = (job.matched_skills || []).map((s) => `<span class="chip hit">${escapeHtml(s)}</span>`).join("");

  detailEl.innerHTML = `
    <button type="button" class="back-btn" id="backBtn">${ICON.back} All jobs</button>
    <header class="detail-head">
      <div>
        <p class="detail-company">${escapeHtml(employer(job))}</p>
        <h2>${escapeHtml(job.title)}</h2>
        <p class="job-meta">${meta}</p>
      </div>
      ${!isRejected && job.score !== null && job.score !== undefined ? `<span class="score big ${tier(job.score)}" title="Similarity to your resume">${job.score}<small>match</small></span>` : ""}
    </header>
    ${instructionsHtml(job)}
    ${typeof outreachBar === "function" ? outreachBar(job) : ""}
    <div class="actions">
      ${url ? `<a class="btn primary" href="${escapeHtml(url)}" target="_blank" rel="noopener" data-open="${escapeHtml(job.uid)}">Open on ${escapeHtml(job.source)} ${ICON.external}</a>` : ""}
      ${url && job.status === "keep" && !job.application ? `<button type="button" class="btn" data-autoapply="${escapeHtml(job.uid)}" title="Applies in your browser using your logins">Auto-apply</button>` : ""}
      <label class="visit big"><input type="checkbox" data-visit="${escapeHtml(job.uid)}" ${job.visited_at ? "checked" : ""} /><span>Visited</span></label>
      ${job.status === "reject" ? "" : statusSelect(job)}
    </div>
    <section class="card">
      <div class="chips">
        <span class="chip source">${escapeHtml(job.source)}</span>
        ${fitChip(job)}
        ${yearsChip(job)}
        ${job.ai_employment ? `<span class="chip">${escapeHtml(job.ai_employment.replace(/_/g, " "))}</span>` : ""}
        ${(job.ai_stack || []).map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join("")}
      </div>
      ${verdict}
    </section>
    ${kitHtml(job)}
    ${contactsHtml(job)}
    ${skills ? `<section class="card"><h3>Your skills in this job</h3><div class="chips">${skills}</div></section>` : ""}
    <section class="card"><h3>Description</h3><div class="description">${escapeHtml(job.description || "No description.")}</div></section>`;
}

async function loadContacts(uid, refresh = false) {
  const job = findJob(uid);
  if (!job || job.status === "reject") return;
  if (state.contacts[uid] && !refresh && !state.contacts[uid].loading) return;
  state.contacts[uid] = { loading: true };
  if (state.selected === uid) renderDetail();
  try {
    const res = await fetch(`/api/contacts?uid=${encodeURIComponent(uid)}${refresh ? "&refresh=true" : ""}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Search failed");
    state.contacts[uid] = { loading: false, data };
    // A hidden employer may have just been identified from search.
    if (data.employer && !job.employer) {
      job.employer = data.employer;
      job.employer_evidence = data.employer_evidence;
      renderList();
    }
    // Update the list badges for every job at this company.
    const c = data.company;
    if (c) {
      const extra = data.from_description || { emails: [], phones: [] };
      for (const j of jobs()) {
        if (j.employer && j.employer === job.employer && j.status !== "reject") {
          j.contacts_summary = { people: c.people.length, emails: c.emails.length + extra.emails.length, phones: c.phones.length + extra.phones.length, searched: true };
        }
      }
      renderList();
    }
  } catch (err) {
    state.contacts[uid] = { loading: false, data: { status: "search_failed", error: err.message } };
  }
  if (state.selected === uid) renderDetail();
}

function select(uid) {
  state.selected = uid;
  renderList();
  renderDetail();
  detailEl.scrollTop = 0;
  if (window.matchMedia("(max-width: 900px)").matches) window.scrollTo(0, 0);
  loadContacts(uid);
  loadKit(uid, false); // show a saved kit if there is one
}

// ---------------------------------------------------------------- top-level render

function render(data) {
  state.data = data;
  $("statKept").textContent = data.total_kept ?? 0;
  $("statNew").textContent = data.total_new ?? 0;
  $("statRejected").textContent = data.total_rejected ?? 0;
  $("statWhen").textContent = formatWhen(data.fetched_at);
  $("tabNew").hidden = !data.total_new;
  $("tabNew").textContent = data.total_new || "";
  $("toolbar").hidden = !jobs().length;

  const reasons = Object.entries(data.rejected_by_category || {})
    .map(([c, n]) => `${CATEGORY_LABELS[c] || c}: ${n}`)
    .join(", ");
  $("rejectedCount").textContent = data.total_rejected ? `(${data.total_rejected})` : "";
  $("rejectedCount").title = reasons;

  const messages = Object.entries(data.errors || {}).map(([actor, msg]) => `${actor}: ${msg}`);
  // (the setup banner already says what's missing, so its AI-check message isn't repeated here)
  for (const e of [data.task?.fetch?.error, state.setupMissing ? null : data.task?.check?.error]) if (e) messages.push(e);
  $("errors").hidden = !messages.length;
  $("errors").textContent = messages.join(" · ");

  if (state.selected && !findJob(state.selected)) state.selected = null;
  const rep = data.fetch_report || {};
  const parts = Object.entries(rep).map(([src, r]) => {
    const cost = typeof r.cost_usd === "number" && r.cost_usd > 0.004 ? `$${r.cost_usd.toFixed(2)}` : r.free || r.cost_usd === 0 ? "free" : "";
    return `${src[0].toUpperCase() + src.slice(1)} ${cost} · ${r.new} new` + (r.already_had ? `, ${r.already_had} seen before` : "") + (r.duplicates ? `, ${r.duplicates} duplicates` : "");
  });
  const live = data.task?.live?.boards || {};
  const liveParts = Object.entries(live).map(([b, r]) => `${BOARD_NAMES[b] || b} free · ${r.added || 0} new`);
  if (liveParts.length) parts.unshift(...liveParts);
  if ((data.no_apify_credit || []).length) parts.push(`${data.no_apify_credit.join(", ")} skipped (no Apify key or no credit left today)`);
  $("fetchReport").textContent = parts.length ? `Last fetch: ${parts.join(" | ")}` : "";

  renderSources();
  syncControls();
  renderList();
  renderDetail();
}

function syncControls() {
  document.querySelectorAll(".seg-btn").forEach((btn) => btn.classList.toggle("on", btn.dataset.sort === state.sort));
  $("days").value = state.days;
  $("showRejected").checked = state.showRejected;
  $("hideVisited").checked = state.hideVisited;
  $("showUnchecked").checked = state.showUnchecked;
}

// ---------------------------------------------------------------- visited

async function setVisited(uids, value) {
  const stamp = value ? new Date().toISOString() : null;
  const wanted = new Set(uids);
  for (const job of jobs()) if (wanted.has(job.uid)) job.visited_at = stamp;
  state.data.total_new = jobs().filter((j) => j.status !== "reject" && !j.visited_at).length;
  $("statNew").textContent = state.data.total_new;
  $("tabNew").hidden = !state.data.total_new;
  $("tabNew").textContent = state.data.total_new || "";
  renderList();
  if (wanted.has(state.selected)) renderDetail();
  try {
    const res = await fetch("/api/visited", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uids, visited: value }),
    });
    if (!res.ok) throw new Error();
  } catch {
    statusEl.textContent = "Could not save visited state. Is the server running?";
  }
}

// ---------------------------------------------------------------- background task polling

let polling = false;

// Fetching and the AI check are independent; each has its own progress text.
const BOARD_NAMES = { linkedin: "LinkedIn", indeed: "Indeed", internshala: "Internshala", wellfound: "Wellfound" };

// Live boards (read all the time; the Fetch button starts a full round) + the regular fetch.
function liveBusy(st) {
  const live = st?.live || {};
  return (live.busy || []).map((b) => {
    const r = b === "linkedin" && live.linkedin_round;
    return r ? `LinkedIn ${r.done}/${r.total} searches` : BOARD_NAMES[b] || b;
  });
}

function describeFetch(st) {
  const f = st?.fetch;
  const parts = [];
  if (f?.running) {
    const waiting = (f.waiting || []).join(", ");
    parts.push(f.total ? `${f.done}/${f.total} boards done${waiting ? ` (waiting for ${waiting})` : ""}` : "starting…");
  }
  parts.push(...liveBusy(st));
  return parts.length ? `Fetching: ${parts.join(" · ")}` : "";
}

function describeCheck(c) {
  if (!c || !c.running) return "";
  if (c.stage === "paused_for_autoapply") return "AI check paused while auto-apply runs";
  if (c.stage === "ranking" || c.stage === "starting") return "Preparing the AI check…";
  return c.total ? `AI checking jobs: ${c.done}/${c.total}` : "AI checking jobs…";
}

function showProgress(st) {
  const busy = st.fetch?.running ? st.fetch : liveBusy(st).length ? { total: 0 } : st.check?.running ? st.check : null;
  $("progress").hidden = !busy;
  if (!busy) return;
  const pct = busy.total ? (busy.done / busy.total) * 100 : null;
  $("progress").classList.toggle("indeterminate", pct === null);
  $("progressFill").style.width = pct === null ? "" : `${pct}%`;
}

function setFetchButton(st) {
  const busy = !!(st?.fetch?.running || liveBusy(st).length);
  fetchBtn.classList.toggle("busy", busy);
  fetchBtn.setAttribute("aria-busy", busy ? "true" : "false");
  fetchBtn.textContent = busy ? "Fetching…" : "Fetch new jobs";
}

let loadInFlight = null;
async function showUsage() {
  try {
    const u = await (await fetch("/api/usage")).json();
    const parts = Object.entries(u.models || {}).map(([m, n]) => `${m.split("/").pop()} ${Math.round(n / 1000)}k/${u.daily_limit_per_model / 1000}k`);
    $("usage").textContent = parts.length ? `AI tokens today: ${parts.join(" · ")}` : "";
  } catch {}
}
setInterval(showUsage, 30000);
showUsage();

async function loadJobs() {
  // One request at a time: after a big fetch the first load can take a while.
  if (loadInFlight) return loadInFlight;
  loadInFlight = (async () => {
    const res = await fetch("/api/jobs");
    if (!res.ok) throw new Error(`Server error ${res.status}`);
    const data = await res.json();
    if (data.fetched_at) render(data);
    return data;
  })();
  try {
    return await loadInFlight;
  } finally {
    loadInFlight = null;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function summaryText(data) {
  if (!data.fetched_at) return "No jobs yet. Click Fetch new jobs.";
  const waiting = data.total_unchecked ? ` ${data.total_unchecked} more waiting for the AI check.` : "";
  return `${data.total_new} new relevant job${data.total_new === 1 ? "" : "s"}.${waiting} Click a job to see who to contact.`;
}

async function pollTask() {
  if (polling) return;
  polling = true;
  let lastDone = -1;
  let lastRefresh = 0;
  let wasFetching = false;
  try {
    for (;;) {
      let st;
      try {
        st = await (await fetch("/api/status")).json();
      } catch {
        statusEl.textContent = "Reconnecting to the server…";
        await sleep(4000);
        continue;
      }
      setFetchButton(st);
      showProgress(st);
      const parts = [describeFetch(st), describeCheck(st.check)].filter(Boolean);
      if (parts.length) statusEl.textContent = parts.join(" · ");
      // New jobs after a fetch, and new AI results, show up in the list as they arrive.
      const fetching = !!(st.fetch?.running || liveBusy(st).length);
      const fetchJustEnded = wasFetching && !fetching;
      wasFetching = fetching;
      const progressed = st.check?.running && st.check.done !== lastDone;
      if (fetchJustEnded || ((progressed || fetching) && Date.now() - lastRefresh > 8000)) {
        lastDone = st.check?.done ?? lastDone;
        lastRefresh = Date.now();
        loadJobs().catch(() => {});
      }
      if (!st.running) break;
      await sleep(2500);
    }
    const data = await loadWithRetry();
    const err = data.task?.check?.error || data.task?.fetch?.error;
    statusEl.textContent = data.fetched_at ? `Up to date. ${summaryText(data)}` : "Ready.";
    if (err) statusEl.textContent += ` (${err})`;
  } finally {
    $("progress").hidden = true;
    setFetchButton(null);
    polling = false;
  }
}

// ---------------------------------------------------------------- events

listEl.addEventListener("click", (e) => {
  if (e.target.closest(".visit")) return;
  const quick = e.target.closest("[data-quickapply]");
  if (quick) {
    const job = findJob(quick.dataset.quickapply);
    if (job) setStatus(job.uid, job.application ? "" : "applied");
    return;
  }
  const item = e.target.closest("[data-uid]");
  if (item) select(item.dataset.uid);
});
document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-status]");
  if (sel) return setStatus(sel.dataset.status, sel.value);
  const box = e.target.closest("[data-visit]");
  if (box) setVisited([box.dataset.visit], box.checked);
});
detailEl.addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open]");
  if (open) {
    const job = findJob(open.dataset.open);
    if (job && !job.visited_at) setTimeout(() => setVisited([job.uid], true), 0); // opening = visited (untick to undo)
    return;
  }
  const refresh = e.target.closest("[data-refresh]");
  if (refresh) return loadContacts(refresh.dataset.refresh, true);
  const kitBtn = e.target.closest("[data-kit]");
  if (kitBtn) return loadKit(kitBtn.dataset.kit, true);
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      copy.classList.add("done");
      setTimeout(() => copy.classList.remove("done"), 1200);
    } catch {}
    return;
  }
  if (e.target.closest("#backBtn")) {
    state.selected = null;
    renderList();
    renderDetail();
  }
});
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  if (e.key === "Escape" && state.selected) {
    state.selected = null;
    renderList();
    renderDetail();
  }
  if (e.key === "j" || e.key === "k") {
    const shown = visibleJobs();
    const i = shown.findIndex((j) => j.uid === state.selected);
    const next = shown[Math.max(0, Math.min(shown.length - 1, i + (e.key === "j" ? 1 : -1)))];
    if (next) {
      select(next.uid);
      listEl.querySelector(`[data-uid="${CSS.escape(next.uid)}"]`)?.scrollIntoView({ block: "nearest" });
    }
  }
});

document.querySelectorAll(".seg-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    state.sort = btn.dataset.sort;
    savePrefs();
    syncControls();
    renderList();
  })
);
$("search").addEventListener("input", (e) => {
  state.search = e.target.value;
  renderList();
});
$("days").addEventListener("change", (e) => {
  state.days = e.target.value;
  savePrefs();
  renderList();
});
$("showRejected").addEventListener("change", (e) => {
  state.showRejected = e.target.checked;
  renderList();
});
$("hideApplied").addEventListener("change", (e) => {
  state.hideApplied = e.target.checked;
  renderList();
});
$("hideVisited").addEventListener("change", (e) => {
  state.hideVisited = e.target.checked;
  savePrefs();
  renderList();
});
$("markAllBtn").addEventListener("click", () => {
  const uids = visibleJobs().filter((j) => !j.visited_at).map((j) => j.uid);
  if (uids.length && confirm(`Mark ${uids.length} job(s) as visited?`)) setVisited(uids, true);
});
$("sources").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-src]");
  if (!btn) return;
  const src = btn.dataset.src;
  state.sources.has(src) ? state.sources.delete(src) : state.sources.add(src);
  renderSources();
  renderList();
});

fetchBtn.addEventListener("click", async () => {
  if (fetchBtn.classList.contains("busy")) return; // already fetching; progress is shown on the button
  setFetchButton({ fetch: { running: true } });
  try {
    const res = await fetch("/api/fetch", { method: "POST" });
    if (!res.ok && res.status !== 409) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Fetch failed");
    }
    pollTask();
    // Naukri has no free API: the extension reads your Naukri searches in a background tab (free).
    if (hasExtension()) toExtension({ type: "jf-naukri-sweep" }).catch(() => {});
  } catch (err) {
    statusEl.textContent = err.message || "Fetch failed";
    setFetchButton(null);
  }
});

$("missedBtn").addEventListener("click", async () => {
  const url = (prompt("Paste the link of a job you found that isn't in the app:") || "").trim();
  if (!url) return;
  statusEl.textContent = "Checking that job…";
  try {
    const res = await fetch("/api/missed", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    const r = await res.json();
    if (!res.ok) throw new Error(r.detail || "Couldn't check that link");
    const name = [r.title, r.company].filter(Boolean).join(" · ") || "That job";
    const label = { keep: "it's in your list", reject: `it was filtered out (${CATEGORY_LABELS[r.category] || r.category || "not relevant"}${r.reason ? ": " + r.reason : ""})`, unchecked: "it's waiting for the AI check" };
    alert(r.found ? `${name}: already fetched, ${label[r.status] || r.status}.`
      : r.imported ? `${name}: this was MISSED (${r.source}). It's added now and will be AI-checked. Logged so the gap can be fixed.`
      : `Couldn't read that page (${r.error || "unknown error"}). The miss is logged.`);
    statusEl.textContent = "";
    if (r.imported) { await loadJobs().catch(() => {}); pollTask(); }
  } catch (e) {
    statusEl.textContent = e.message;
  }
});

async function loadWithRetry() {
  for (let attempt = 1; ; attempt++) {
    try {
      return await loadJobs();
    } catch {
      statusEl.textContent =
        attempt < 3
          ? "Loading jobs… (the first load after a big fetch can take a minute)"
          : "Can't reach the server. Is python main.py running? Retrying…";
      await sleep(Math.min(3000 * attempt, 15000));
    }
  }
}

$("showUnchecked").addEventListener("change", (e) => {
  state.showUnchecked = e.target.checked;
  savePrefs();
  renderList();
});

// ---------------------------------------------------------------- auto-apply (runs in the extension)

let autoPolling = false;
const AUTO_ICON = { applied: "✅", skipped: "⏭", failed: "⚠", waiting: "✋", resumed: "▶", start: "▶", finished: "🏁" };
const hasExtension = () => !!document.documentElement.dataset.jobfinderExtension;

// Talk to the FresherPilot extension through bridge.js (window messages).
function toExtension(message) {
  return new Promise((resolve, reject) => {
    const id = Math.random().toString(36).slice(2);
    const onMsg = (e) => {
      if (e.source !== window || !e.data || e.data.__jf !== "from-extension" || e.data.id !== id) return;
      window.removeEventListener("message", onMsg);
      const r = e.data.response;
      r && r.ok ? resolve(r.data) : reject(new Error((r && r.error) || "Extension didn't answer"));
    };
    window.addEventListener("message", onMsg);
    window.postMessage({ __jf: "to-extension", id, message }, "*");
    setTimeout(() => { window.removeEventListener("message", onMsg); reject(new Error("Extension didn't answer. Reload it in edge://extensions.")); }, 10000);
  });
}

function renderAuto(s) {
  $("autoPanel").hidden = false;
  const counts = `applied ${s.applied}, skipped ${s.skipped}, failed ${s.failed}`;
  $("autoTitle").textContent = s.running ? `Auto-applying (${s.index || 0}/${s.total || 0}): ${counts}` : `Auto-apply finished: ${counts}`;
  $("autoNow").textContent = s.current ? `Now: ${s.current.title} · ${s.current.company || ""}` : "";
  const w = s.waiting;
  $("autoWaiting").hidden = !w;
  if (w) $("autoWaitingText").textContent = `Needs you: ${w.reason}. The tab is open in your browser; do it there, then click Continue.`;
  $("autoStop").hidden = !s.running;
  $("autoBtn").disabled = s.running;
  document.querySelectorAll("[data-autoapply]").forEach((b) => (b.disabled = s.running));
  $("autoLog").innerHTML = (s.log || [])
    .slice()
    .reverse()
    .map((e) => `<li>${AUTO_ICON[e.event] || "•"} <strong>${escapeHtml(e.title || e.event)}</strong>${e.company ? ` · ${escapeHtml(e.company)}` : ""}${e.detail ? ` <span class="muted">${escapeHtml(e.detail)}</span>` : ""}</li>`)
    .join("");
}

async function pollAuto() {
  if (autoPolling) return;
  autoPolling = true;
  try {
    for (;;) {
      let s;
      try {
        s = await (await fetch("/api/autoapply")).json();
      } catch {
        await sleep(5000);
        continue;
      }
      renderAuto(s);
      if (!s.running) break;
      await sleep(3000);
    }
    loadJobs().catch(() => {});
  } finally {
    autoPolling = false;
  }
}

async function startAutoApply(uids) {
  if (!hasExtension()) {
    alert("Auto-apply runs inside the FresherPilot browser extension (so it uses your logins).\n\nInstall it, or reload it in edge://extensions / chrome://extensions, then refresh this page.");
    return;
  }
  if (!uids && !confirm("Auto-apply to your best-matching jobs (up to 25), one by one in background tabs?\n\nIt pauses and asks you whenever a site needs you to sign in.")) return;
  try {
    const res = await fetch("/api/autoapply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(uids ? { uids, max: uids.length } : { max: 25 }),
    });
    const start = await res.json();
    if (!res.ok) throw new Error(start.detail || "Couldn't start auto-apply");
    await toExtension({ type: "jf-autoapply-start", start });
    pollAuto();
  } catch (err) {
    statusEl.textContent = err.message;
  }
}

const autoCommand = (action) =>
  fetch("/api/autoapply/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });

$("autoBtn").addEventListener("click", () => startAutoApply(null));
$("autoStop").addEventListener("click", () => autoCommand("stop"));
$("autoContinue").addEventListener("click", () => autoCommand("continue"));
$("autoSkip").addEventListener("click", () => autoCommand("skip"));
$("autoClose").addEventListener("click", () => ($("autoPanel").hidden = true));
detailEl.addEventListener("click", (e) => {
  const b = e.target.closest("[data-autoapply]");
  if (b) startAutoApply([b.dataset.autoapply]);
});
fetch("/api/autoapply").then((r) => r.json()).then((s) => { if (s.running) pollAuto(); }).catch(() => {});

// ---------------------------------------------------------------- views (Dashboard / Jobs)

function showView(name) {
  if (!["dashboard", "jobs"].includes(name)) name = "dashboard";
  $("dashboardView").hidden = name !== "dashboard";
  $("jobsView").hidden = name !== "jobs";
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("on", t.dataset.view === name);
    t.setAttribute("aria-current", t.dataset.view === name ? "page" : "false");
  });
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "dashboard" && typeof refreshDashboard === "function") refreshDashboard();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
window.addEventListener("hashchange", () => showView(location.hash.slice(1)));

function toast(message, ms = 3500) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), ms);
}

syncControls();
statusEl.textContent = "Loading jobs…";
loadWithRetry().then((data) => {
  statusEl.textContent = summaryText(data);
  if (data.task?.running) pollTask();
});

// The AI checker can also start by itself (new jobs, or after a quota reset): follow it when it does.
setInterval(async () => {
  if (polling) return;
  try {
    const st = await (await fetch("/api/status")).json();
    if (st.running) pollTask();
  } catch {}
}, 60000);

// Who is signed in; give the browser extension this user's token so it works for them.
fetch("/api/me").then((r) => r.json()).then((me) => {
  const setup = me.setup || {};
  const missing = [!setup.ai_key && "an AI key (Groq, free)", !setup.resume && "your resume"].filter(Boolean);
  state.setupMissing = missing.length > 0;
  $("setupBanner").hidden = !missing.length;
  if (missing.length) $("setupText").textContent = `Add ${missing.join(" and ")} so the app can check which jobs fit you.`;
  if (me.prefs?.target_area) $("tagline").textContent = `${me.prefs.target_area}, matched to your resume`;
  if (me.user) $("userBtn").textContent = `${me.user.username} · Settings`;
  if (me.extension_token && hasExtension()) toExtension({ type: "jf-set-token", token: me.extension_token }).catch(() => {});
}).catch(() => {});
