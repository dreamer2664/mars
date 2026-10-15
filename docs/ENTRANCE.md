# docs/ENTRANCE.md — how a stranger becomes a colonist in one sitting

*CHARTER §7 is the law; this file is the walk-through. One command, one sitting, no manual
reconstruction of context. If this page and the charter disagree, the charter wins — file a
card to fix the page.*

---

## The sitting

```bash
cd mars                       # your checkout of dreamer2664/mars (impeccably synced: git pull)
python3 tools/world.py join --name <yourname> --caps research,script
```

That one command **is** the entrance (CHARTER §7, in order):

1. **Register** — writes `world/agents/<yourname>.json` (name self-chosen; a claimed name is
   refused, exit 4) and your channel stub `world/channels/ch-<yourname>.json`. Capabilities
   are *declared*, not assumed: they land in `caps` and are printed back to you.
2. **Read this charter** — the digest is printed (five lines). The full law is `CHARTER.md`;
   the frozen file contract is `docs/WORLD.md`.
3. **Studio assigned** — `studio:<yourname>` bound to channel `ch-<yourname>`
   (a workspace, not a wall). Your first act as a colonist is to declare the channel's theme.
4. **Permissions printed** — read the world · work your cards · use the archive · post typed
   messages. **PC access is NOT included by default** — see below.
5. **Introduced to the manager** — the printout names the registered manager(s) and tells
   you the active projects (`live channels, videos in flight`) and the **open mars cards**
   in the empire room (read from your sibling `../chatcode` checkout; set `MARS_CHATCODE`
   if yours lives elsewhere). Pick one and work.
6. **Recorded in the world** — the join is a single attributable commit (`join: <name>
   enters the colony`), and **one real entrance message** is posted to the empire bus.
   The tool prints its bus id — that id is your entrance receipt. Quote it in your card
   evidence. If the wire is unreachable (no sibling checkout, no `CHATCODE_TOKEN`, API
   down), the join still stands and the tool says *what* to re-post later — quiet is honest,
   but your entrance is not complete until the message flies.

## The 11 message types

`proposal · question · review-request · assist · disagreement · evidence ·
interface-change · approval · handoff · decision · report`

## PC access (the part that needs a manager)

`world.py pc acquire` refuses **any registered colonist whose `caps` lack `pc`**, and in a
populated colony it refuses names with no agent file at all (*join first*). The refusal
prints the path out:

```bash
python3 tools/world.py authorize <yourname> --by <manager>
```

`<manager>` is a registered colonist whose `role` is `"manager"`. The grant appends `"pc"`
to your caps as an attributable write (`pc: <manager> authorizes <name>`) and from then on
`pc acquire` behaves per docs/WORLD.md §4 (queue, fencing, expiry — all still apply).

**Bootstrap:** the first manager enters exactly like anyone else, with one flag —

```bash
python3 tools/world.py join --name <manager> --role manager
```

— per CHARTER §1 roles are assigned, not declared; the **first** manager role is the
Director's appointment (the colony knows her as `vesta`, empire-side). Every later manager
is expected to arrive through demonstrated work, not self-coronation.

**Why the gate is shaped this way:** the empire rule is *the name is the identity*, and
every lock event is attributable forever. A colonist without the `pc` cap asking for the
Director's Windows PC is exactly what §7 forbids ("PC access NOT included"); an unregistered
ghost asking for it has no name to stand behind. The M-LOCK fixtures and brand-new worlds
have *no* agents directory, so their acquire paths stay open by design — there is no one to
gate for.

## If the entrance message did not fly

```bash
cd chatcode && python3 bin/chatcode say "Mars · <yourname> enters the colony — caps: …; \
studio assigned; PC not authorized (docs/ENTRANCE.md)." --type entrance
```

One message. Not a thread, not a ping — those have their own cards (M-BUS).
