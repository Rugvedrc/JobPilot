// One-click outreach: email everyone found for a job (Gmail) and WhatsApp the mobile numbers.
// Messages come from your templates ({name}, {role}, {company}, {skills}…), filled in on the
// server, so no AI tokens are used. The draft is shown until you tick "don't show again".

const orState = { uid: null, preview: null, focus: null, sending: false, cache: {} };
const enc = encodeURIComponent;
const BAD_INBOX = /^(no-?reply|do-?not-?reply|donotreply|privacy|abuse|legal|unsubscribe|press|media|sales|support|help|helpdesk|security|postmaster|webmaster|admin|billing|marketing|grievance)([._-].*)?@/i;
const ORIGIN = {
  listed: ["ok", "✓ Genuine · in the job post"],
  "job post": ["ok", "✓ Genuine · in the job post"],
  public: ["ok", "✓ Genuine · public on the web"],
  "found online": ["ok", "✓ Genuine · public on the web"],
  guessed: ["guess", "⚠ Guessed · needs verification"],
};
const badge = (kind) => {
  const [cls, text] = ORIGIN[kind] || ["", kind];
  return `<span class="badge ${cls}">${text}</span>`;
};

// Gmail compose window, already filled, in the right Google account (not your browser's default one).
function gmailUrl(sender, to, subject, body) {
  return `https://mail.google.com/mail/?authuser=${enc(sender)}&view=cm&fs=1&tf=1&to=${enc(to)}&su=${enc(subject)}&body=${enc(body)}`;
}
function mobileDigits(raw) {
  let n = String(raw || "").replace(/\D/g, "");
  if (n.length === 11 && n.startsWith("0")) n = n.slice(1);
  if (n.length === 10 && /^[6-9]/.test(n)) return "91" + n;
  if (n.length === 12 && /^91[6-9]/.test(n)) return n;
  return "";
}
const personVals = (p, name) => {
  const first = (String(name || "").match(/[A-Za-z]{2,}/) || [""])[0];
  return { ...p.values, name: first ? first[0].toUpperCase() + first.slice(1).toLowerCase() : p.values.name };
};

// Draft for a job (templates filled in by the server); loaded once its contacts are known.
function orPreview(uid) {
  const c = orState.cache[uid];
  if (c) return c.data || null;
  if (!state.contacts[uid]?.data || state.contacts[uid].loading) return null;
  orState.cache[uid] = { loading: true };
  fetch(`/api/outreach?uid=${enc(uid)}`)
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((data) => (orState.cache[uid] = { data }))
    .catch(() => (orState.cache[uid] = { error: true }))
    .finally(() => state.selected === uid && renderDetail());
  return null;
}

function gmailBtn(job, addr, name = "") {
  const p = orPreview(job.uid);
  if (!p || BAD_INBOX.test(addr) || FAKE_DOMAIN.test(addr)) return "";
  const v = personVals(p, name);
  const url = gmailUrl(p.sender, addr, fillTemplate(p.settings.email_subject, v), fillTemplate(p.settings.email_body, v));
  return `<a class="btn small gmail" href="${escapeHtml(url)}" target="_blank" rel="noopener" title="Opens Gmail (${escapeHtml(p.sender)}) with subject and message filled in">Open in Gmail ${ICON.external}</a>`;
}

const FAKE_DOMAIN = /@(example|mysite|yoursite|yourdomain|domain|yourcompany|company|email|test|sample|website|mail)\.(com|in|org|net|co)$/i;

function emailRow(job, addr, kind, source = "") {
  if (FAKE_DOMAIN.test(addr)) return ""; // placeholder text from some website, not a real inbox
  const bad = BAD_INBOX.test(addr);
  const src = source ? `<a class="src" href="${escapeHtml(safeUrl(source))}" target="_blank" rel="noopener" title="Where this was found">source</a>` : "";
  return `<li class="addr">${ICON.mail}<span class="addr-v">${escapeHtml(addr)}</span>${copyBtn(addr)}
    ${bad ? `<span class="badge">not a hiring inbox</span>` : badge(kind)}${gmailBtn(job, addr)}${src}</li>`;
}

function phoneRow(job, raw, kind, source = "") {
  const num = mobileDigits(raw);
  const p = orPreview(job.uid);
  const src = source ? `<a class="src" href="${escapeHtml(safeUrl(source))}" target="_blank" rel="noopener" title="Where this was found">source</a>` : "";
  const wa = num && p
    ? `<a class="btn small gmail" href="https://wa.me/${num}?text=${enc(fillTemplate(p.settings.whatsapp_body, p.values))}" target="_blank" rel="noopener">WhatsApp chat ${ICON.external}</a>`
    : "";
  return `<li class="addr">${ICON.phone}<a class="addr-v" href="tel:${escapeHtml(raw)}">${escapeHtml(raw)}</a>${copyBtn(raw)}
    ${badge(kind)}${num ? "" : `<span class="badge">landline · call</span>`}${wa}${src}</li>`;
}

