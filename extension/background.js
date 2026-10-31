// FresherPilot extension background worker:
//  - form filling (popup button / Alt+Shift+F)
//  - auto-apply runner: opens jobs in YOUR browser (your logins), asks the app's AI what to press
//    next (Vimium-style hint keys), fills forms, and pauses for you when a site needs sign-in etc.

const DEFAULT_SERVER = "http://localhost:8000";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rand = ([a, b]) => (a + Math.random() * (b - a)) * 1000;

async function serverUrl() {
  const { server } = await chrome.storage.local.get("server");
  return (server || DEFAULT_SERVER).replace(/\/+$/, "");
}

// The app-page bridge (bridge.js) is built in for localhost. For the app at another address (e.g.
// your Cloudflare Tunnel URL, set in the popup) it is registered for that address here.
async function syncBridge() {
  let u;
  try {
    u = new URL(await serverUrl());
  } catch {
    return;
  }
  await chrome.scripting.unregisterContentScripts({ ids: ["jf-bridge-remote"] }).catch(() => {});
  if (/^(localhost|127\.0\.0\.1)$/.test(u.hostname)) return;
  await chrome.scripting
    .registerContentScripts([{ id: "jf-bridge-remote", matches: [`${u.protocol}//${u.hostname}/*`], js: ["bridge.js"],
      runAt: "document_start", persistAcrossSessions: true }])
    .catch((e) => console.warn("FresherPilot bridge:", e));
}
chrome.runtime.onInstalled.addListener(syncBridge);
chrome.runtime.onStartup.addListener(syncBridge);
chrome.storage.onChanged.addListener((changes) => { if (changes.server) syncBridge(); });

async function authHeader() {
  const { token } = await chrome.storage.local.get("token");
  return token ? { "X-JF-Token": token } : {};
}

// The app's login sessions live in memory, so a restart makes the saved token stale.
// Get a fresh one from the browser's own app login (cookie) when that happens.
async function refreshToken() {
  const res = await fetch(`${await serverUrl()}/api/me`, { credentials: "include" }).catch(() => null);
  const me = res && res.ok ? await res.json().catch(() => null) : null;
  if (!me?.extension_token) return false;
  await chrome.storage.local.set({ token: me.extension_token });
  return true;
}

async function authFetch(path, options = {}) {
  const go = async () =>
    fetch(`${await serverUrl()}${path}`, { ...options, headers: { ...(await authHeader()), ...(options.headers || {}) } });
  let res;
  try {
    res = await go();
  } catch {
    throw new Error(`Can't reach the FresherPilot app at ${await serverUrl()}. Start it with python main.py.`);
  }
  if (res.status === 401 && (await refreshToken())) res = await go();
  if (res.status === 401) throw new Error(`Sign in to the FresherPilot app (open ${await serverUrl()} and log in), then try again.`);
  return res;
}

async function api(path, options = {}) {
  const res = await authFetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `FresherPilot app error ${res.status}`);
  return data;
}

async function resumeBase64() {
  const res = await authFetch("/api/resume");
  if (!res.ok) throw new Error("Could not load resume from the FresherPilot app");
  const name = (res.headers.get("content-disposition") || "").match(/filename="?([^"]+)"?/)?.[1] || "resume.pdf";
  const bytes = new Uint8Array(await res.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return { name, type: "application/pdf", base64: btoa(binary) };
}

// ---------------------------------------------------------------- manual fill (popup / shortcut)

async function fillTab(tabId, overwrite) {
  await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["content.js"] });
  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: (ow) => (window.__jobFinderFill ? window.__jobFinderFill(ow) : null),
    args: [!!overwrite],
  });
  const summary = { found: 0, filled: 0, review: 0, files: 0, job: null, errors: [], missing_profile: [] };
  for (const r of results) {
    const v = r.result;
    if (!v) continue;
    summary.found += v.found || 0;
    summary.filled += v.filled || 0;
    summary.review += v.review || 0;
    summary.files += v.files || 0;
    summary.job = summary.job || v.job;
    if (v.error) summary.errors.push(v.error);
    if (v.missing_profile?.length) summary.missing_profile = v.missing_profile;
  }
  return summary;
}

