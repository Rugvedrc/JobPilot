// Auto-apply helpers injected into the job tab: readiness detection, challenge detection,
// Vimium-style hints, clicking, and a small status panel with Continue / Skip / Stop.
(() => {
  if (window.__jfAgent) return;

  const visible = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0" && r.bottom > 0 && r.top < innerHeight * 1.6 && !e.disabled;
  };
  // Rendered and usable anywhere on the page (not just on screen), so a Submit far below is still found.
  const rendered = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0" && !e.disabled;
  };
  const clean = (t) => String(t || "").replace(/\s+/g, " ").trim();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------------------------------------------------------------- readiness

  // Page changes and network activity, recorded continuously.
  let lastMutation = performance.now();
  new MutationObserver((records) => {
    // Ignore our own hint badges / status panel.
    if (records.every((r) => [...r.addedNodes, ...r.removedNodes].every((n) => n.classList?.contains("__jfhint") || n.id === "__jf_status"))) return;
    lastMutation = performance.now();
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, characterData: true });
  let lastRequest = performance.now();
  try {
    new PerformanceObserver(() => (lastRequest = performance.now())).observe({ type: "resource", buffered: false });
  } catch {}

  const BUSY_TEXT = /^(loading|please wait|just a moment|submitting|processing|uploading)\b|checking your browser|loading\.\.\.|loading…/i;

  // Why the page is still busy, or "" when it looks ready.
  function busyReason() {
    if (document.readyState !== "complete") return "page loading";
    if (blockedState() === "challenge-loading") return "security check running";
    if ([...document.querySelectorAll('[aria-busy="true"]')].some(visible)) return "page says it is busy";
    // Step indicators ("step 4 of 4", 100%) are determinate and static: not loading. Only an
    // indeterminate bar (no value) means "wait"; a moving bar also keeps the DOM changing.
    const indeterminate = [...document.querySelectorAll("[role=progressbar], progress")].find(
      (e) => visible(e) && (e.tagName === "PROGRESS" ? !e.hasAttribute("value") : !e.hasAttribute("aria-valuenow"))
    );
    if (indeterminate) return "loading bar";
    // A spinner/skeleton only counts while it is actually animating.
    const animating = (e) => {
      try {
        return e.getAnimations({ subtree: true }).some((a) => a.playState === "running");
      } catch {
        return getComputedStyle(e).animationName !== "none";
      }
    };
    const spinner = [...document.querySelectorAll('[class*="spinner" i], [class*="loader" i], [class*="loading" i], [class*="skeleton" i], [class*="shimmer" i]')]
      .find((e) => visible(e) && e.getBoundingClientRect().width < innerWidth * 0.9 && animating(e));
    if (spinner) return "spinner";
    const dialog = currentDialog();
    const text = clean((dialog || document.body).innerText).slice(0, 300);
    if (BUSY_TEXT.test(text)) return "loading text";
    return "";
  }

  // Wait until the page is loaded, nothing is spinning, and the DOM + network have been quiet a moment.
  async function waitReady(timeout = 30000, quietMs = 800) {
    const start = performance.now();
    let reason = "";
    while (performance.now() - start < timeout) {
      reason = busyReason();
      const now = performance.now();
      if (!reason && now - lastMutation > quietMs && now - lastRequest > 500) return { ready: true, waited: Math.round(now - start) };
      await sleep(200);
    }
    return { ready: false, waited: Math.round(performance.now() - start), reason: reason || "page kept changing" };
  }

  // ---------------------------------------------------------------- security checks / login

  // "challenge" = a human must act, "challenge-loading" = an automatic check still running, "login", or "".
  function blockedState() {
    const frames = [...document.querySelectorAll("iframe")].filter(visible);
    const challengeFrame = frames.find((f) => /challenges\.cloudflare\.com|hcaptcha\.com|recaptcha\/api2\/(bframe|anchor)|arkoselabs|funcaptcha/i.test(f.src || "") && f.getBoundingClientRect().width > 60);
    const text = clean(document.body ? document.body.innerText : "").slice(0, 2000);
    if (/just a moment|checking your browser|checking if the site connection is secure/i.test(document.title + " " + text) && !challengeFrame) return "challenge-loading";
    if (challengeFrame || /verify you are (a )?human|are you a robot|press (&|and) hold|complete the security check/i.test(text)) return "challenge";
    if (/\/(login|signin|sign-in|authwall)\b|nlogin|secure\.indeed\.com\/auth|accounts\.google/i.test(location.href)) return "login";
    return "";
  }

  // ---------------------------------------------------------------- hints

  function currentDialog() {
    const dialogs = [...document.querySelectorAll("[role=dialog],[aria-modal=true],.artdeco-modal,[class*=modal],[class*=Drawer]")].filter(visible);
    return dialogs[dialogs.length - 1] || null;
  }

  function snapshot() {
    document.querySelectorAll(".__jfhint").forEach((n) => n.remove());
    document.querySelectorAll("[data-jf-hint]").forEach((n) => n.removeAttribute("data-jf-hint"));
    const sel = "a[href],button,[role=button],[role=link],input[type=submit],input[type=button],[role=menuitem],[role=tab],[role=radio],[role=option],summary";
    const all = [...document.querySelectorAll(sel)].filter((e) => rendered(e) && !e.closest("#__jf_status"));
    const dialog = currentDialog();
    // Real buttons (Next / Submit...) first, then links; dialog contents before the rest of the page.
    const isButton = (e) => e.tagName !== "A";
    const order = (list) => list.filter(isButton).concat(list.filter((e) => !isButton(e)));
    const pool = dialog ? order(all.filter((e) => dialog.contains(e))).concat(order(all.filter((e) => !dialog.contains(e))).slice(0, 40)) : order(all);
    const letters = "asdfghjklqwertyuiopzxcvbnm";
    const items = [];
    pool.slice(0, 160).forEach((e, i) => {
      const key = letters[Math.floor(i / 26) % 26] + letters[i % 26];
      e.setAttribute("data-jf-hint", key);
      const r = e.getBoundingClientRect();
      const b = document.createElement("div");
      b.className = "__jfhint";
      b.textContent = key;
      Object.assign(b.style, {
        position: "fixed", left: `${Math.max(0, r.left)}px`, top: `${Math.max(0, r.top)}px`, background: "#fde047",
        color: "#000", font: "bold 11px monospace", padding: "0 3px", zIndex: 2147483646, borderRadius: "3px", pointerEvents: "none",
      });
      document.body.appendChild(b);
      items.push({
        key, text: clean(e.innerText || e.value).slice(0, 80), label: clean(e.getAttribute("aria-label")).slice(0, 80),
        tag: e.tagName.toLowerCase(), href: (e.getAttribute("href") || "").slice(0, 100), in_dialog: !!(dialog && dialog.contains(e)),
        below: r.top > innerHeight, // further down the page (clicking scrolls to it)
      });
    });
    const root = dialog || document.body;
    const fields = [...root.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea")].filter((f) => rendered(f) && !f.closest("#__jf_status"));
    const empty = fields.filter((f) => (f.type === "radio" || f.type === "checkbox" ? false : !clean(f.value))).length;
    return {
      url: location.href, title: document.title, items, dialog: !!dialog, fields: fields.length, empty_fields: empty,
      blocked: blockedState(), text: clean(root.innerText).slice(0, 2500), full_text: clean(document.body.innerText).slice(0, 8000),
    };
  }

  function click(key) {
    const el = document.querySelector(`[data-jf-hint="${key}"]`);
    if (!el) return false;
    el.scrollIntoView({ block: "center" });
    document.querySelectorAll(".__jfhint").forEach((n) => n.remove());
    if (el.tagName === "A" && el.target === "_blank") el.removeAttribute("target"); // keep the flow in this tab
    el.click();
    lastMutation = performance.now();
    return true;
  }

  function emptyRequired() {
    const root = currentDialog() || document;
    const out = [];
    const shown = (n) => n && n.getClientRects().length > 0 && getComputedStyle(n).visibility !== "hidden";
    root.querySelectorAll("input, select, textarea").forEach((el) => {
      if (el.closest("#__jf_status")) return;
      const req = el.required || el.getAttribute("aria-required") === "true";
      if (!req || el.disabled || el.type === "hidden" || el.type === "file") return;
      if (!shown(el) && !(el.labels && [...el.labels].some(shown))) return;
      if (el.type === "radio") {
        if (!root.querySelector(`input[type=radio][name="${CSS.escape(el.name)}"]:checked`)) out.push(el.name);
      } else if (el.type === "checkbox") {
        if (!el.checked) out.push((el.labels && el.labels[0] && clean(el.labels[0].innerText)) || el.name || "checkbox");
      } else if (!clean(el.value)) {
        out.push(clean((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute("aria-label") || el.name || el.id).slice(0, 60));
      }
    });
    return [...new Set(out)];
  }

  // ---------------------------------------------------------------- status panel (in the job tab)

  function showStatus(text, waiting) {
    let host = document.getElementById("__jf_status");
    if (!host) {
      host = document.createElement("div");
      host.id = "__jf_status";
      Object.assign(host.style, { position: "fixed", right: "16px", bottom: "16px", zIndex: 2147483647 });
      host.attachShadow({ mode: "open" });
      document.documentElement.appendChild(host);
    }
    const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
    host.shadowRoot.innerHTML = `
      <style>
        .box{font:13px/1.4 system-ui,"Segoe UI",sans-serif;background:#15202b;color:#fff;border-radius:12px;padding:10px 12px;
             box-shadow:0 8px 28px rgba(0,0,0,.3);max-width:320px;display:grid;gap:8px;border-left:4px solid ${waiting ? "#f59e0b" : "#22c55e"}}
        b{font-weight:700}.row{display:flex;gap:6px;flex-wrap:wrap}
        button{font:inherit;font-weight:600;border:0;border-radius:999px;padding:5px 12px;cursor:pointer;background:#334155;color:#fff}
        button.go{background:#16a34a}
      </style>
      <div class="box"><div><b>FresherPilot</b> · ${esc(text)}</div>
        <div class="row">${waiting ? '<button class="go" data-a="continue">Continue</button><button data-a="skip">Skip job</button>' : ""}<button data-a="stop">Stop</button></div></div>`;
    host.shadowRoot.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        chrome.runtime.sendMessage({ type: "jf-command", action: b.dataset.a });
        showStatus(b.dataset.a === "continue" ? "Continuing…" : b.dataset.a === "skip" ? "Skipping this job…" : "Stopping…", false);
      })
    );
  }

  // Scroll down the page and back so content that only appears when scrolled into view
  // (lazy-loaded Apply buttons, e.g. on Indeed) is created before the page is read.
  async function scrollThrough() {
    const scroller = currentDialog() || document.scrollingElement || document.documentElement;
    const startY = scroller.scrollTop;
    for (let i = 0; i < 12; i++) {
      const before = scroller.scrollTop;
      scroller.scrollBy({ top: Math.max(400, innerHeight * 0.8), behavior: "instant" });
      await sleep(250);
      if (scroller.scrollTop === before) break; // reached the bottom
    }
    await sleep(400);
    scroller.scrollTo({ top: startY, behavior: "instant" });
    await sleep(200);
    return true;
  }

  window.__jfAgent = { snapshot, click, emptyRequired, waitReady, blockedState, showStatus, scrollThrough };
})();