function draftHtml(job) {
  if (job.status === "reject") return "";
  const p = orPreview(job.uid);
  if (!p) return orState.cache[job.uid]?.loading ? `<p class="muted small loading">Preparing your email draft…</p>` : "";
  const subj = fillTemplate(p.settings.email_subject, p.values);
  const body = fillTemplate(p.settings.email_body, p.values);
  const wa = fillTemplate(p.settings.whatsapp_body, p.values);
  const hasPhone = p.recipients.phones.length;
  return `<details class="draft" open>
    <summary><strong>Ready-to-send draft</strong> <span class="muted small">copy it, or use "Open in Gmail" next to an address below</span></summary>
    <div class="draft-field"><span class="draft-label">Subject</span><div class="draft-text">${escapeHtml(subj)}</div>${copyBtn(subj)}</div>
    <div class="draft-field"><span class="draft-label">Body</span><div class="draft-text body">${escapeHtml(body)}</div>${copyBtn(body)}</div>
    ${hasPhone ? `<div class="draft-field"><span class="draft-label">WhatsApp</span><div class="draft-text body">${escapeHtml(wa)}</div>${copyBtn(wa)}</div>` : ""}
    <p class="muted small">Gmail opens as <strong>${escapeHtml(p.sender)}</strong>. Gmail links can't attach files, so attach your resume yourself: <a href="/api/resume" download>download resume PDF</a>. "Hi there" becomes the person's first name when it's known.</p>
  </details>`;
}

const fillTemplate = (tpl, vals) =>
  String(tpl || "")
    .replace(/\{(\w+)\}/g, (m, k) => (k in vals ? vals[k] : m))
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

function estimateRecipients(job) {
  const d = state.contacts[job.uid]?.data;
  if (!d) return null;
  const jd = d.from_description || { emails: [], phones: [] };
  const c = d.company || {};
  const bad = /^(no-?reply|donotreply|privacy|abuse|legal|press|sales|support|help|security|postmaster|webmaster|admin|billing|marketing)([._-].*)?@/i;
  const emails = new Set([...jd.emails, ...(c.emails || []).map((e) => e.value)].map((e) => e.toLowerCase()).filter((e) => !bad.test(e)));
  const mobile = (p) => {
    let n = String(p).replace(/\D/g, "");
    if (n.length === 11 && n.startsWith("0")) n = n.slice(1);
    if (n.length === 12 && n.startsWith("91")) n = n.slice(2);
    return n.length === 10 && /^[6-9]/.test(n) ? n : "";
  };
  const phones = new Set([...jd.phones, ...(c.phones || []).map((p) => p.value)].map(mobile).filter(Boolean));
  const guessed = (d.guessed?.mx && d.guessed.emails?.length) || 0;
  return { emails: emails.size, phones: phones.size, guessed };
}

function outreachBar(job) {
  if (job.status === "reject") return "";
  const sent = job.outreach;
  const entry = state.contacts[job.uid];
  const uid = escapeHtml(job.uid);
  if (sent) {
    const parts = [sent.email && `${sent.email} email${sent.email === 1 ? "" : "s"}`, sent.whatsapp && `${sent.whatsapp} WhatsApp`].filter(Boolean).join(" + ");
    return `<div class="reach-bar done"><p>✓ <strong>Contacted</strong>: ${parts} sent.</p><button type="button" class="btn small" data-outreach="${uid}">Send more…</button></div>`;
  }
  if (!entry || entry.loading) return `<div class="reach-bar"><p class="muted loading">Looking for people to contact…</p></div>`;
  const est = estimateRecipients(job);
  if (!est || (!est.emails && !est.phones)) {
    return est?.guessed
      ? `<div class="reach-bar"><p>No public email or mobile found. ${est.guessed} likely inboxes (careers@, hr@…) can be tried.</p><button type="button" class="btn small" data-outreach="${uid}">Choose &amp; send…</button></div>`
      : "";
  }
  const what = [est.emails && `${est.emails} email${est.emails === 1 ? "" : "s"}`, est.phones && `${est.phones} WhatsApp`].filter(Boolean).join(" + ");
  return `<div class="reach-bar"><p><strong>Reach out:</strong> ${what} found for this job.</p><button type="button" class="btn primary small" data-outreach="${uid}">Send to all</button></div>`;
}