// ---------------------------------------------------------------- auto-apply runner

const applyingTab = {}; // tabId -> job uid (so form answers use the right job)
let running = false;

async function inTab(tabId, func, args = []) {
  const [res] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return res?.result;
}

async function inject(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js", "agent.js"] });
}

async function waitForLoad(tabId, timeout = 25000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("The tab was closed");
    if (tab.status === "complete") return tab;
    await sleep(500);
  }
  return chrome.tabs.get(tabId);
}

const report = (event, uid, detail = "", waiting = null) =>
  api("/api/autoapply/event", { method: "POST", body: JSON.stringify({ event, uid, detail, waiting }) }).catch(() => null);

// Pause until the user clicks Continue / Skip / Stop in the app.
const status = (tabId, text, waiting = false) =>
  inTab(tabId, (t, w) => window.__jfAgent && window.__jfAgent.showStatus(t, w), [text, waiting]).catch(() => null);

// Read the page; if it navigated or reloaded in between (helpers gone), re-inject and retry.
async function takeSnapshot(tabId) {
  for (let i = 0; i < 4; i++) {
    const snap = await inTab(tabId, () => (window.__jfAgent ? window.__jfAgent.snapshot() : null)).catch(() => null);
    if (snap) return snap;
    await sleep(1500);
    await ready(tabId).catch(() => null);
  }
  throw new Error("Couldn't read the page (it kept reloading)");
}

// Inject helpers and wait until the page is really ready (loaded, nothing spinning, DOM + network quiet).
const scrolledPages = new Set(); // tabId|url already scrolled through once

async function ready(tabId) {
  await waitForLoad(tabId);
  await inject(tabId);
  const r = await inTab(tabId, () => window.__jfAgent.waitReady(30000));
  // First visit to a page: scroll through it once so lazy-loaded buttons exist.
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const key = `${tabId}|${(tab?.url || "").split("#")[0]}`;
  if (tab && !scrolledPages.has(key)) {
    scrolledPages.add(key);
    await inTab(tabId, () => window.__jfAgent.scrollThrough()).catch(() => null);
    await inTab(tabId, () => window.__jfAgent.waitReady(8000, 500)).catch(() => null);
  }
  return r;
}

// Pause until the user acts. Resumes by itself when the problem is gone (challenge solved, signed in,
// required answers filled), or when the user clicks Continue / Skip / Stop in the app or in the tab's panel.
async function waitForUser(tabId, job, reason, kind) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (tab) {
    await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  }
  await report("waiting", job.uid, reason, { reason, url: tab?.url || "", title: job.title });
  let okStreak = 0;
  for (let i = 0; i < 900; i++) { // up to ~30 minutes
    await sleep(2000);
    const { command } = await api("/api/autoapply/command").catch(() => ({ command: null }));
    if (command) {
      if (command === "continue") await report("resumed", job.uid);
      return command;
    }
    // Keep the panel visible (the page may have navigated) and check whether it's resolved.
    await inject(tabId).catch(() => null);
    await status(tabId, `Needs you: ${reason}`, true);
    const resolved = await inTab(tabId, (k) => {
      const a = window.__jfAgent;
      if (!a) return false;
      if (k === "answers") return a.emptyRequired().length === 0;
      return a.blockedState() === "";
    }, [kind]).catch(() => false);
    okStreak = resolved ? okStreak + 1 : 0;
    if (okStreak >= 2) { // stable for ~4 s
      await report("resumed", job.uid, "resolved, continuing");
      return "continue";
    }
  }
  return "skip";
}

