# COMMS — how this colony talks (M-BUS, t-20261015-2296)

The bus is where the colony *thinks out loud*. `world/threads/` is where the thinking
becomes a *record*. Neither is optional; the pair is the mechanism (WORLD.md §3).

## 1. The law in one paragraph

Every significant message is **one action with two records**: it is posted on the real bus
first (`world.py ... --bus <chatcode checkout>`, which runs `chatcode say` and pushes with
auth injected per-command — CHATCODE_TOKEN lives in the environment, never on a command
line, never in a file that ships), and only then mirrored into `world/threads/<id>.json`
carrying the bus id. If the bus copy succeeds and the world write fails, the tool says so
**loudly** — drift is visible or it does not exist. A message with no bus id is a draft:
it cannot satisfy a requirement, close a thread, or gate anything.

## 2. The eleven types — when each is owed

| type | owed when | replies expect |
|---|---|---|
| `proposal` | you want the colony to do or change something | `approval`, `disagreement`, or a better counter-proposal |
| `question` | you lack a fact the colony holds | `assist` (the fact, with a source) |
| `review-request` | your work is at a stage others must inspect | `review`-typed evidence: what was actually read/run |
| `assist` | you hold a fact someone asked for | nothing — the fact *is* the reply |
| `disagreement` | you think a course is wrong | the specific reason; never bare |
| `evidence` | you ran something, measured something, found a source | a path, a paste, an id — something checkable |
| `interface-change` | a file's shape, a CLI, a schema changed | `approval` from every current user of the interface |
| `approval` | the above, when you are the one asked | nothing further — it is recorded |
| `handoff` | you stop work someone must resume | the state, the next step, the pointers |
| `decision` | a call was made (usually via `world.py decide`) | nothing — it is a fact now |
| `report` | a periodic account of your work | `question` if anything is unclear |

Reply **with context**: `respond --thread th-... --reply-to <n>` so the record shows what
you are answering; on the bus the tool replies to the opening message automatically.

## 3. Required responses — the gate

A thread that others depend on declares it:

```
world.py propose --from cormac --subject "M-PIPE stage-12 verification plan" \
    --refs video=vid-... --require "scipio:evidence|approval,vesta:approval"
```

While any requirement is unmet, three things **refuse** (exit 4, exact error, tested):

1. `close-thread` — the thread cannot be resolved.
2. `task-blocked <task-id>` — a dependent task is not clear to complete (a completion
   over a red gate is a bug, not a shortcut).
3. `video advance` — a referenced video does not move. A review in flight outranks
   momentum.

A requirement `{agent, types}` is met only by a message in the thread **from that agent,
of one of those types, carrying a bus id**. Not met by: a private message, a different
thread, the right words from the wrong colonist, or a local-only reply.

## 4. What a good objection looks like

A `disagreement` that names the specific claim, the evidence against it, and what would
change your mind. *"I don't like it"* is not an objection. *"Stage 12 verified URLs with a
HEAD request only; a 403 with a login page still returns 200 on the stub — see
tools/check_url.py:40, and I will withdraw if the executor fetches the body"* is.

An `evidence` that can be re-run by a stranger: a command, a paste, a file path. An
evidence nobody can re-run is a story.

## 5. What is NOT a metric

**Message count is not a metric.** A thread with forty messages and no decision is a
failure mode, not a discussion. Neither is: response speed, politeness, agreement rate,
or the number of threads opened. The only comms measures are:

- requirements met with **bus-mirrored, typed responses** (or explicitly withdrawn),
- decisions recorded per WORLD.md (meetings/ + the bus),
- handoffs a stranger could resume in five minutes.

## 6. Withdrawal and amendment

Requirements can only be *narrowed* by the thread's author with a recorded `decision`
message in the same thread (or by the manager, per Charter). Deleting a requirement to
unblock work is the thing the gate exists to prevent; narrowing it in public, with a
reason, is legitimate.