// ---------------------------------------------------------------- draft dialog

function recipientRow(r, kind) {
  const p = orState.preview;
  const label = kind === "email" ? r.to : `+${r.to.slice(0, r.to.length - 10)} ${r.to.slice(-10)}`;
  const sent = r.sent_at ? ` <span class="badge">sent ${escapeHtml(formatWhen(r.sent_at))}</span>` : "";
  const v = personVals(p, r.name);
  const link = kind === "email"
    ? gmailUrl(p.sender, r.to, fillTemplate(p.settings.email_subject, v), fillTemplate(p.settings.email_body, v))
    : `https://wa.me/${r.to}?text=${enc(fillTemplate(p.settings.whatsapp_body, v))}`;
  return `<li><label><input type="checkbox" data-r="${kind}" value="${escapeHtml(r.to)}" data-name="${escapeHtml(r.name || "")}" ${r.selected ? "checked" : ""}/>
    ${escapeHtml(label)}</label> ${badge(r.origin)}${sent}
    <a class="link-btn small" href="${escapeHtml(link)}" target="_blank" rel="noopener">${kind === "email" ? "open in Gmail" : "open chat"} ↗</a></li>`;
}

function renderDialog() {
  const p = orState.preview;
  const job = findJob(orState.uid) || {};
  const s = p.settings;
  const { emails, phones } = p.recipients;
  const vals = p.values;
  const ph = Object.entries(p.placeholders).map(([k, v]) => `<code data-ph="{${k}}" title="${escapeHtml(v)}">{${k}}</code>`).join("");
  const gmail = p.email_ready
    ? ""
    : `<p class="notice warn">Gmail isn't connected yet, so emails can't be sent. One-time setup (2 minutes):<br>
       1. Turn on 2-Step Verification for ${escapeHtml(p.sender || "your Google account")}.<br>
       2. Open <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">myaccount.google.com/apppasswords</a>, create one named "FresherPilot".<br>
       3. Add <code>GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx</code> to <code>.env</code> and restart <code>python main.py</code>.</p>`;
  const left = { email: p.caps.email - p.sent_today.email, whatsapp: p.caps.whatsapp - p.sent_today.whatsapp };

  $("orForm").innerHTML = `
    <div>
      <h2 id="orTitle">Reach out about ${escapeHtml(vals.role)} at ${escapeHtml(vals.company)}</h2>
      <p class="muted small" style="margin:4px 0 0">One message per person, sent from ${escapeHtml(p.sender || "your Gmail")} and your WhatsApp. Today: ${p.sent_today.email}/${p.caps.email} emails, ${p.sent_today.whatsapp}/${p.caps.whatsapp} WhatsApp.</p>
    </div>
    ${gmail}
    <div class="stack">
      <div><h4>Email (${emails.length})</h4>${emails.length ? `<ul class="recips">${emails.map((r) => recipientRow(r, "email")).join("")}</ul>` : `<p class="muted small">No email addresses found.</p>`}
        ${emails.some((r) => r.origin === "guessed") ? `<p class="muted small">Guessed inboxes may bounce, which can hurt your Gmail reputation. Untick them unless nothing else is available.</p>` : ""}</div>
      <div><h4>WhatsApp (${phones.length})</h4>${phones.length ? `<ul class="recips">${phones.map((r) => recipientRow(r, "phone")).join("")}</ul>` : `<p class="muted small">No mobile numbers found.</p>`}
        ${phones.length ? `<p class="muted small">Sent through WhatsApp Web in this browser (keep it logged in), ~30 s apart.</p>` : ""}</div>
    </div>

    <div>
      <h4>Preview</h4>
      <div class="preview"><span class="subj">${escapeHtml(fillTemplate(s.email_subject, vals))}</span>${escapeHtml(fillTemplate(s.email_body, vals))}${s.attach_resume ? `\n\n📎 resume.pdf attached` : ""}</div>
      ${phones.length ? `<div class="preview" style="margin-top:8px">${escapeHtml(fillTemplate(s.whatsapp_body, vals))}</div>` : ""}
      <div class="row" style="margin-top:8px">
        <button type="button" class="btn small" data-copyfield="subject">${ICON.copy} Copy subject</button>
        <button type="button" class="btn small" data-copyfield="body">${ICON.copy} Copy body</button>
        ${phones.length ? `<button type="button" class="btn small" data-copyfield="whatsapp">${ICON.copy} Copy WhatsApp text</button>` : ""}
        <a class="btn small" href="/api/resume" download>Download resume</a>
      </div>
    </div>

    <details id="orEdit">
      <summary class="link-btn edit-toggle">Edit the template (used for every job)</summary>
      <div style="display:grid;gap:10px;margin-top:10px">
        <div class="ph" aria-label="Placeholders (click to insert)">${ph}</div>
        <label class="field">Email subject<input type="text" name="email_subject" value="${escapeHtml(s.email_subject)}"/></label>
        <label class="field">Email message<textarea name="email_body" rows="9">${escapeHtml(s.email_body)}</textarea></label>
        <label class="field">WhatsApp message<textarea name="whatsapp_body" rows="4">${escapeHtml(s.whatsapp_body)}</textarea></label>
      </div>
    </details>

    <div class="modal-foot">
      <label class="grow"><input type="checkbox" name="hide_draft" ${s.show_draft ? "" : "checked"}/> Don't show this draft again: send right away next time</label>
      <button type="button" class="btn" value="cancel" id="orCancel">Cancel</button>
      <button type="submit" class="btn primary" id="orSend"></button>
    </div>`;
  updateSendLabel(left);
}