async function applyJob(job, settings, mode) {
  const tab = await chrome.tabs.create({ url: job.url, active: mode === "single" });
  let tabId = tab.id;
  const opened = [tabId];
  applyingTab[tabId] = job.uid;
  try {
    let filled = false;
    let submitted = false;
    let refills = 0;
    let rescanned = false;
    const history = [];
    for (let step = 0; step < (settings.max_steps || 28); step++) {
      const r = await ready(tabId);
      await status(tabId, r && !r.ready ? `Page still busy (${r.reason}); continuing anyway…` : "Reading the page…");
      let snapshot = await takeSnapshot(tabId);

      // Security checks: automatic ones get time to finish; real challenges go to the user.
      if (snapshot.blocked === "challenge" || snapshot.blocked === "challenge-loading") {
        await status(tabId, "Waiting for the site's security check…");
        for (let i = 0; i < 8 && snapshot.blocked; i++) {
          await sleep(2000);
          snapshot = await takeSnapshot(tabId);
        }
      }
      if (snapshot.blocked) {
        const reason = snapshot.blocked === "login" ? "Sign in on this site" : "Solve the security check (CAPTCHA)";
        const cmd = await waitForUser(tabId, job, reason, "blocked");
        if (cmd === "stop") return { event: "skipped", detail: "stopped by you", stop: true };
        if (cmd === "skip") return { event: "skipped", detail: `skipped by you (${reason})` };
        filled = false;
        continue;
      }

      const decision = await api("/api/autoapply/decide", {
        method: "POST",
        body: JSON.stringify({ job, snapshot, filled, submitted, history }),
      });
      history.push(`${decision.action} ${decision.key || ""} (${(decision.reason || "").slice(0, 60)})`);

      if (decision.action === "done") {
        await status(tabId, "Applied ✓");
        return { event: "applied", detail: job.source };
      }
      if (decision.action === "skip") {
        // The site says you've applied before: that counts as applied, not skipped.
        if (/already (applied|submitted)|you('ve| have) applied/i.test(decision.reason || "")) {
          return { event: "applied", detail: "already applied earlier" };
        }
        // "No apply button" is often content that appears only after scrolling: look once more.
        if (!rescanned && /button|apply|not (visible|present|found|shown)|no .*(link|option)/i.test(decision.reason || "")) {
          rescanned = true;
          await status(tabId, "Looking further down the page…");
          await inTab(tabId, () => window.__jfAgent.scrollThrough()).catch(() => null);
          await inTab(tabId, () => window.__jfAgent.waitReady(8000, 500)).catch(() => null);
          continue;
        }
        return { event: "skipped", detail: decision.reason };
      }
      if (decision.action === "needs_user") {
        const cmd = await waitForUser(tabId, job, decision.reason, "blocked");
        if (cmd === "stop") return { event: "skipped", detail: "stopped by you", stop: true };
        if (cmd === "skip") return { event: "skipped", detail: `skipped by you (${decision.reason})` };
        filled = false;
        continue;
      }
      if (decision.action === "fill") {
        if (filled) {
          if (refills++ >= 1) return { event: "skipped", detail: "AI kept asking to fill an already-filled form" };
          filled = false;
        }
        await status(tabId, "Filling the form…");
        await inTab(tabId, () => window.__jobFinderFill(false));
        filled = true;
        await inTab(tabId, () => window.__jfAgent.waitReady(10000, 500));
        const missing = await inTab(tabId, () => window.__jfAgent.emptyRequired());
        if (missing?.length) {
          const reason = `Answer: ${missing.slice(0, 3).join(", ")}`;
          if (mode !== "single") return { event: "skipped", detail: `needs your answer: ${missing.slice(0, 3).join(", ")}` };
          const cmd = await waitForUser(tabId, job, reason, "answers");
          if (cmd !== "continue") return { event: "skipped", detail: cmd === "stop" ? "stopped by you" : "skipped by you", stop: cmd === "stop" };
        }
        continue;
      }
      // click
      const target = (snapshot.items || []).find((it) => it.key === decision.key);
      await status(tabId, `Pressing “${(target?.text || target?.label || decision.key).slice(0, 40)}”…`);
      await sleep(rand(decision.is_submit ? settings.before_submit : settings.step_delay));
      const before = new Set((await chrome.tabs.query({})).map((t) => t.id));
      const ok = await inTab(tabId, (k) => window.__jfAgent.click(k), [decision.key]);
      if (!ok) return { event: "skipped", detail: "the chosen button disappeared" };
      if (decision.is_submit) submitted = true;
      await sleep(600);
      // Followed a link that opened a new tab (e.g. "Apply on company site")? Continue there.
      const newTab = (await chrome.tabs.query({})).find((t) => !before.has(t.id) && t.openerTabId === tabId);
      if (newTab) {
        tabId = newTab.id;
        opened.push(tabId);
        applyingTab[tabId] = job.uid;
      }
      filled = false;
    }
    return { event: "skipped", detail: "too many steps" };
  } finally {
    for (const id of opened) delete applyingTab[id];
    if (mode !== "single") for (const id of opened) chrome.tabs.remove(id).catch(() => {});
  }
}

async function runQueue(start) {
  if (running) return { ok: false, error: "Auto-apply is already running" };
  running = true;
  // Chrome stops idle extension workers after ~30 s; a cheap API call every 20 s keeps this run alive.
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 20000);
  const { queue, settings, mode } = start;
  (async () => {
    try {
      for (let i = 0; i < queue.length; i++) {
        const job = queue[i];
        await report("job_start", job.uid);
        let result;
        try {
          result = await applyJob(job, settings, mode);
        } catch (err) {
          result = { event: "failed", detail: String(err.message || err).slice(0, 200) };
        }
        await report(result.event, job.uid, result.detail);
        if (result.stop) break;
        const { command } = await api("/api/autoapply/command").catch(() => ({ command: null }));
        if (command === "stop") break;
        if (i < queue.length - 1) await sleep(rand(settings.between_jobs));
      }
    } finally {
      clearInterval(keepAlive);
      running = false;
      await report("finished", null);
    }
  })();
  return { ok: true };
}

