// Runs on the FresherPilot app page (localhost, or the address set in the popup). Lets the app start auto-apply in this browser.
document.documentElement.dataset.jobfinderExtension = chrome.runtime.getManifest().version;

function toBackground(message, reply) {
  try {
    chrome.runtime.sendMessage(message, (res) => {
      const err = chrome.runtime.lastError;
      reply(err ? { ok: false, error: err.message } : res);
    });
  } catch (e) {
    // The extension was reloaded after this page opened: the page needs a refresh.
    reply({ ok: false, error: "Extension was reloaded. Refresh this page (F5)." });
  }
}

window.addEventListener("message", (event) => {
  if (event.source !== window || !event.data || event.data.__jf !== "to-extension") return;
  const { id, message } = event.data;
  toBackground(message, (response) => window.postMessage({ __jf: "from-extension", id, response }, "*"));
});

// Give the extension the signed-in user's token from whichever app page is open
// (tokens are reset whenever the app restarts).
fetch("/api/me", { credentials: "same-origin" })
  .then((r) => (r.ok ? r.json() : null))
  .then((me) => me && me.extension_token && toBackground({ type: "jf-set-token", token: me.extension_token }, () => {}))
  .catch(() => {});
