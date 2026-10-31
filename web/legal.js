// Fills the operator name, contact email and version on the Terms / Privacy pages (from .env).
fetch("/api/auth/state")
  .then((r) => r.json())
  .then((s) => {
    for (const el of document.querySelectorAll(".v-operator")) el.textContent = s.operator;
    for (const el of document.querySelectorAll(".v-version")) el.textContent = s.terms_version;
    for (const el of document.querySelectorAll(".v-contact")) {
      el.textContent = s.contact || "the email given by the owner";
      if (s.contact) el.href = `mailto:${s.contact}`;
    }
  })
  .catch(() => {});