// ---------------------------------------------------------------- WhatsApp outreach (WhatsApp Web)
// Opens each chat with the message pre-typed (web.whatsapp.com/send?phone=…&text=…) in YOUR logged-in
// WhatsApp Web and presses Send. One tab is reused; there is a pause between messages.

let waRunning = false;

function waPageState() {
  const text = document.body ? document.body.innerText : "";
  if (/phone number shared via url is invalid|isn.t on whatsapp/i.test(text)) return { state: "invalid" };
  const useHere = [...document.querySelectorAll("button, [role=button]")].find((b) => /^use here$/i.test(b.innerText.trim()));
  if (useHere) { useHere.click(); return { state: "loading" }; }
  if (document.querySelector('canvas[aria-label*="scan" i], [data-ref] canvas') || /scan (the|this) qr|log ?in with phone number|steps to log in/i.test(text))
    return { state: "login" };
  const icon = document.querySelector('[data-icon="send"], [data-icon="wds-ic-send-filled"], [data-icon*="send" i]');
  const btn = (icon && icon.closest('button, [role="button"]')) || document.querySelector('footer button[aria-label="Send"], button[aria-label="Send"]');
  if (btn) return { state: "ready" };
  return { state: "loading" };
}

function waPressSend() {
  const icon = document.querySelector('[data-icon="send"], [data-icon="wds-ic-send-filled"], [data-icon*="send" i]');
  const btn = (icon && icon.closest('button, [role="button"]')) || document.querySelector('footer button[aria-label="Send"], button[aria-label="Send"]');
  if (!btn) return false;
  btn.click();
  return true;
}

function waComposerEmpty() {
  const box = document.querySelector('footer [contenteditable="true"]');
  return !box || !box.innerText.trim();
}

async function waSendOne(tabId, item) {
  const url = `https://web.whatsapp.com/send?phone=${item.phone}&text=${encodeURIComponent(item.text)}&type=phone_number&app_absent=0`;
  await chrome.tabs.update(tabId, { url });
  await sleep(3000);
  const start = Date.now();
  let askedLogin = false;
  while (Date.now() - start < (askedLogin ? 240000 : 75000)) {
    if (!(await chrome.tabs.get(tabId).catch(() => null))) return { ok: false, error: "The WhatsApp tab was closed" };
    await waitForLoad(tabId, 30000).catch(() => null);
    const st = (await inTab(tabId, waPageState).catch(() => null)) || { state: "loading" };
    if (st.state === "invalid") return { ok: false, error: "number is not on WhatsApp" };
    if (st.state === "login" && !askedLogin) {
      askedLogin = true;
      await chrome.tabs.update(tabId, { active: true });
      await inTab(tabId, () => alert("FresherPilot: scan the QR code to log in to WhatsApp Web. Sending continues by itself after you log in.")).catch(() => {});
    }
    if (st.state === "ready") {
      await sleep(800 + Math.random() * 1200);
      if (!(await inTab(tabId, waPressSend).catch(() => false))) continue;
      for (let i = 0; i < 10; i++) {
        await sleep(700);
        if (await inTab(tabId, waComposerEmpty).catch(() => false)) return { ok: true };
      }
      return { ok: false, error: "pressed Send but the message didn't go out" };
    }
    await sleep(1500);
  }
  return { ok: false, error: askedLogin ? "WhatsApp Web login wasn't completed" : "WhatsApp Web didn't load the chat" };
}

