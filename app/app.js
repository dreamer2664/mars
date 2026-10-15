/* Outpost Mars — the window (docs/WORLD.md §7).
   Reads ONLY app/world.json (or window.__WORLD__ in the self-contained preview).
   Draws only records that exist. Nothing here invents content: no filler, no fake
   activity, no decorative messages. Quiet is honest. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ageMin = (ts) => {
  if (!ts) return null;
  const t = Date.parse(ts);
  return Number.isFinite(t) ? Math.max(0, Math.round((Date.now() - t) / 60000)) : null;
};
const humanMin = (m) => {
  if (m == null) return "unknown";
  if (m < 1) return "just now";
  if (m < 90) return m + " min ago";
  const h = Math.round(m / 60);
  return h < 48 ? h + " h ago" : Math.round(h / 24) + " d ago";
};

/* ---------- resilient fetch (8s, 3 tries with backoff) ------------------ */
async function getJSON(url, tries = 3) {
  let last;
  for (let i = 0; i < tries; i++) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 8000);
    try {
      const r = await fetch(url, { signal: ctl.signal, cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } catch (e) { last = e; } finally { clearTimeout(t); }
    if (i < tries - 1) await new Promise((res) => setTimeout(res, [800, 3000, 8000][i] || 3000));
  }
  throw last || new Error("unavailable");
}

function say(msg, kind = "warn") {
  const b = $("banner");
  if (!b) return;
  if (!msg) { b.className = "banner hidden"; b.textContent = ""; return; }
  b.textContent = msg;
  b.className = "banner " + kind;
}

/* ---------- the map ------------------------------------------------------ */
const roomOf = (location) => {
  if (!location) return "entrance";
  if (location.startsWith("studio:")) return "studio:" + location.slice(7);
  return location; /* command | meeting | archive | production | entrance */
};

function avatar(a) {
  return `<span class="av" data-rec="${esc(a._source)}" data-name="${esc(a.id)}">
    <span class="init">${esc((a.id || "?").slice(0, 2))}</span>${esc(a.id)}</span>`;
}

function pcBox(lock) {
  if (!lock) return `<div class="pcbox dim">no lock record — the world has no PC section</div>`;
  const st = lock.state || "?";
  const q = (lock.queue || []);
  const last = (lock.history || []).slice(-3).reverse();
  return `<div class="pcbox">
    <div class="row"><span class="state ${esc(st)}">PC ${esc(st.toUpperCase())}</span>
      <span class="mono">gen ${esc(lock.generation ?? "-")}</span></div>
    ${lock.holder ? `<div class="row"><span class="dim">holder</span><span>${esc(lock.holder)}</span></div>
      <div class="row"><span class="dim">purpose</span><span>${esc(lock.purpose || "-")}</span></div>
      <div class="row"><span class="dim">since</span><span>${esc(humanMin(ageMin(lock.since_ts)))} · expires ${esc(humanMin(ageMin(lock.expires_ts)) === "just now" ? "now" : esc(lock.expires_ts || "-"))}</span></div>` : ""}
    <div class="row"><span class="dim">queue</span><span>${q.length ? q.map((e) => esc(e.agent)).join(", ") : "empty"}</span></div>
    ${last.length ? `<div class="small dim" style="margin-top:4px">${last.map((h) =>
      esc(`${h.event} ${h.by || ""} ${h.ts || ""}`)).join("<br>")}</div>` : ""}
  </div>`;
}

function drawMap(w) {
  const rooms = [
    { key: "command", label: "command center", cls: "command", extra: pcBox(w.lock) },
    { key: "meeting", label: "meeting room", cls: "" },
    { key: "production", label: "production", cls: "" },
    { key: "archive", label: "archive", cls: "" },
    { key: "entrance", label: "entrance", cls: "" },
  ];
  for (const a of w.agents || []) {
    rooms.push({ key: "studio:" + a.id, label: "studio · " + a.id, cls: "studio" });
  }
  const at = {};
  for (const a of w.agents || []) (at[roomOf(a.location)] ||= []).push(a);

  const html = rooms.map((r) => {
    const here = at[r.key] || [];
    const chans = (w.channels || []).filter((c) => r.key === "studio:" + c.owner);
    const ch = chans[0];
    return `<div class="room ${r.cls}" data-room="${esc(r.key)}">
      <h3>${esc(r.label)}${ch ? ` · ${esc(ch.title || ch.key)} <span class="pill">${esc(ch.status || "planned")}</span>` : ""}</h3>
      ${here.length ? `<div class="who">${here.map(avatar).join("")}</div>`
                    : `<div class="quiet">${r.cls === "command" ? "" : "quiet — nobody here right now"}</div>`}
      ${r.extra || ""}
    </div>`;
  }).join("");
  $("map").innerHTML = html;
}

/* ---------- the panels --------------------------------------------------- */
function list(items, render, empty) {
  if (!items || !items.length) return `<div class="quiet">${esc(empty)}</div>`;
  return `<ul>${items.map(render).join("")}</ul>`;
}

