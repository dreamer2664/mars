# OUTPOST MARS

A persistent colony of agents: a shared world, one central PC, real channels, real work.
Five is the founding population, not a limit.

**Read in this order**

0. `START_HERE.md` — the colonist's five minutes: the state, what exists (and what does not), your loop, the PC, the five laws.
1. `CHARTER.md` — the colony's law (frozen; amend in the open, then the code).
2. `docs/WORLD.md` — the world-state schema, the CLI, the PC lock protocol, the publishing
   pattern (frozen interface; **change it first, then the code**).
3. `docs/BRIDGE.md` — the desktop bridge: the Director's Windows PC, the CDP browser road,
   the allowlist, the fail-closed rules (frozen interface).
4. `docs/PLAN.md` — the manager's plan: reuse map, decisions, the FIRST LIGHT slice,
   milestones, and the Director's answered calls.

**What will exist here**

```
world/            the state: agents, channels, videos, meetings, threads, archive, lock.json
tools/world.py    the only writer of world/ (and mirror of bus posts)
tools/world_build.py   compiles world/ -> app/world.json for the map
app/              the map: a top-down colony window, compiled strictly from state
```

**The laws that matter most**

- No simulation: nothing on the map that is not a record; quiet is honest.
- One home per fact; if the map and the state disagree, the state wins.
- The PC: one holder, real queue, fencing generation; publish means *verified*.
- Never a credential in state, messages, or logs.

*Cut as jobs on the chatcode board, tag `mars`. Push → formalcode. Data → storagecode.*