async function runWhatsApp({ items, gap }) {
  if (waRunning) return { ok: false, error: "WhatsApp messages are already being sent" };
  if (!items?.length) return { ok: true, count: 0 };
  waRunning = true;
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 20000);
  (async () => {
    let tabId;
    try {
      const [existing] = await chrome.tabs.query({ url: "https://web.whatsapp.com/*" });
      tabId = existing ? existing.id : (await chrome.tabs.create({ url: "https://web.whatsapp.com/", active: false })).id;
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        let r;
        try {
          r = await waSendOne(tabId, item);
        } catch (err) {
          r = { ok: false, error: String(err.message || err).slice(0, 200) };
        }
        await api("/api/outreach/event", {
          method: "POST",
          body: JSON.stringify({ uid: item.uid, channel: "whatsapp", to: item.phone, ok: r.ok, error: r.error || "" }),
        }).catch(() => {});
        if (/tab was closed/i.test(r.error || "")) break;
        if (i < items.length - 1) await sleep(rand(gap || [25, 45]));
      }
    } finally {
      clearInterval(keepAlive);
      waRunning = false;
    }
  })();
  return { ok: true, count: items.length };
}

// ---------------------------------------------------------------- Naukri sweep
// Naukri has no free API, so the app's Naukri coverage comes from here when Apify credit runs out:
// open the user's Naukri searches in one background tab of this browser (a normal page visit, at a
// human pace), read the job cards on each results page and send them to the app.

let naukriRunning = false;

// Runs inside the Naukri results page.
function readNaukriCards() {
  const out = [];
  const seen = new Set();
  const pick = (el, sels) => {
    for (const s of sels) {
      const x = el.querySelector(s);
      if (x && x.textContent.trim()) return x.textContent.trim();
    }
    return "";
  };
  let cards = [...document.querySelectorAll(".srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple")];
  if (!cards.length) cards = [...document.querySelectorAll('a[href*="/job-listings-"]')].map((a) => a.closest("article, li, div") || a);
  for (const c of cards) {
    const a = c.matches && c.matches('a[href*="/job-listings-"]') ? c : c.querySelector('a.title, a[href*="/job-listings-"]');
    if (!a) continue;
    const url = a.href.split("?")[0];
    if (seen.has(url)) continue;
    seen.add(url);
    out.push({
      id: c.getAttribute && (c.getAttribute("data-job-id") || ""),
      title: (a.getAttribute("title") || a.textContent || "").trim(),
      url,
      company: pick(c, ["a.comp-name", ".comp-name", ".companyInfo a", "[class*='comp-name']"]),
      experience: pick(c, [".expwdth", ".exp-wrap", ".experience", "[class*='exp-wrap']"]),
      location: pick(c, [".locWdth", ".loc-wrap", ".location", "[class*='loc-wrap']"]),
      salary: pick(c, [".sal-wrap", ".salary", "[class*='sal-wrap']"]),
      posted: pick(c, [".job-post-day", "[class*='post-day']"]),
      description: pick(c, [".job-desc", ".job-description", "[class*='job-desc']"]),
      skills: [...c.querySelectorAll(".tags-gt li, ul.tags li")].map((li) => li.textContent.trim()).filter(Boolean),
    });
  }
  const head = `${document.title} ${(document.body?.innerText || "").slice(0, 600)}`;
  return {
    jobs: out,
    blocked: /captcha|access denied|are you a robot|unusual traffic/i.test(head),
    empty: /no (matching )?jobs? found|didn.t find any jobs/i.test((document.body?.innerText || "").slice(0, 4000)),
  };
}

