const $ = (id) => document.getElementById(id);
const send = (msg) =>
  new Promise((resolve) => chrome.runtime.sendMessage(msg, (res) => resolve(res || { ok: false, error: "No response" })));

const PROFILE_LABELS = {
  "personal.pincode": "pincode",
  "personal.date_of_birth": "date of birth",
  "personal.gender": "gender",
  "job_preferences.expected_ctc_lpa": "expected CTC",
};

let server = "http://localhost:8000";
let matchedJob = null;

async function checkServer() {
  server = (await send({ type: "jf-server" })).data?.server || server;
  $("serverUrl").value = server;
  const res = await send({ type: "jf-status" });
  if (!res.ok) {
    const msg = /sign in/i.test(res.error || "") ? res.error : `Can't reach the FresherPilot app at ${server}. Start it with <b>python main.py</b>.`;
    $("server").innerHTML = `<span class="err">${msg}</span>`;
    $("fill").disabled = true;
    return;
  }
  $("server").textContent = "Connected to FresherPilot.";
  $("fill").disabled = false;
  showMissing(res.data.missing || []);
}

function showMissing(missing) {
  $("missing").hidden = !missing.length;
  $("missing").textContent = missing.length
    ? `Add your ${missing.map((m) => PROFILE_LABELS[m] || m).join(", ")} in your profile, so forms asking for them can be filled.`
    : "";
}

function showResult(s) {
  const box = $("result");
  box.hidden = false;
  if (s.errors?.length && !s.found) {
    box.innerHTML = `<span class="err">${s.errors[0]}</span>`;
    return;
  }
  if (!s.found) {
    box.innerHTML = `<span class="warn">No empty form fields found on this page.</span><span class="muted">Open the application form (click Apply on the job page) and try again. Tick "replace" to refill fields.</span>`;
    return;
  }
  box.innerHTML = `
    <span class="ok">Filled ${s.filled + s.files} of ${s.found} field(s)${s.files ? ` (incl. resume upload)` : ""}.</span>
    ${s.review ? `<span class="warn">${s.review} need your review (orange outline).</span>` : ""}
    ${s.job ? `<span class="muted">Matched job: ${s.job.title} · ${s.job.company || ""}</span>` : `<span class="muted">Job not in your FresherPilot list; answers used this page's text.</span>`}
    ${s.errors?.length ? `<span class="err">${s.errors[0]}</span>` : ""}`;
  if (s.missing_profile?.length) showMissing(s.missing_profile);
  matchedJob = s.job;
  $("applied").hidden = !matchedJob;
  const ins = s.job?.instructions;
  if (ins) {
    const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const extra = esc([...(ins.links || []), ...(ins.emails || [])].join(", "));
    box.insertAdjacentHTML(
      "afterbegin",
      `<div class="notice" style="display:block">⚠ ${esc(ins.summary || "This job has special application instructions.")}${extra ? `<br>${extra}` : ""}${ins.subject_line ? `<br>Subject: ${esc(ins.subject_line)}` : ""}</div>`
    );
  }
}

$("fill").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  $("fill").disabled = true;
  $("fill").textContent = "Filling… (AI answers take ~10–30 s)";
  const res = await send({ type: "jf-fill-tab", tabId: tab.id, overwrite: $("overwrite").checked });
  $("fill").disabled = false;
  $("fill").textContent = "Fill this page";
  if (!res.ok) {
    $("result").hidden = false;
    $("result").innerHTML = `<span class="err">${res.error}</span><span class="muted">Some pages (Chrome Web Store, browser settings) can't be filled.</span>`;
    return;
  }
  showResult(res.data);
});

$("applied").addEventListener("click", async () => {
  if (!matchedJob) return;
  const res = await send({ type: "jf-applied", uid: matchedJob.uid });
  $("applied").textContent = res.ok ? "Marked as applied ✓" : "Couldn't save";
  $("applied").disabled = res.ok;
});

$("openApp").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: `${server}/` });
});
$("openProfile").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: `${server}/static/profile.html` });
});
$("serverUrl").addEventListener("change", async (e) => {
  await chrome.storage.local.set({ server: e.target.value.trim() || "http://localhost:8000" });
  checkServer();
});

checkServer();
