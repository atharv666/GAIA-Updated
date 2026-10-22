/* Shared helpers for the GAIA web interface (no build step, no framework). */
(function () {
  "use strict";

  const store = {
    get(key, fallback = null) {
      try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
    del(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
  };

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  async function api(path, { method = "GET", json, form, headers = {}, signal } = {}) {
    const h = { ...headers };
    const key = store.get("gaia.apiKey");
    if (key) h["X-API-Key"] = key;
    let body;
    if (json !== undefined) { h["Content-Type"] = "application/json"; body = JSON.stringify(json); }
    if (form) body = form;
    let res;
    try {
      res = await fetch(`/api/v1${path}`, { method, headers: h, body, signal });
    } catch (e) {
      throw new ApiError("Cannot reach the GAIA server.", 0);
    }
    const type = res.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      if (res.status === 401) askForKey();
      let detail = typeof data === "object" && data ? data.detail : data;
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
      throw new ApiError(detail || `Request failed (${res.status})`, res.status);
    }
    return data;
  }

  function askForKey() {
    if (document.getElementById("key-dialog")) return;
    const wrap = document.createElement("div");
    wrap.id = "key-dialog";
    wrap.className = "toast";
    wrap.innerHTML = `<div class="stack"><strong>API key required</strong>
      <input type="password" placeholder="X-API-Key" aria-label="API key">
      <div class="row"><button class="btn sm">Save</button><button class="btn sm ghost">Cancel</button></div></div>`;
    document.body.appendChild(wrap);
    const input = wrap.querySelector("input");
    const [save, cancel] = wrap.querySelectorAll("button");
    save.onclick = () => { store.set("gaia.apiKey", input.value.trim()); wrap.remove(); location.reload(); };
    cancel.onclick = () => wrap.remove();
    input.focus();
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* Minimal, safe markdown: escape first, then add a small set of inline/block rules. */
  function inline(text) {
    return text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\[([FKW])(\d{1,3})\]/g, (_, k, n) => `<button class="cite ${k.toLowerCase()}" data-cite="${k}${n}">${k}${n}</button>`);
  }

  function md(src) {
    const lines = esc(src).replace(/\r/g, "").split("\n");
    const out = [];
    let list = null;
    let para = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
    const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const raw of lines) {
      const line = raw.trim();
      let m;
      if (!line) { flushPara(); flushList(); continue; }
      if ((m = line.match(/^#{1,4}\s+(.*)$/))) { flushPara(); flushList(); out.push(`<p><strong>${inline(m[1])}</strong></p>`); continue; }
      if ((m = line.match(/^[-*•]\s+(.*)$/))) {
        flushPara();
        if (list !== "ul") { flushList(); out.push("<ul>"); list = "ul"; }
        out.push(`<li>${inline(m[1])}</li>`); continue;
      }
      if ((m = line.match(/^\d+[.)]\s+(.*)$/))) {
        flushPara();
        if (list !== "ol") { flushList(); out.push("<ol>"); list = "ol"; }
        out.push(`<li>${inline(m[1])}</li>`); continue;
      }
      flushList();
      para.push(line);
    }
    flushPara(); flushList();
    return out.join("");
  }

  const fmt = {
    time(iso) { return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "—"; },
    dateTime(iso) {
      return iso ? new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "—";
    },
    ago(iso) {
      if (!iso) return "never";
      const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
      if (s < 60) return "just now";
      if (s < 3600) return `${Math.round(s / 60)} min ago`;
      if (s < 86400) return `${Math.round(s / 3600)} h ago`;
      return `${Math.round(s / 86400)} d ago`;
    },
    pct(x) { return `${Math.round((x || 0) * 100)}%`; },
    num(x, d = 1) { return x === null || x === undefined || Number.isNaN(x) ? "—" : Number(x).toFixed(d); },
  };

  let toastTimer;
  function toast(msg, kind = "info") {
    document.querySelectorAll(".toast:not(#key-dialog)").forEach((t) => t.remove());
    const el = document.createElement("div");
    el.className = `toast ${kind === "error" ? "error" : ""}`;
    el.textContent = msg;
    document.body.appendChild(el);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.remove(), 4200);
  }

  /* SVG line chart with a min–max band, gap breaks and hover read-out. */
  function chart(el, points, { unit = "", decimals = 1, tipEl } = {}) {
    if (!points.length) { el.innerHTML = '<div class="empty">No data in this range yet.</div>'; return; }
    const W = Math.max(280, Math.round(el.clientWidth || 640)), H = 190, L = 42, R = 12, T = 12, B = 24;
    const xs = points.map((p) => new Date(p.t).getTime());
    let lo = Math.min(...points.map((p) => p.min)), hi = Math.max(...points.map((p) => p.max));
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const x0 = Math.min(...xs), x1 = Math.max(...xs) || x0 + 1;
    const sx = (t) => L + ((t - x0) / Math.max(1, x1 - x0)) * (W - L - R);
    const sy = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    // Split into segments wherever the rover was not collecting (gap > 2.5 × typical spacing).
    const steps = xs.slice(1).map((t, i) => t - xs[i]).sort((a, b) => a - b);
    const typical = steps.length ? steps[Math.floor(steps.length / 2)] : 0;
    const segments = [[0]];
    for (let i = 1; i < xs.length; i++) {
      if (typical && xs[i] - xs[i - 1] > typical * 2.5) segments.push([]);
      segments[segments.length - 1].push(i);
    }
    const P = (i, v) => `${sx(xs[i]).toFixed(1)},${sy(v).toFixed(1)}`;
    const lines = segments.map((seg) => seg.map((i, k) => `${k ? "L" : "M"}${P(i, points[i].avg)}`).join("")).join("");
    const bands = segments.filter((seg) => seg.length > 1).map((seg) =>
      `<polygon class="band" points="${seg.map((i) => P(i, points[i].max)).join(" ")} ${[...seg].reverse().map((i) => P(i, points[i].min)).join(" ")}"/>`).join("");
    const dots = segments.filter((seg) => seg.length === 1).map(([i]) => `<circle class="hover-dot" r="3" cx="${sx(xs[i])}" cy="${sy(points[i].avg)}"/>`).join("");
    const yTicks = [lo + pad, (lo + hi) / 2, hi - pad];
    const nx = W < 420 ? 3 : 5;
    const xTicks = Array.from({ length: nx }, (_, i) => x0 + (i / (nx - 1)) * (x1 - x0));
    const multiDay = x1 - x0 > 36 * 3600e3;
    const tLabel = (t) => new Date(t).toLocaleString([], multiDay ? { day: "numeric", month: "short" } : { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    const anchor = (i) => (i === 0 ? "start" : i === nx - 1 ? "end" : "middle");
    el.innerHTML = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img">
      ${yTicks.map((v) => `<line class="axis" x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text x="${L - 6}" y="${sy(v) + 3}" text-anchor="end">${v.toFixed(decimals)}</text>`).join("")}
      ${xTicks.map((t, i) => `<text x="${sx(t)}" y="${H - 6}" text-anchor="${anchor(i)}">${tLabel(t)}</text>`).join("")}
      ${bands}<path class="line" d="${lines}"/>${dots}
      <line class="hover-line hidden" y1="${T}" y2="${H - B}"/><circle class="hover-dot hover hidden" r="4"/>
      <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
    </svg>`;
    const svg = el.querySelector("svg"), hl = svg.querySelector(".hover-line"), hd = svg.querySelector(".hover-dot.hover");
    svg.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect();
      const x = ((e.clientX - r.left) / r.width) * W;
      let best = 0;
      xs.forEach((t, i) => { if (Math.abs(sx(t) - x) < Math.abs(sx(xs[best]) - x)) best = i; });
      const p = points[best];
      hl.setAttribute("x1", sx(xs[best])); hl.setAttribute("x2", sx(xs[best])); hl.classList.remove("hidden");
      hd.setAttribute("cx", sx(xs[best])); hd.setAttribute("cy", sy(p.avg)); hd.classList.remove("hidden");
      if (tipEl) tipEl.textContent = `${fmt.dateTime(p.t)} · avg ${p.avg.toFixed(decimals)}${unit} (min ${p.min.toFixed(decimals)}, max ${p.max.toFixed(decimals)}, ${p.count} readings)`;
    });
    svg.addEventListener("mouseleave", () => { hl.classList.add("hidden"); hd.classList.add("hidden"); if (tipEl) tipEl.textContent = ""; });
  }

  const SENSOR_STYLE = {
    temperature: { icon: "thermo", bg: "bg-soil" },
    humidity: { icon: "drop", bg: "bg-water" },
    soil_moisture: { icon: "sprout", bg: "bg-plant" },
    mq2: { icon: "wind", bg: "bg-air" },
    mq5: { icon: "wind", bg: "bg-air" },
    mq6: { icon: "wind", bg: "bg-air" },
    distance_cm: { icon: "radar", bg: "bg-mist" },
    battery_v: { icon: "battery", bg: "bg-mist" },
  };
  const icon = (name, cls = "") => `<svg class="icon ${cls}"><use href="#i-${name}"/></svg>`;

  function metricCard(r) {
    const st = SENSOR_STYLE[r.sensor] || { icon: "gauge", bg: "bg-white" };
    const progress = r.progress == null ? null : Math.max(0, Math.min(1, r.progress));
    return `<div class="metric ${st.bg} fade-in">
      <div class="row between">${icon(st.icon)}${r.band ? `<span class="pill neutral">${esc(r.band)}</span>` : ""}</div>
      <div class="value">${esc(r.display)}</div>
      <div class="label">${esc(r.label)}</div>
      ${progress !== null ? `<div class="bar"><i style="width:${(progress * 100).toFixed(0)}%"></i></div>` : ""}
      <div class="meta">${esc(fmt.ago(r.ts))} · ${esc(r.device_id)}</div>
    </div>`;
  }

  async function refreshStatus() {
    const el = document.getElementById("sys-status");
    if (!el) return;
    try {
      const h = await api("/health");
      el.innerHTML = [
        [h.db, `Database · ${h.database}`],
        [h.llm.available, `LLM · ${h.llm.model}`],
        [h.vlm.available, `VLM · ${h.vlm.model}`],
        [h.search.available, `Web search · ${h.search.provider}`],
      ].map(([ok, label]) => `<div class="status-line"><span class="dot ${ok ? "ok" : "bad"}"></span><span class="truncate">${esc(label)}</span></div>`).join("");
    } catch { el.innerHTML = '<div class="status-line"><span class="dot bad"></span>Server unreachable</div>'; }
  }

  window.GAIA = { api, ApiError, esc, md, fmt, toast, chart, store, icon, metricCard, SENSOR_STYLE };
  document.addEventListener("DOMContentLoaded", refreshStatus);
})();
