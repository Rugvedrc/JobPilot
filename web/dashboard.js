// Dashboard: what you did today / yesterday / this week, outreach, pipeline and recent applications.

const dash = { data: null, loading: null };
const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const dayOf = (iso) => new Date(`${iso}T00:00:00`);
const fmtDay = (iso) => {
  const d = dayOf(iso);
  return `${WEEKDAY[d.getDay()]} ${d.getDate()} ${MONTH[d.getMonth()]}`;
};

async function refreshDashboard() {
  if (dash.loading) return dash.loading;
  dash.loading = (async () => {
    try {
      const res = await fetch("/api/dashboard");
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      dash.data = await res.json();
      renderDashboard();
    } catch (err) {
      if (!dash.data) $("dash").innerHTML = `<p class="notice warn">Couldn't load the dashboard: ${escapeHtml(err.message)}</p>`;
    } finally {
      dash.loading = null;
    }
  })();
  return dash.loading;
}

function deltaText(now, before) {
  const d = now - before;
  if (!before && !now) return `<span class="muted">none yesterday either</span>`;
  if (d === 0) return `<span class="muted">same as yesterday (${before})</span>`;
  return `<span class="delta ${d > 0 ? "up" : "down"}">${d > 0 ? "▲" : "▼"} ${Math.abs(d)}</span> <span class="muted">vs yesterday (${before})</span>`;
}

function kpi(label, value, sub, extra = "") {
  return `<div class="kpi ${extra}"><span class="kpi-label">${label}</span><span class="kpi-value">${value}</span><span class="kpi-sub">${sub}</span></div>`;
}

// ---------------------------------------------------------------- charts (inline SVG)

function niceMax(v) {
  if (v <= 4) return 4;
  const step = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * step >= v) return m * step;
  return 10 * step;
}