function selected(kind) {
  return [...$("orForm").querySelectorAll(`input[data-r="${kind}"]:checked`)].map((i) => ({ to: i.value, name: i.dataset.name || "" }));
}

function updateSendLabel(left) {
  const p = orState.preview;
  left = left || { email: p.caps.email - p.sent_today.email, whatsapp: p.caps.whatsapp - p.sent_today.whatsapp };
  const e = p.email_ready ? selected("email").length : 0;
  const w = selected("phone").length;
  const btn = $("orSend");
  const parts = [e && `${e} email${e === 1 ? "" : "s"}`, w && `${w} WhatsApp`].filter(Boolean);
  const needsGmail = !p.email_ready && selected("email").length && !w;
  btn.textContent = orState.sending ? "Sending…" : parts.length ? `Send ${parts.join(" + ")}` : needsGmail ? "Connect Gmail to send" : "Nothing selected";
  btn.disabled = orState.sending || !parts.length || (e && left.email <= 0 && !w);
}

// Re-render the preview live while the template is edited.
function livePreview() {
  const f = $("orForm");
  const vals = orState.preview.values;
  const s = orState.preview.settings;
  for (const name of ["email_subject", "email_body", "whatsapp_body"]) if (f.elements[name]) s[name] = f.elements[name].value;
  const previews = f.querySelectorAll(".preview");
  previews[0].innerHTML = `<span class="subj">${escapeHtml(fillTemplate(s.email_subject, vals))}</span>${escapeHtml(fillTemplate(s.email_body, vals))}${s.attach_resume ? "\n\n📎 resume.pdf attached" : ""}`;
  if (previews[1]) previews[1].textContent = fillTemplate(s.whatsapp_body, vals);
}

$("orForm").addEventListener("input", (e) => {
  if (e.target.matches("textarea, input[type=text]")) livePreview();
});
$("orForm").addEventListener("change", (e) => {
  if (e.target.matches("[data-r]")) updateSendLabel();
});
$("orForm").addEventListener("focusin", (e) => {
  if (e.target.matches("textarea, input[type=text]")) orState.focus = e.target;
});
$("orForm").addEventListener("click", (e) => {
  const ph = e.target.closest("[data-ph]");
  if (ph && orState.focus) {
    const el = orState.focus;
    const at = el.selectionStart ?? el.value.length;
    el.value = el.value.slice(0, at) + ph.dataset.ph + el.value.slice(el.selectionEnd ?? at);
    el.focus();
    el.selectionStart = el.selectionEnd = at + ph.dataset.ph.length;
    livePreview();
  }
  if (e.target.closest("#orCancel")) $("outreachDialog").close();
  const cf = e.target.closest("[data-copyfield]");
  if (cf) {
    const st = orState.preview.settings;
    const text = { subject: st.email_subject, body: st.email_body, whatsapp: st.whatsapp_body }[cf.dataset.copyfield];
    navigator.clipboard.writeText(fillTemplate(text, orState.preview.values)).then(() => toast("Copied", 1500)).catch(() => {});
  }
});
$("orForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = $("orForm");
  const emails = orState.preview.email_ready ? selected("email") : [];
  sendOutreach(orState.uid, {
    emails,
    phones: selected("phone"),
    email_subject: f.elements.email_subject.value,
    email_body: f.elements.email_body.value,
    whatsapp_body: f.elements.whatsapp_body.value,
    show_draft: !f.elements.hide_draft.checked,
  });
});

// ---------------------------------------------------------------- sending

