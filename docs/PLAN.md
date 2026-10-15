# OUTPOST MARS — the manager's plan

*Adopted from the Advisor's brief (ChatGPT, 2026-10-15). Prepared by vesta for the Director.*

---

## The verdict: adopted — with one change of framing

We do not build a second company. We build a **district**. The empire already runs the
unsexy half of the Advisor's spec — a task system with leases and ballots, a bus with
attribution, a ledger that cannot forget, and a phone app that shows it all. Outpost Mars
adds the part the empire lacks: **presence, one scarce shared resource, a window you can
watch, and a real external product.**

The Advisor's sharpest insight is exactly right for us: *"The solution is not to tell agents
to communicate more often. The solution is to make communication part of the task execution
protocol."* Today the bus is optional; tomorrow a review gate can be **refused by the
tooling** until the required responses exist. That is a real upgrade, not a metaphor.

## 1. What the empire already has (do not rebuild)

| The Advisor asked for | What exists today | The gap we build |
|---|---|---|
| Task system with owners, deps, evidence (§8) | **chatcode board** — 234 cards, leases, review queue, ballots, sweep, evidence notes | dependency gates that actually block; a circular-dep check |
| Common bus, attributable messages (§5) | **the bus** — 34 channels incl. `meeting`/`meetings`; 5 ntfy wires; every push signed | structured message types; mirrored threads; dependency-locked reviews |
| Persistent, attributable state (§2) | **the record** — storage ledger (85+ rows), shift notes, storagecode vaults, formalcode wire | nothing. This is already the law |
| Manager control panel (§11) | **Vigil** — live board, crew, records, projects on your phone | a World tab later (one small job) |
| Video production (§7, partial) | **motion-studio** — headless Chromium frames → FFmpeg MP4, render API, quality manifests (M2 verification is in the review queue right now) | concept→script→review→publish stages, channels, upload, publication verification |
| Autonomous cold operation (§10) | **the kit + pipeline law** — agents wake, work, push, sleep; no servers needed | event triggers for the colony; the world must stay cheap |
| Meetings (§5) | `meeting`/`meetings` channels already on the bus | decision records with outcomes |

## 2. What is genuinely new (the build)

1. **The world state** — identity, presence, location, studio and channel bindings.
2. **The central PC lock** — exclusive lease, FIFO queue, expiry, crash recovery, fencing.
3. **The map** — a top-down colony window, compiled strictly from the state.
4. **Channels and the 13-stage video pipeline** — with "published" meaning *verified*.
5. **The entrance** — one-sitting onboarding for a new colonist.
6. **The bridge** — the one piece that will touch your desktop (waiting on your call).

## 3. Decisions I have taken (documented, changeable — say the word)

- **Repo: `dreamer2664/mars`** — world state, map, and tools in one place; pushes land on
  formalcode like everything else.
- **One home per fact.** Tasks stay on the chatcode board; messages stay on the bus;
  presence/lock/channels/videos live in `mars`. The map reads a *compiled copy*; if the map
  and the state ever disagree, the state wins and the map is a bug. (A build check proves
  every avatar corresponds to a record.)
- **Git is the mutex.** Two agents claiming the PC at the same instant cannot both win: only
  one push lands. The lock carries a lease, a FIFO queue, and a monotonic **generation** —
  the executor refuses any action bearing a stale generation. Enforcement lives outside the
  map; drawing an avatar at the computer grants nothing.
- **Push is the clock.** This morning we learned GitHub's scheduler is unreliable on this
  account (cron ticks hours late or never). The world's compiled file republishes on push —
  the pattern that already works for Vigil.
- **The bridge waits for you.** Until your call, the last two pipeline stages use a clearly
  labeled stub executor that writes an auditable receipt. No desktop, no risk.
- **Colonists are empire agents with a colony identity** — the same hand, a second name;
  any verified agent may take mars cards. The map is N-agent from day one.
- **Nothing goes public without your yes** until you decide otherwise.

## 4. The first build — the vertical slice, and how we'll know it works

The Advisor's §13 demand, in our words. The slice is done when **FIRST LIGHT** runs end to
end — real records, no simulation:

> A colonist proposes a video concept (typed message, mirrored as a thread). Two respond —
> one challenges, one brings evidence. The manager records a decision. The concept becomes a
> board task with a named dependency. A deliverable lands; the review refuses to complete
> without the required response; ballots close it. The manager approves; publish-approval is
> recorded. The colonist requests the PC — the queue is real, the lock is granted, the
> generation bumps. The executor (stub until the bridge) performs one permitted action and
> writes a receipt. The lock is released; the next in queue gets a new generation. The map
> shows every step of this, and it invented nothing.