// Stacked daily bars. keys: [{key, label, color}]
function barChart(el, series, keys) {
  const W = Math.max(320, Math.round(el.clientWidth || 560));
  const H = 210;
  const m = { l: 30, r: 8, t: 18, b: 26 };
  const iw = W - m.l - m.r;
  const ih = H - m.t - m.b;
  const totals = series.map((d) => keys.reduce((s, k) => s + (d[k.key] || 0), 0));
  const max = niceMax(Math.max(1, ...totals));
  const band = iw / series.length;
  const bw = Math.max(6, Math.min(30, band * 0.62));
  const y = (v) => m.t + ih - (v / max) * ih;
  const ticks = [0, max / 2, max].filter((v, i, a) => Number.isInteger(v) && a.indexOf(v) === i);
  const maxIdx = totals.indexOf(Math.max(...totals));
  const every = band < 36 ? 2 : 1;

  let svg = `<g class="grid">${ticks.map((t) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>`).join("")}</g>`;
  svg += `<g class="axis">${ticks.map((t) => `<text x="${m.l - 8}" y="${y(t) + 4}" text-anchor="end">${t}</text>`).join("")}</g>`;
  series.forEach((d, i) => {
    const cx = m.l + band * i + band / 2;
    const x = cx - bw / 2;
    let acc = 0;
    const segs = keys.map((k) => ({ k, v: d[k.key] || 0 })).filter((s) => s.v > 0);
    segs.forEach((s, j) => {
      const y0 = y(acc);
      const y1 = y(acc + s.v);
      acc += s.v;
      const top = j === segs.length - 1;
      const gap = j > 0 ? 2 : 0; // 2px surface gap between stacked segments
      const h = Math.max(1, y0 - y1 - gap);
      const yy = y1;
      if (top) {
        const r = Math.min(4, h, bw / 2);
        svg += `<path fill="${s.k.color}" d="M${x},${yy + h} V${yy + r} Q${x},${yy} ${x + r},${yy} H${x + bw - r} Q${x + bw},${yy} ${x + bw},${yy + r} V${yy + h} Z"/>`;
      } else {
        svg += `<rect fill="${s.k.color}" x="${x}" y="${yy}" width="${bw}" height="${h}"/>`;
      }
    });
    const isToday = i === series.length - 1;
    if (totals[i] && (isToday || i === maxIdx)) svg += `<text class="val" x="${cx}" y="${y(totals[i]) - 5}" text-anchor="middle">${totals[i]}</text>`;
    if (isToday || (series.length - 1 - i) % every === 0) {
      const dd = dayOf(d.date);
      svg += `<text class="lbl" x="${cx}" y="${H - 8}" text-anchor="middle">${isToday ? "Today" : `${WEEKDAY[dd.getDay()][0]} ${dd.getDate()}`}</text>`;
    }
    svg += `<rect class="hit" data-i="${i}" x="${m.l + band * i}" y="${m.t}" width="${band}" height="${ih}" rx="4"/>`;
  });
  svg += `<line class="base" x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}"/>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escapeHtml(keys.map((k) => k.label).join(" and "))} per day, last ${series.length} days">${svg}</svg><div class="tip" hidden></div>`;

  const tip = el.querySelector(".tip");
  el.querySelectorAll(".hit").forEach((r) => {
    r.addEventListener("mouseenter", () => {
      const i = Number(r.dataset.i);
      const d = series[i];
      const rows = keys.map((k) => `${keys.length > 1 ? `<i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${k.color};margin-right:5px"></i>` : ""}${k.label}: <b>${d[k.key] || 0}</b>`);
      tip.innerHTML = `${fmtDay(d.date)}<br>${rows.join("<br>")}`;
      const box = el.getBoundingClientRect();
      const rb = r.getBoundingClientRect();
      tip.style.left = `${rb.left - box.left + rb.width / 2}px`;
      tip.style.top = `${(y(totals[i]) / H) * box.height - 8}px`;
      tip.hidden = false;
    });
    r.addEventListener("mouseleave", () => (tip.hidden = true));
  });
}

function dataTable(series, keys) {
  const head = `<tr><th>Day</th>${keys.map((k) => `<th>${k.label}</th>`).join("")}</tr>`;
  const rows = series
    .slice()
    .reverse()
    .map((d) => `<tr><td>${fmtDay(d.date)}</td>${keys.map((k) => `<td>${d[k.key] || 0}</td>`).join("")}</tr>`)
    .join("");
  return `<details class="datatable"><summary>Show as table</summary><table>${head}${rows}</table></details>`;
}

function hbars(entries, labels = {}) {
  const max = Math.max(1, ...entries.map(([, v]) => v));
  if (!entries.some(([, v]) => v)) return `<p class="empty-state">Nothing yet.</p>`;
  return `<div class="hbars">${entries
    .map(([k, v]) => `<div class="hbar"><span>${escapeHtml(labels[k] || k)}</span><div class="hbar-track"><div class="hbar-fill" style="width:${(v / max) * 100}%"></div></div><b>${v}</b></div>`)
    .join("")}</div>`;
}

// ---------------------------------------------------------------- render

function renderDashboard() {
  const d = dash.data;
  if (!d) return;
  const k = d.kpi;
  const css = getComputedStyle(document.documentElement);
  const c1 = css.getPropertyValue("--viz-1").trim();
  const c2 = css.getPropertyValue("--viz-2").trim();
  const now = new Date();
  const greet = now.getHours() < 12 ? "Good morning" : now.getHours() < 17 ? "Good afternoon" : "Good evening";

  const recent = d.recent.length
    ? `<div class="table-wrap"><table class="apps"><thead><tr><th>Job</th><th>Board</th><th>Applied</th><th>Outreach</th><th>Status</th></tr></thead><tbody>${d.recent
        .map((a) => {
          const reach = [a.outreach.email && `✉ ${a.outreach.email}`, a.outreach.whatsapp && `WhatsApp ${a.outreach.whatsapp}`].filter(Boolean).join(" · ");
          const title = a.url ? `<a href="${escapeHtml(safeUrl(a.url))}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>` : escapeHtml(a.title);
          return `<tr>
            <td><div class="t">${title}</div><div class="muted small">${escapeHtml(a.company)}</div></td>
            <td><span class="chip source">${escapeHtml(a.source)}</span></td>
            <td class="muted">${escapeHtml(formatWhen(a.applied_at))}</td>
            <td class="muted small">${reach || "–"}</td>
            <td>${statusSelect({ uid: a.uid, application: { status: a.status } })}</td>
          </tr>`;
        })
        .join("")}</tbody></table></div>`
    : `<p class="empty-state">No applications recorded yet. In <strong>Jobs</strong>, press <strong>Mark applied</strong> on a job after applying (auto-apply records them by itself).</p>`;

  $("dash").innerHTML = `
    <div class="dash-head">
      <div><h2>${greet}, ${escapeHtml(dash.firstName || "there")}</h2><p>${fmtDay(d.today)}</p></div>
      ${k.new_matches ? `<button type="button" class="btn primary" data-goto="jobs">Review ${k.new_matches} new match${k.new_matches === 1 ? "" : "es"} →</button>` : ""}
    </div>

    <div class="kpis">
      ${kpi("Applied today", k.applied_today, deltaText(k.applied_today, k.applied_yesterday), "hero")}
      ${kpi("Last 7 days", k.applied_week, `${k.applied_total} applications in total`)}
      ${kpi("Outreach today", k.emails_today + k.whatsapp_today, `${k.emails_today} email${k.emails_today === 1 ? "" : "s"} · ${k.whatsapp_today} WhatsApp`)}
      <button type="button" class="kpi" data-goto="jobs"><span class="kpi-label">New matches</span><span class="kpi-value">${k.new_matches}</span><span class="kpi-sub">not visited yet${k.pending_ai ? ` · ${k.pending_ai} pending AI check` : ""}</span></button>
      ${kpi("Interviews", k.interviews, `${k.offers} offer${k.offers === 1 ? "" : "s"} · ${k.rejected} not selected`)}
    </div>

    <div class="panels">
      <section class="panel">
        <div class="panel-head"><h3>Applications per day</h3><span class="muted">last 14 days</span></div>
        <div class="chart" id="chartApplied"></div>
        ${dataTable(d.series, [{ key: "applied", label: "Applied" }])}
      </section>
      <section class="panel">
        <div class="panel-head"><h3>Outreach per day</h3><span class="muted">last 14 days</span>
          <div class="legend"><span><i style="background:${c1}"></i>Email</span><span><i style="background:${c2}"></i>WhatsApp</span></div></div>
        <div class="chart" id="chartOutreach"></div>
        ${dataTable(d.series, [{ key: "emails", label: "Email" }, { key: "whatsapp", label: "WhatsApp" }])}
      </section>
      <section class="panel">
        <div class="panel-head"><h3>Pipeline</h3><span class="muted">all applications</span></div>
        ${hbars(Object.entries(d.pipeline), STATUS_LABELS)}
      </section>
      <section class="panel">
        <div class="panel-head"><h3>Applied by board</h3></div>
        ${hbars(Object.entries(d.sources))}
        <p class="muted small" style="margin:12px 0 0">Today: ${k.reviewed_today} jobs reviewed · ${Math.round(d.ai_tokens_today / 1000)}k AI tokens used · daily send limits ${d.caps.email} emails / ${d.caps.whatsapp} WhatsApp</p>
      </section>
    </div>

    <section class="panel">
      <div class="panel-head"><h3>Recent applications</h3></div>
      ${recent}
    </section>`;

  barChart($("chartApplied"), d.series, [{ key: "applied", label: "Applied", color: c1 }]);
  barChart($("chartOutreach"), d.series, [
    { key: "emails", label: "Email", color: c1 },
    { key: "whatsapp", label: "WhatsApp", color: c2 },
  ]);
}

$("dash").addEventListener("click", (e) => {
  if (e.target.closest("[data-goto]")) showView("jobs");
});

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => !$("dashboardView").hidden && renderDashboard(), 150);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderDashboard());
setInterval(() => !$("dashboardView").hidden && !document.hidden && refreshDashboard(), 60000);

fetch("/api/profile")
  .then((r) => r.json())
  .then((p) => {
    dash.firstName = (p.profile || p).personal?.first_name || "";
    renderDashboard();
  })
  .catch(() => {});

// ---------------------------------------------------------------- "Did you apply?" after opening a job

let openedUid = null;
detailEl.addEventListener("click", (e) => {
  const open = e.target.closest("[data-open]");
  if (open) openedUid = open.dataset.open;
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !openedUid) return;
  const job = findJob(openedUid);
  openedUid = null;
  if (!job || job.application) return;
  const el = $("toast");
  el.innerHTML = `Did you apply to <strong>${escapeHtml(job.title)}</strong>? <button type="button" class="btn small primary" id="toastYes" style="margin-left:10px">Yes, mark applied</button> <button type="button" class="link-btn" id="toastNo" style="color:inherit;margin-left:6px">Not yet</button>`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 20000);
  $("toastYes").onclick = () => {
    el.hidden = true;
    setStatus(job.uid, "applied");
  };
  $("toastNo").onclick = () => (el.hidden = true);
});

showView(location.hash.slice(1) || "dashboard");
