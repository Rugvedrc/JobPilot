// Shared by the onboarding and account pages: API keys.
const $ = (id) => document.getElementById(id);
const KEY_FIELDS = ["GROQ_API_KEY", "APIFY_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"];

function msg(id, text, bad = false) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.style.color = bad ? "var(--bad)" : "var(--muted)";
}

async function loadKeys() {
  const res = await fetch("/api/me");
  if (res.status === 401) return (location.href = "/login");
  const me = await res.json();
  for (const k of KEY_FIELDS) {
    const el = $(k);
    if (el) el.placeholder = me.keys[k] ? "saved ✓ (type to replace, or leave empty)" : el.placeholder;
  }
  return me;
}

document.addEventListener("click", async (e) => {
  if (e.target.id !== "saveKeys") return;
  const body = {};
  for (const k of KEY_FIELDS) if ($(k) && $(k).value.trim()) body[k] = $(k).value.trim();
  if (!Object.keys(body).length) return msg("keysMsg", "Nothing to save.", true);
  const res = await fetch("/api/account/keys", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) return msg("keysMsg", "Couldn't save the keys.", true);
  for (const k of KEY_FIELDS) if ($(k)) $(k).value = "";
  await loadKeys();
  msg("keysMsg", "Saved (encrypted) ✓");
});