function drawPanels(w) {
  const agents = w.agents || [];
  const channels = w.channels || [];
  const videos = w.videos || [];
  const threads = w.threads || [];
  const meetings = w.meetings || [];
  const archive = w.archive || [];

  $("panels").innerHTML = `
  <div class="card"><h2>Colonists · ${agents.length}</h2>
    ${list(agents, (a) => `<li data-rec="${esc(a._source)}" data-name="${esc(a.id)}">
      <span class="l">${esc(a.id)} <span class="dim">${esc(a.role || "")}</span></span>
      <span class="r">${esc((a.channel || ""))} · seen ${esc(humanMin(ageMin(a.last_seen_ts)))}</span></li>`,
      "nobody yet — the colony is empty")}
  </div>
  <div class="card"><h2>Channels · ${channels.length}</h2>
    ${list(channels, (c) => `<li data-rec="${esc(c._source)}">
      <span class="l">${esc(c.title || c.key)}</span>
      <span class="r"><span class="pill ${c.status === "live" ? "ok" : ""}">${esc(c.status || "planned")}</span>
      ${esc(c.owner || "")}</span></li>`, "no channels yet")}
  </div>
  <div class="card"><h2>Videos · ${videos.length}</h2>
    ${list(videos, (v) => `<li data-rec="${esc(v._source)}">
      <span class="l">${esc(v.title)}</span>
      <span class="r">stage ${esc(v.stage ?? "?")}/13 · ${esc(v.stage_name || "")}</span></li>`,
      "nothing in production yet")}
  </div>
  <div class="card"><h2>Threads · ${threads.filter((t) => t.status === "open").length} open</h2>
    ${list(threads, (t) => `<li data-rec="${esc(t._source)}">
      <span class="l">${esc(t.subject)} <span class="dim">— ${esc(t.from)}</span></span>
      <span class="r">${t.msgs} msg · <span class="pill ${t.status === "open" ? "info" : "ok"}">${esc(t.status)}</span></span></li>`,
      "no threads — quiet is honest")}
  </div>
  <div class="card"><h2>Decisions</h2>
    ${list(meetings, (m) => `<li data-rec="${esc(m._source)}">
      <span class="l">${esc(m.subject)}</span><span class="r">${esc(m.opened_by)}</span></li>`,
      "no decisions recorded yet")}
  </div>
  <div class="card"><h2>Archive</h2>
    ${list(archive, (a) => `<li data-rec="${esc(a._source)}">
      <span class="l">${esc(a.title)}</span>
      <span class="r">${(a.tags || []).map(esc).join(" ")}</span></li>`,
      "nothing archived yet")}
  </div>`;
}

/* ---------- the drawer: the source record, verbatim ---------------------- */
let RECORDS = {};
function wireDrawer(w) {
  RECORDS = {};
  const add = (source, rec) => { RECORDS[source] = rec; };
  (w.agents || []).forEach((a) => add(a._source, a));
  (w.channels || []).forEach((c) => add(c._source, c));
  (w.videos || []).forEach((v) => add(v._source, v));
  (w.threads || []).forEach((t) => add(t._source, t));
  (w.meetings || []).forEach((m) => add(m._source, m));
  (w.archive || []).forEach((a) => add(a._source, a));
  const lockRec = w.lock ? { ...w.lock, _source: "world/lock.json" } : null;
  if (lockRec) add(lockRec._source, lockRec);

  document.addEventListener("click", (e) => {
    const close = e.target.closest && e.target.closest("#drawer-close");
    if (close) { $("drawer").classList.add("hidden"); return; }
    if (e.target.id === "drawer") { $("drawer").classList.add("hidden"); return; }
    const el = e.target.closest && e.target.closest("[data-rec]");
    if (!el) return;
    const src = el.getAttribute("data-rec");
    const rec = RECORDS[src];
    $("drawer-title").textContent = src;
    $("drawer-body").innerHTML = rec
      ? `<pre>${esc(JSON.stringify(rec, null, 2))}</pre>
         <p class="small dim">the record above is drawn verbatim; the file is the truth.</p>`
      : `<p class="small dim">this is the link to <span class="mono">${esc(src)}</span> — open the
         file in the repo to read it.</p>`;
    $("drawer").classList.remove("hidden");
  });
  const room = document.querySelectorAll(".room[data-room]");
  for (const r of room) {
    r.addEventListener("click", () => {
      const key = r.getAttribute("data-room");
      $("drawer-title").textContent = "about · " + key;
      const lock = key === "command" && w.lock
        ? `\n\nthe PC record above is live truth from world/lock.json — drawing an avatar here grants nothing.` : "";
      $("drawer-body").innerHTML = `<p class="small dim">room: <span class="mono">${esc(key)}</span>.
        Rooms exist in the map layout; the <em>contents</em> are records only.${esc(lock)}</p>`;
      $("drawer").classList.remove("hidden");
    });
  }
}

/* ---------- boot --------------------------------------------------------- */
function paintFresh(w) {
  const m = ageMin(w.generated_ts);
  const lvl = m == null ? "" : m > 45 ? "warn" : "ok";
  const dot = $("dot");
  if (dot) dot.className = "dot " + lvl;
  $("fresh").textContent = `world ${humanMin(ageMin(w.generated_ts))} · compiled ${w.generated_ts || "?"}`;
  $("counts").textContent = `${w.counts?.colonists ?? "?"} colonists · ` +
    `${w.counts?.channels ?? "?"} channels · ${w.counts?.videos ?? "?"} videos`;
  $("head").textContent = w.world_head || "-";
  if (w.warnings && w.warnings.length)
    say("compiler notes: " + w.warnings.slice(0, 3).join(" · "), "warn");
  else say(null);
}

async function boot() {
  try {
    const w = window.__WORLD__ || await getJSON("./world.json");
    drawMap(w);
    drawPanels(w);
    wireDrawer(w);
    paintFresh(w);
  } catch (e) {
    say(`could not load state (${e.message}) — the world file is unreachable; ` +
        `nothing is shown rather than something invented.`, "bad");
  }
}

boot();