async function openOutreach(uid, { forceDraft = false } = {}) {
  const job = findJob(uid);
  if (!job) return;
  toast("Preparing messages…", 2000);
  let p;
  try {
    const res = await fetch(`/api/outreach?uid=${encodeURIComponent(uid)}`);
    p = await res.json();
    if (!res.ok) throw new Error(p.detail || "Couldn't prepare the messages");
  } catch (err) {
    return toast(err.message);
  }
  orState.uid = uid;
  orState.preview = p;
  const emails = p.recipients.emails.filter((r) => r.selected);
  const phones = p.recipients.phones.filter((r) => r.selected);
  const direct = !forceDraft && !p.settings.show_draft && (emails.length || phones.length) && (p.email_ready || !emails.length);
  if (direct) {
    return sendOutreach(uid, { emails: emails.map((r) => ({ to: r.to, name: r.name })), phones: phones.map((r) => ({ to: r.to, name: r.name })) });
  }
  $("toast").hidden = true;
  renderDialog();
  $("outreachDialog").showModal();
}

async function sendOutreach(uid, body) {
  const dialog = $("outreachDialog");
  orState.sending = true;
  if (dialog.open) updateSendLabel();
  const n = body.emails.length;
  toast(n ? `Sending ${n} email${n === 1 ? "" : "s"}… (a few seconds apart)` : "Starting WhatsApp…", 60000);
  try {
    const res = await fetch("/api/outreach/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid, ...body }),
    });
    const r = await res.json();
    if (!res.ok) throw new Error(r.detail || "Sending failed");
    orState.cache = {}; // the template may have changed
    const okMails = r.emails.filter((x) => x.ok).length;
    const failed = r.emails.filter((x) => !x.ok);
    let waNote = "";
    if (r.whatsapp.length) {
      if (!hasExtension()) {
        waNote = " WhatsApp needs the FresherPilot extension (reload it, then refresh this page).";
      } else {
        const started = await toExtension({ type: "jf-whatsapp-send", items: r.whatsapp, gap: r.whatsapp_gap }).catch((e) => ({ ok: false, error: e.message }));
        waNote = started?.ok === false ? ` WhatsApp: ${started.error}` : ` Sending ${r.whatsapp.length} WhatsApp message${r.whatsapp.length === 1 ? "" : "s"} in the background.`;
        if (started?.ok !== false) followWhatsApp(uid, r.whatsapp.length);
      }
    }
    const errs = [...r.errors, ...failed.map((x) => `${x.to}: ${x.error}`)];
    const job = findJob(uid);
    if (job && okMails) job.outreach = { email: (job.outreach?.email || 0) + okMails, whatsapp: job.outreach?.whatsapp || 0 };
    if (job && (okMails || r.whatsapp.length)) job.visited_at = job.visited_at || new Date().toISOString();
    if (dialog.open && (okMails || r.whatsapp.length)) dialog.close();
    toast(`${okMails ? `✓ ${okMails} email${okMails === 1 ? "" : "s"} sent.` : ""}${waNote}${errs.length ? ` Problems: ${errs.join("; ")}` : ""}`.trim() || "Nothing was sent.", errs.length ? 12000 : 6000);
    renderList();
    renderDetail();
    refreshDashboard();
  } catch (err) {
    toast(err.message, 8000);
  } finally {
    orState.sending = false;
    if (dialog.open) updateSendLabel();
  }
}

// WhatsApp results arrive from the extension one by one; refresh the counts as they do.
async function followWhatsApp(uid, expected) {
  const events = async () => {
    const r = await (await fetch(`/api/outreach/job?uid=${encodeURIComponent(uid)}`)).json();
    return { r, wa: r.sent.filter((e) => e.channel === "whatsapp") };
  };
  let start;
  try {
    start = (await events()).wa.length;
  } catch {
    return;
  }
  const deadline = Date.now() + expected * 120000 + 60000;
  let seen = start;
  while (Date.now() < deadline && seen < start + expected) {
    await sleep(8000);
    try {
      const { r, wa } = await events();
      for (const e of wa.slice(seen)) toast(e.ok ? `✓ WhatsApp sent to +${e.to}` : `WhatsApp to +${e.to} failed: ${e.error}`, 5000);
      seen = wa.length;
      const job = findJob(uid);
      if (job) job.outreach = r.counts.email || r.counts.whatsapp ? { ...r.counts } : null;
      renderList();
      if (state.selected === uid) renderDetail();
    } catch {}
  }
  refreshDashboard();
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-outreach]");
  if (b) openOutreach(b.dataset.outreach, { forceDraft: !!findJob(b.dataset.outreach)?.outreach });
});
