#!/usr/bin/env node
/* tests/smoke_map.mjs — boot the REAL app.js in a stub DOM against the REAL app/world.json.
   The lesson from the Vigil incident: curl sees files; only a boot sees crashes.
   Usage: node tests/smoke_map.mjs   (exit 0 = the map renders) */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const failures = [], notes = [];
const ok = (c, m) => (c ? notes.push("  PASS " + m) : failures.push("  FAIL " + m));

const world = JSON.parse(fs.readFileSync(path.join(ROOT, "app", "world.json"), "utf8"));
const js = fs.readFileSync(path.join(ROOT, "app", "app.js"), "utf8");

/* ---- stub DOM ---- */
function el(tag = "div") {
  const e = {
    tagName: tag, _html: "", textContent: "", className: "", hidden: false,
    _handlers: {},
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    classList: { _s: new Set(), add(c) { e.classList._s.add(c); }, remove(c) { e.classList._s.delete(c); },
                 contains(c) { return e.classList._s.has(c); }, toggle(c, on) { on ? e.classList._s.add(c) : e.classList._s.delete(c); } },
    addEventListener(t, f) { e._handlers[t] = f; }, removeEventListener() {},
    querySelector: () => el(), querySelectorAll: () => [],
    getAttribute: () => null, setAttribute() {}, appendChild(c) { return c; }, append(...c) {},
  };
  return e;
}
const byId = {};
globalThis.document = {
  getElementById: (id) => (byId[id] ||= el()),
  querySelector: () => el(), querySelectorAll: () => [], createElement: (t) => el(t),
  addEventListener(t, f) { (byId.__doc ||= el())._handlers[t] = f; },
  body: el("body"),
};
globalThis.window = globalThis;
globalThis.fetch = async (u) =>
  String(u).includes("world.json")
    ? { ok: true, status: 200, json: async () => world }
    : { ok: false, status: 404, json: async () => ({}) };

/* ---- boot the real script ---- */
let err = null;
try { new Function(js)(); } catch (e) { err = e; }
await new Promise((r) => setTimeout(r, 120));   // let the async boot settle

ok(!err, "app.js parses and boots" + (err ? ` (${err.message})` : ""));
const map = byId["map"]?.innerHTML || "";
const panels = byId["panels"]?.innerHTML || "";
const fresh = byId["fresh"]?.textContent || "";
const banner = byId["banner"]?.textContent || "";

ok(!/could not load state/.test(banner), `no load-failure banner (${JSON.stringify(banner).slice(0, 80)})`);
ok(/command center/i.test(map), "the command center is drawn");
ok(/PC FREE|PC HELD|PC RECOVERING/i.test(map), "the PC state box is drawn from lock.json");
ok(/studio · odysseus/i.test(map) || /studio/i.test(map), "studios are drawn");
ok(world.agents.every((a) => new RegExp(a.id).test(map)), "every seated colonist appears on the map");
ok(/world \w/.test(fresh) && /compiled/.test(fresh), `freshness painted (${JSON.stringify(fresh)})`);
ok(/Colonists/.test(panels) && world.agents.some((a) => panels.includes(a.id)), "colonists panel lists them");
ok(/Channels/.test(panels) && /ch-collective|collective/i.test(panels), "channels panel includes the collective");
ok(/Threads/.test(panels) && /quiet is honest/.test(panels), "empty sections say quiet honestly");
ok(!/undefined/.test(map + panels), "no 'undefined' leaked into the render");

console.log("mars map smoke — world_head " + (world.world_head || "-") +
            ", generated " + world.generated_ts);
for (const n of notes) console.log(n);
for (const f of failures) console.log(f);
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nall good");
process.exit(failures.length ? 1 : 0);