async function readNaukriPage(tabId, url) {
  await chrome.tabs.update(tabId, { url });
  await waitForLoad(tabId, 30000).catch(() => null);
  for (let i = 0; i < 20; i++) { // the results render after the page loads
    await sleep(1000);
    const r = await inTab(tabId, readNaukriCards).catch(() => null);
    if (r && (r.jobs.length || r.blocked || r.empty)) return r;
  }
  return { jobs: [], blocked: false, empty: true };
}

async function runNaukriSweep() {
  if (naukriRunning) return { ok: true, already: true };
  const plan = await api("/api/naukri/sweep");
  naukriRunning = true;
  (async () => {
    let tabId = null;
    const total = { pages: 0, read: 0, added: 0 };
    try {
      tabId = (await chrome.tabs.create({ url: "about:blank", active: false })).id;
      for (const base of plan.urls) {
        let lastIds = "";
        for (let page = 1; page <= (plan.max_pages || 5); page++) {
          const url = page === 1 ? base : base.replace(/-jobs\?/, `-jobs-${page}?`);
          const r = await readNaukriPage(tabId, url);
          total.pages++;
          if (r.blocked) throw new Error("Naukri showed a check page; open naukri.com once in this browser, then fetch again");
          const ids = r.jobs.map((j) => j.url).join("|");
          if (!r.jobs.length || ids === lastIds) break; // past the last page
          lastIds = ids;
          total.read += r.jobs.length;
          const res = await api("/api/import_jobs", { method: "POST", body: JSON.stringify({ kind: "naukri", jobs: r.jobs }) }).catch(() => null);
          total.added += res?.added || 0;
          if (r.jobs.length < 15) break; // last page
          await sleep(rand(plan.gap_seconds || [4, 8]));
        }
        await sleep(rand(plan.gap_seconds || [4, 8]));
      }
      await chrome.storage.local.set({ naukriSweep: { at: Date.now(), ...total, error: null } });
    } catch (e) {
      await chrome.storage.local.set({ naukriSweep: { at: Date.now(), ...total, error: String(e.message || e) } });
    } finally {
      if (tabId) chrome.tabs.remove(tabId).catch(() => {});
      naukriRunning = false;
    }
  })();
  return { ok: true, started: true, searches: plan.urls.length };
}

// ---------------------------------------------------------------- messages

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case "jf-autofill": {
        const uid = sender.tab ? applyingTab[sender.tab.id] : undefined;
        return api("/api/autofill", { method: "POST", body: JSON.stringify({ ...msg.payload, uid: uid || "" }) });
      }
      case "jf-resume":
        return resumeBase64();
      case "jf-fill-tab":
        return fillTab(msg.tabId, msg.overwrite);
      case "jf-status":
        return api("/api/profile");
      case "jf-applied":
        return api("/api/application", { method: "POST", body: JSON.stringify({ uid: msg.uid, status: "applied" }) });
      case "jf-server":
        return { server: await serverUrl() };
      case "jf-autoapply-start": // from the app page (via bridge.js)
        return runQueue(msg.start);
      case "jf-set-token": // from the app page: the signed-in user's token
        await chrome.storage.local.set({ token: msg.token });
        return { ok: true };
      case "jf-ping":
        return { running, waRunning, naukriRunning, naukriSweep: (await chrome.storage.local.get("naukriSweep")).naukriSweep || null };
      case "jf-naukri-sweep": // from the app page: read Naukri searches in this browser (free)
        return runNaukriSweep();
      case "jf-whatsapp-send": // from the app page: [{uid, phone, text}]
        return runWhatsApp(msg);
      case "jf-command": // Continue / Skip / Stop from the panel in the job tab
        return api("/api/autoapply/command", { method: "POST", body: JSON.stringify({ action: msg.action }) });
      default:
        return null;
    }
  })()
    .then((data) => sendResponse({ ok: true, data }))
    .catch((err) => sendResponse({ ok: false, error: String(err.message || err) }));
  return true;
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "fill-page") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) fillTab(tab.id, false).catch(() => {});
});
