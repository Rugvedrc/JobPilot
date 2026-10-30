// FresherPilot Autofill: scans the application form in this frame, asks the local app for
// answers, and fills them in. It never clicks Submit; the user reviews and submits.
(() => {
  if (window.__jobFinderFill) return; // already injected in this frame

  const FILLED = "2px solid #16a34a";
  const REVIEW = "2px solid #f59e0b";
  const SKIP_TYPES = new Set(["hidden", "submit", "button", "image", "reset", "password", "search", "range", "color"]);
  const RESUME_LABEL = /resume|résumé|\bcv\b|curriculum/i;

  const clean = (t) => String(t || "").replace(/\s+/g, " ").trim();
  const textOf = (node) => clean(node ? node.innerText || node.textContent : "");
  // The auto-apply runner provides window.__jfSend; inside the extension, chrome.runtime is used.
  const send = (msg) =>
    window.__jfSend
      ? window.__jfSend(msg).then((res) => {
          if (!res || !res.ok) throw new Error(res ? res.error : "No response");
          return res.data;
        })
      : new Promise((resolve, reject) =>
      chrome.runtime.sendMessage(msg, (res) => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        if (!res || !res.ok) return reject(new Error(res ? res.error : "No response from extension"));
        resolve(res.data);
      })
    );
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---------------------------------------------------------------- visibility / labels

  function isVisible(el) {
    if (!el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // Radios/checkboxes are often hidden behind styled labels: visible if they or their label are.
  function isUsable(el) {
    if (el.disabled || el.readOnly) return false;
    if (el.type === "file") return true;
    if (isVisible(el)) return true;
    if ((el.type === "radio" || el.type === "checkbox") && el.labels) return [...el.labels].some(isVisible);
    return false;
  }

  // Text of a container minus its form controls: usually the question being asked.
  function containerText(container) {
    const clone = container.cloneNode(true);
    clone.querySelectorAll("input,select,textarea,option,button,script,style,svg,[role=option],[role=listbox]").forEach((n) => n.remove());
    return clean(clone.textContent);
  }

  function nearbyQuestion(el, maxControls = 2) {
    let node = el;
    for (let i = 0; i < 6 && node; i++) {
      node = node.parentElement;
      if (!node || node === document.body) break;
      const controls = node.querySelectorAll("input:not([type=hidden]),select,textarea").length;
      if (controls > maxControls) break;
      const t = containerText(node);
      if (t.length >= 2) return t.slice(0, 300);
    }
    return "";
  }

  function labelFor(el) {
    const parts = [];
    if (el.labels && el.labels.length) parts.push([...el.labels].map(textOf).join(" "));
    const ids = el.getAttribute("aria-labelledby");
    if (ids) parts.push(ids.split(/\s+/).map((id) => textOf(document.getElementById(id))).join(" "));
    if (el.getAttribute("aria-label")) parts.push(el.getAttribute("aria-label"));
    let label = clean(parts.join(" "));
    if (!label) label = nearbyQuestion(el);
    if (!label) label = clean(el.placeholder || el.getAttribute("title") || el.name || el.id || "");
    return label.slice(0, 300);
  }

  function hintFor(el) {
    const ids = el.getAttribute("aria-describedby");
    return ids ? ids.split(/\s+/).map((id) => textOf(document.getElementById(id))).join(" ").slice(0, 300) : "";
  }

  function isRequired(el, label) {
    return el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(label);
  }

  // Question for a group of radios/checkboxes: legend, radiogroup label, or text around the group.
  function groupQuestion(els) {
    const first = els[0];
    const fieldset = first.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend && textOf(legend)) return textOf(legend).slice(0, 300);
    }
    const group = first.closest("[role=radiogroup],[role=group]");
    if (group) {
      const l = group.getAttribute("aria-label") || (group.getAttribute("aria-labelledby") || "").split(/\s+/).map((id) => textOf(document.getElementById(id))).join(" ");
      if (clean(l)) return clean(l).slice(0, 300);
    }
    // Smallest ancestor that contains every option, minus the option labels.
    let node = first.parentElement;
    while (node && node !== document.body && !els.every((e) => node.contains(e))) node = node.parentElement;
    for (let i = 0; i < 3 && node && node !== document.body; i++, node = node.parentElement) {
      const optionText = new Set(els.map(optionLabel));
      let t = containerText(node);
      optionText.forEach((o) => (t = t.replace(o, " ")));
      t = clean(t);
      if (t.length >= 2) return t.slice(0, 300);
    }
    return clean(first.name || "");
  }

  function optionLabel(el) {
    if (el.labels && el.labels.length) return textOf(el.labels[0]);
    if (el.getAttribute("aria-label")) return clean(el.getAttribute("aria-label"));
    const next = el.nextSibling;
    if (next && clean(next.textContent)) return clean(next.textContent);
    return textOf(el) || clean(el.value);
  }

  // ---------------------------------------------------------------- collecting fields

  let counter = 0;
  const registry = new Map(); // field id -> { type, els, field }

  function register(type, els, extra) {
    const id = `jf${++counter}`;
    els.forEach((e) => (e.dataset.jfId = id));
    const field = { id, type, ...extra };
    registry.set(id, { type, els, field });
    return field;
  }

  function isPlaceholderOption(opt, index) {
    return (index === 0 && (!opt.value || /select|choose|--|please/i.test(opt.textContent))) || !clean(opt.textContent);
  }

  function collect(overwrite) {
    registry.clear();
    document.querySelectorAll("[data-jf-group]").forEach((n) => delete n.dataset.jfGroup);
    const fields = [];
    const doneGroups = new Set();
    const controls = document.querySelectorAll("input, textarea, select, [role=radio], [role=checkbox], [role=combobox]:not(input)");

    for (const el of controls) {
      if (el.dataset.jfSkip) continue;
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (tag === "input" && SKIP_TYPES.has(type)) continue;

      // Native radios: one field per name.
      if (tag === "input" && type === "radio") {
        const key = `radio:${el.form ? [...document.forms].indexOf(el.form) : -1}:${el.name}`;
        if (!el.name || doneGroups.has(key)) continue;
        doneGroups.add(key);
        const els = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)].filter(isUsable);
        if (!els.length) continue;
        if (!overwrite && els.some((e) => e.checked)) continue;
        fields.push(register("radio", els, { label: groupQuestion(els), options: els.map(optionLabel), required: els.some((e) => e.required) }));
        continue;
      }

      // Native checkboxes: group by name when several share it.
      if (tag === "input" && type === "checkbox") {
        if (!isUsable(el)) continue;
        const same = el.name ? [...document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`)].filter(isUsable) : [el];
        if (same.length > 1) {
          const key = `cbg:${el.name}`;
          if (doneGroups.has(key)) continue;
          doneGroups.add(key);
          if (!overwrite && same.some((e) => e.checked)) continue;
          fields.push(register("checkbox_group", same, { label: groupQuestion(same), options: same.map(optionLabel) }));
        } else {
          if (!overwrite && el.checked) continue;
          fields.push(register("checkbox", [el], { label: labelFor(el) || optionLabel(el), required: el.required }));
        }
        continue;
      }

      // Custom ARIA radios (divs/buttons) grouped by their radiogroup.
      if (el.getAttribute("role") === "radio") {
        const group = el.closest("[role=radiogroup]") || el.parentElement;
        if (!group || group.dataset.jfGroup) continue;
        group.dataset.jfGroup = "1";
        const els = [...group.querySelectorAll("[role=radio]")].filter(isVisible);
        if (!els.length || (!overwrite && els.some((e) => e.getAttribute("aria-checked") === "true"))) continue;
        fields.push(register("radio", els, { label: groupQuestion(els), options: els.map((e) => textOf(e) || clean(e.getAttribute("aria-label"))) }));
        continue;
      }
      if (el.getAttribute("role") === "checkbox") {
        if (!isVisible(el) || (!overwrite && el.getAttribute("aria-checked") === "true")) continue;
        fields.push(register("checkbox", [el], { label: textOf(el) || nearbyQuestion(el) }));
        continue;
      }

      if (tag !== "input" && tag !== "textarea" && tag !== "select" && el.getAttribute("role") !== "combobox") continue;
      if (!isUsable(el)) continue;
      const label = labelFor(el);

      if (type === "file") {
        if (!RESUME_LABEL.test(`${label} ${el.name} ${el.id} ${el.accept}`)) continue;
        if (!overwrite && el.files && el.files.length) continue;
        fields.push(register("file", [el], { label }));
        continue;
      }

      if (tag === "select") {
        const opts = [...el.options].filter((o, i) => !isPlaceholderOption(o, i) && !o.disabled);
        if (!opts.length) continue;
        const current = el.selectedIndex > -1 && !isPlaceholderOption(el.options[el.selectedIndex], el.selectedIndex);
        if (!overwrite && current) continue;
        fields.push(register(el.multiple ? "checkbox_group" : "select", [el], {
          label, hint: hintFor(el), required: isRequired(el, label), options: opts.map((o) => clean(o.textContent)),
        }));
        continue;
      }

      // Text-like inputs, textareas and searchable dropdowns.
      const value = el.value !== undefined ? el.value : textOf(el);
      if (!overwrite && clean(value)) continue;
      const isCombo = el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list";
      let ftype = tag === "textarea" ? "textarea" : ["email", "tel", "url", "number", "date", "month"].includes(type) ? type : "text";
      if (isCombo) ftype = "combobox";
      if (ftype === "tel") ftype = "text";
      const field = { label, hint: hintFor(el), placeholder: clean(el.placeholder), required: isRequired(el, label) };
      const ml = parseInt(el.getAttribute("maxlength"), 10);
      if (ml > 0 && ml < 100000) field.maxlength = ml;
      if (isCombo) {
        const listId = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
        const list = listId && document.getElementById(listId);
        if (list) field.options = [...list.querySelectorAll("[role=option]")].map(textOf).filter(Boolean).slice(0, 80);
      }
      fields.push(register(ftype, [el], field));
    }
    return fields;
  }

  // ---------------------------------------------------------------- filling

  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    el.focus();
    if (setter) setter.call(el, value);
    else el.value = value;
    // React, Angular and Vue all listen to these.
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function isChecked(el) {
    return el.type === "radio" || el.type === "checkbox" ? el.checked : el.getAttribute("aria-checked") === "true";
  }

  async function chooseComboOption(el, value) {
    setNativeValue(el, value);
    el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "ArrowDown" }));
    for (let i = 0; i < 8; i++) {
      await sleep(150);
      const opts = [...document.querySelectorAll("[role=option]")].filter(isVisible);
      if (!opts.length) continue;
      const v = value.toLowerCase();
      const pick = opts.find((o) => textOf(o).toLowerCase() === v) || opts.find((o) => textOf(o).toLowerCase().includes(v)) || opts.find((o) => v.includes(textOf(o).toLowerCase()));
      if (pick) {
        pick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        pick.click();
        return true;
      }
    }
    return false;
  }

  async function attachResume(el) {
    const file = await send({ type: "jf-resume" });
    const bytes = Uint8Array.from(atob(file.base64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], file.name, { type: file.type }));
    el.files = dt.files;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function mark(entry, ok, note) {
    const target = entry.els.length > 1 ? entry.els[0].closest("fieldset,[role=radiogroup]") || entry.els[0].parentElement : entry.els[0];
    const box = target && (target.type === "radio" || target.type === "checkbox") && target.labels?.[0] ? target.labels[0] : target;
    if (!box) return;
    box.style.outline = ok ? FILLED : REVIEW;
    box.style.outlineOffset = "2px";
    box.title = ok ? "Filled by FresherPilot: please check" : `FresherPilot: needs your answer${note ? ` (${note})` : ""}`;
  }

  async function apply(entry, value) {
    const { type, els } = entry;
    if (type === "select" || (type === "checkbox_group" && els[0].tagName === "SELECT")) {
      const sel = els[0];
      const wanted = value.split(" || ").map((v) => v.trim().toLowerCase());
      let any = false;
      [...sel.options].forEach((o) => {
        if (wanted.includes(clean(o.textContent).toLowerCase())) {
          if (sel.multiple) o.selected = true;
          else sel.value = o.value;
          any = true;
        }
      });
      if (any) {
        sel.dispatchEvent(new Event("input", { bubbles: true }));
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return any;
    }
    if (type === "radio" || type === "checkbox_group") {
      const wanted = value.split(" || ").map((v) => v.trim().toLowerCase());
      let any = false;
      for (const el of els) {
        const text = (el.getAttribute("role") ? textOf(el) || el.getAttribute("aria-label") : optionLabel(el)).toLowerCase();
        if (wanted.includes(clean(text)) && !isChecked(el)) {
          el.click();
          any = true;
        } else if (wanted.includes(clean(text))) any = true;
      }
      return any;
    }
    if (type === "checkbox") {
      const want = value === "true";
      if (isChecked(els[0]) !== want) els[0].click();
      return true;
    }
    if (type === "combobox") return chooseComboOption(els[0], value);
    setNativeValue(els[0], value);
    return true;
  }

  function toast(text) {
    if (window.top !== window) return;
    const box = document.createElement("div");
    box.textContent = text;
    Object.assign(box.style, {
      position: "fixed", right: "16px", bottom: "16px", zIndex: 2147483647, maxWidth: "360px",
      background: "#15202b", color: "#fff", padding: "12px 14px", borderRadius: "10px",
      font: "14px/1.4 system-ui, sans-serif", boxShadow: "0 6px 24px rgba(0,0,0,.25)",
    });
    document.body.appendChild(box);
    setTimeout(() => box.remove(), 9000);
  }

  // ---------------------------------------------------------------- entry point

  window.__jobFinderFill = async (overwrite) => {
    const summary = { found: 0, filled: 0, review: 0, files: 0, job: null, error: null, missing_profile: [] };
    try {
      const fields = collect(overwrite);
      summary.found = fields.length;
      if (!fields.length) return summary;

      const fileFields = fields.filter((f) => f.type === "file");
      for (const f of fileFields) {
        try {
          await attachResume(registry.get(f.id).els[0]);
          summary.files++;
          mark(registry.get(f.id), true);
        } catch (e) {
          mark(registry.get(f.id), false, "attach your resume");
          summary.review++;
        }
      }

      const askFields = fields.filter((f) => f.type !== "file");
      if (!askFields.length) return summary;
      const res = await send({
        type: "jf-autofill",
        payload: { url: location.href, title: document.title, page_text: textOf(document.body).slice(0, 8000), fields: askFields },
      });
      summary.job = res.job;
      summary.missing_profile = res.missing_profile || [];
      for (const a of res.answers || []) {
        const entry = registry.get(a.id);
        if (!entry) continue;
        let ok = false;
        if (a.value) {
          try {
            ok = await apply(entry, a.value);
          } catch (e) {
            ok = false;
          }
        }
        if (ok && !a.needs_review) summary.filled++;
        else summary.review++;
        mark(entry, ok && !a.needs_review, a.value ? "please check this answer" : "no answer in your profile");
      }
      toast(`FresherPilot filled ${summary.filled + summary.files} field(s). ${summary.review} need your review (orange outline). Nothing was submitted.`);
    } catch (e) {
      summary.error = String(e.message || e);
    }
    return summary;
  };
})();