**Cut today — 8 cards, tag `mars`:** M-STATE (world files + CLI) · M-LOCK (the PC) · M-MAP
(the window) · M-BUS (structured messages + gates) · M-ENTRANCE (onboarding) · M-PIPE (13
stages) · and two Review cards over the critical pair and the visible pair.

## 5. Milestones

- **M0 — the slice** (this batch): world + lock + map + bus + entrance + pipeline records.
  FIRST LIGHT passes.
- **M1 — the bridge and uploads** (after your calls): the local desktop service, allowlisted
  actions, real publication with verification.
- **M2 — the colony lives**: channels go live, first real videos, colonists grow, the
  collective channel starts producing genuinely collaborative work.

Honest sizing: M0 is vigil-scale work — the crew has been closing cards like this in hours
to days. The long poles are *your* two setup items (YouTube access, the PC bridge), not the
code.

## 6. Risks and honest limits

- **Desktop automation is fragile** and needs your PC awake and logged in. Uploads via
  YouTube's official API are the robust road; browser automation of logged-in sessions is
  the fallback, and grayer with the platform's rules.
- **YouTube quota**: roughly six uploads per day per Google Cloud project without Google's
  audit. Verify before we rely on the number; one collective + individual channels may need
  a second project or the audit.
- **The map must never become a second truth** — the "no simulation" law is Charter §3 and
  the build includes the check that makes it enforceable.
- **Scope**: the Advisor's do-not-build list is now Charter §8. No survival mechanics, no
  decorative systems, no simulated conversation, no architecture months.
- **Five is a founding population, not a constant** — nothing in the schema says five.

## 7. What I need from you — four calls

1. **Who are the founding colonists?**
2. **The central PC — which machine, and may I build the bridge for it?**
3. **YouTube — the official API road, or browser automation of your sessions?**
4. **Who approves before a video goes public?**

## 8. Reply to the Advisor (paste-able)

> Advisor — the brief is adopted in full, with one framing change: Outpost Mars is a
> district of an empire that already runs a task board (234 cards, leases, ballots), a bus
> (34 channels, five public wires), an immutable ledger, and a phone control panel. We will
> not rebuild those; we bind them. The slice is cut: world state, the PC lock (lease + FIFO
> + fencing generation, enforced outside the map), the compiled map, structured messages
> whose gates can *refuse completion* without the required responses, the entrance protocol,
> and the 13-stage pipeline where "published" means verified. Your §15 became our Charter §8
> — law, not advice. Two dependencies are the Director's to open: the desktop bridge (his
> machine) and the upload road (official API recommended; ~6 uploads/day/project without
> Google's audit). We report with files, tests, and receipts; nothing is claimed until it
> has been run.

---

## 7b. The Director's calls — answered (2026-10-15, 2026-10-15T12:29:45Z)

1. **Colonists** — the current crew: **odysseus, argus, scipio, cormac, ariadne**. Seated in
   `world/agents/` the same hour; each declares its channel theme at first wake (the voice
   is the colonist's, not the manager's).
2. **The PC** — a **Windows machine**, and the bridge is authorised: a tiny local service
   (the only thing that ever touches the desktop), allowlisted actions, token+generation
   fencing, fail-closed. Spec: `docs/BRIDGE.md`. Job: M-BRIDGE.
3. **Uploads** — the **browser road** (the Director's call): attach over CDP to a dedicated
   profile he logs into once; his sessions do the uploads. No API paperwork, no cookies
   exported, no password in the project. The fragility is accepted and fenced: receipts,
   verification, never an assumed upload.
4. **Approval** — his word on the **idea** is a directive: a bus/ntfy message naming the
   thread or video, and it pre-authorises that video end-to-end. Without a directive the
   gate pings him; if he is unavailable, the **manager** approves on the record. Every
   approval records which kind it was. (CHARTER §5, amended.)

With these calls, M0 and M1 are both unblocked. The bridge card is cut; the founding state
is seeded; the five have their studios.


## Director pause — 2026-10-15

OUTPOST MARS is set aside in favor of agent training for real five-minute YouTube
video production. All unfinished Mars cards are blocked with a reversible Director
pause; previous status and evidence are preserved on each card. Resume only on a
new Director order; do not treat this project or its Windows bridge as a dependency
of the video-training campaign. The map remains available as the existing view.

Shared archive cutover: dreamer2664/storagecode is the only writable archive for
PUSHES, shifts, data and wire. dreamer2664/storage is retained read-only for history.
