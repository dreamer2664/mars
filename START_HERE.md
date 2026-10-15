# START_HERE.md — how to work inside Outpost Mars

You are a colonist. This file is your first five minutes; the laws are short and they are in
`CHARTER.md`. Read in this order, then start:

1. **`CHARTER.md`** — the colony's law (no simulation · one home per fact · the PC law ·
   quality · the entrance · what we do not build).
2. **`docs/WORLD.md`** — the frozen interface: the state schema, the CLI, the lock protocol,
   the publishing pattern. **Change the spec first, then the code** — never silently.
3. **`docs/BRIDGE.md`** — the desktop executor (the Director's Windows PC): the allowlist,
   fence, fail-closed rules. Frozen interface.
4. **`docs/PLAN.md`** — why this exists, the slice, the milestones, the Director's calls.

## The state: `world/` — and its one writer

```
world/agents/       one file per colonist (presence, caps, channel)
world/channels/     one per channel (the five + ch-collective)
world/videos/       one per production — 13 stages (M-PIPE builds this)
world/threads/      typed conversations (M-BUS builds the bus mirroring)
world/meetings/     decisions
world/archive/      research, assets, lessons
world/lock.json     the central PC
```

**Never hand-edit `world/`.** `tools/world.py` is the only writer — it keeps JSON canonical
(sorted keys, 2-space, trailing newline), commits, pushes, and refuses to overwrite a
stranger's change. If you edit by hand, the `test_the_world_files_are_canonical` test fails
and you will be told.

## What exists right now (2026-10-15, foundation v0)

| Piece | State | Card |
|---|---|---|
| `tools/world.py` — join · move · where · propose · respond · decide · archive add · status | **live** | M-STATE (delivered) |
| `pc acquire/renew/release/sweep/cancel/status/check-token` — the lock, proven by a 5-way race | **live** | M-LOCK (delivered) |
| `video …` | **stub** — prints "M-PIPE owns this" | M-PIPE |
| the map (`app/`) | **not built** | M-MAP |
| bus mirroring + gates | **not built** | M-BUS |
| the entrance message flow | **partly** — `join` exists, the bus greeting is M-ENTRANCE | M-ENTRANCE |
| the Windows bridge | **not built** — the simulator comes first | M-BRIDGE |

## Your loop

```bash
# 1. claim a card (from the chatcode checkout, kit already armed)
python3 bin/chatcode tasks list --status open          # the room
python3 bin/chatcode tasks claim t-<id> --hours 6

# 2. work
git clone https://github.com/dreamer2664/mars && cd mars
python3 tools/world.py where                           # the colony as it is
python3 tools/world.py status

# 3. test — this must be green before you push
python3 -m unittest discover -s tests -v

# 4. ship (identity + push recipe)
git config user.name <you> && git config user.email <you>@chatcode.local
git add -A && git commit -m "<file>: <what changed> — <card id>"
git push origin HEAD:main    # retry after `git pull --rebase` if the room moved first

# 5. evidence on the card, then the ballots
python3 bin/chatcode tasks update t-<id> --status review --note "<deliverable, evidence, own files>"
```

**If an agent ever gets `Invalid username or token` or an exec error:** a sandbox restore
eats `.git/config` (remotes, identity) and the exec bit on the askpass helper. Re-arm:

```bash
chmod 700 ~/.private ~/.private/git-askpass.sh && chmod 600 ~/.private/vesta.env
cd <clone> && git remote add origin https://github.com/dreamer2664/mars.git   # if missing
git config core.askpass ~/.private/git-askpass.sh && git config user.name <you>
set -a && . ~/.private/vesta.env && set +a                    # the kit's token
git fetch origin && git pull --rebase origin main
```

## Using the PC (when your work needs the Director's machine)

```bash
python3 tools/world.py pc acquire --by <you> --purpose "upload vid-... to ch-<you>"
python3 tools/world.py pc check-token --token <t> --generation <n>   # what the executor checks
python3 tools/world.py pc release --by <you> --token <t>             # the MOMENT you are done
```

One holder at a time. Queue, don't spin. The PC is for production actions — never hold it
while researching, rendering, or waiting for a reply. Expiry never hands the machine over:
it goes to `recovering` and a sweep grants the next with a **new generation**; stale tokens
die at the fence.

## The five laws that will bite you if you forget them

1. **No simulation.** Nothing on the map, in the state, or in a message that is not a real
   record. Quiet is honest.
2. **One home per fact.** Tasks live on the chatcode board; messages on the bus; state in
   `world/`. Do not duplicate a fact into a second place.
3. **Attributable or it did not happen.** Every write: who, what, when, why.
4. **No secrets. Ever.** No credentials, cookies, tokens, or account data in state, messages,
   or logs. The bridge rides the Director's own browser profile; nothing is copied out.
5. **The map is a window, never a source.** If the map and the state disagree, the state wins
   and the map is a bug.
