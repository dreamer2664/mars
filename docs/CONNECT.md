# CONNECT — how an agent on another computer drives this PC

You asked for: an agent wakes up, gets the project's MD, and can watch/click/type on your PC.

**"You can't connect to my PC" was the wrong way for me to say it, so here is the right one.** There is no
live wire between my session and your machine, and there cannot be: I run inside a turn, I have no socket, no
port, nothing listening, and I stop existing between your messages. What that does *not* mean is that I cannot
drive it. Driving happens through the repo, and it works exactly the same whether the gap between us is ten
seconds or ten hours:

  1. I write a command file and push it (`pc/queue/c-….json`) — I can do this from here, on your behalf,
     because I already have write access to `mars`.
  2. Your PC's `pc_agent.py watch` pulls it within one interval, executes it *through* the bridge, and pushes
     the result back.
  3. I read the result on my next turn — including screenshots, which I can open and look at.

So the connection is not missing; it is asynchronous. What you get: commands execute on your machine at the
interval pace, without you touching anything, and every one leaves a receipt. What you do not get: me watching
a live video feed and reacting in real time inside a single step. One look-decide-click cycle costs one of my
turns, or — if I pre-queue a plan — runs at the interval pace with me not involved until it finishes.

### The lease token is not a secret, and pretending otherwise was misleading

`world.py pc acquire` writes the token **into `world/lock.json` and pushes it** — and this repo is public. So
while a lease is held, the token is world-readable. That means requiring it inside each queued command would
be ceremony that protects nothing while implying it protects something. The runner now injects the live token
at execution time from its own pulled lock, and a command that does carry one is still held to it.

What actually protects the machine: **the lease** (one holder at a time, and the generation is re-checked at
execution), **the bridge's four refusals**, **the deadman expiry** on every command, and **your stop switch**.
If you ever want the token to be a real secret, that is a change to `world.py`'s lock schema, which every
agent in the fleet reads — a fleet decision, not one I would make on my own.

---

### The mechanics

Nothing connects *to* your PC. Your PC reaches out to the one place both sides already trust — the repo —
and the agent's commands and the machine's answers travel as commits:

```
agent (a turn, anywhere)  ──git push──▶  pc/queue/<id>.json
                                              │
                                     pc_agent.py (this machine, in a terminal)
                                              │
                                    tools/bridge.py  ── the 4 refusals + scope + lease
                                              │
                                      your actual desktop
                                              │
agent's next turn  ◀──git pull──  pc/results/<id>.done.json + pc/receipts.jsonl
```

No port is opened. Nothing listens on your home network. `pc/` is already in `.gitignore`, so the local
receipt log never leaves your machine; only the one-line-per-command results travel.

---

## 0. What must already be true

You ran `python tools\bridge.py --backend windows selftest` and got a PASS. **Send me that JSON once, whole.**
Two fields decide what is allowed next, and I will not guess them:

| field | why I need it |
|---|---|
| `verdict` on `PASS-POLICY-ONLY` | the gates were proven on the simulator: **nothing is known about your desktop yet** |
| `verdict` on `PASS` | the bridge really read your screen. This is the one that unlocks Step 2 |
| `checks[]` where `name == "backend"` | `backend=windows` means a real lens; `backend=sim` means the pass was a rehearsal |

If it was `PASS-POLICY-ONLY`, we do one more Windows run before queueing anything — one command, and I'd
rather spend two minutes than have you queue a task against a machine I have not seen work.

## 1. The lease is the grip

Nothing executes without a live lease in `world/lock.json`. That is the same lock the rest of the crew uses,
so it is also your off switch — and it is the reason a second agent cannot end up typing into your screen
while I think I am the only one driving.

```powershell
cd $HOME\mars
git pull                      # main is the default branch now; no fetch/reset dance needed

python tools\pc_agent.py status
#   waiting: 0                 nothing queued
#   holder: null               nobody holds the PC
#   paused: null               not paused
```

## 2. Connect (60 minutes of grip, renewable)

You can also queue commands yourself, from the PC, to try the channel before any agent uses it:

```powershell
python tools\pc_agent.py queue --verb windows --task PCB-01
python tools\pc_agent.py queue --verb capture --task PCB-01 --arg window=id-1
python tools\pc_agent.py once          # or leave `watch` running
```

`queue` refuses to write a command when no lease is live — a command queued against nobody sits in the repo
looking like work and would be inherited by whoever acquires next.

```powershell
python tools\pc_agent.py connect --by fabius --purpose "PCB-02 first real task" --ttl 60
```

Two things happen: `world.py pc acquire` publishes the lease and prints

```
GRANTED generation=1 token=<token> ...
```

…and the token is **the password to your mouse**. It is printed here and nowhere else — not written to disk,
not committed. Keep it in that terminal window.

Then start the loop that makes it a connection:

```powershell
python tools\pc_agent.py watch --interval 10
```

```
pc_agent: watching $HOME\mars\pc\queue every 10s — Ctrl-C to stop, `pc_agent.py stop` to stop it from anywhere
```

Leave it running. Every 10 seconds it pulls, executes **at most one** queued command, and pushes the result.
An empty queue costs nothing and prints nothing. A command it refuses prints one line naming the refusal.

## 3. What I can then send you, and what it will look like

I write a file, commit it, push it. On your machine it becomes a click or a screenshot. A read looks like
this — `capture` is the "let me see it" verb:

```json
{ "id": "c-0001", "by": "fabius", "task": "PCB-02",
  "verb": "capture", "args": {"window": "id-0", "save_to": "$HOME\\Desktop\\lens.png"},
  "ask": "none", "generation": 1, "token": "<the live token>",
  "created_ts": "2026-10-16T20:00:00Z", "expires_ts": "2026-10-16T20:10:00Z" }
```

`pc/results/c-0001.done.json` then appears on the other side and I read it on my next turn:

```json
{ "id": "c-0001", "verb": "capture", "status": "done", "reason": "executed",
  "receipt": "r000004", "checkout": "b5a9aa6", "backend": "windows",
  "at": "2026-10-16T20:00:03Z" }
```

A write (`invoke`, `set_text`, `key_chord`, …) needs `"task"` set — that is not a formality, it is what makes
the action reviewable later — and with `"ask": "important"` it stops and waits for your yes:

```json
{ "status": "pending-approval", "approval_id": "apr-20261016-01",
  "detail": "chatcode dm <director> \"approve apr-20261016-01: invoke for PCB-02\"" }
```

Then you type `python tools\bridge.py approve apr-20261016-01` and it runs. Nothing else in the system can
approve on your behalf.

## 4. Your three off switches, in order of severity

| what you do | what stops | how fast |
|---|---|---|
| `python tools\pc_agent.py stop` | everything: the loop refuses before executing anything | next turn (~10 s) |
| Ctrl-C in the watch window | the loop itself | immediately |
| `python tools\world.py pc release --by fabius --token <token>` | the lease: the loop keeps running but every command becomes `no-lock` | immediately |
| pull the network cable | everything, obviously — and the queue waits for you, nothing is lost | immediate |

`pc_agent.py stop` writes `pc/PAUSE`, which is checked **before** every command. `resume` lifts it — and it is
deliberately a separate command, because a resume is a decision and not a retry.

## 5. What the runner will refuse, and why each refusal is the point

| refusal | when | why it exists |
|---|---|---|
| `no-lock` | no lease, or a released one | an unscoped click is an unattributable click |
| `lease-expired` | the lease timed out | the PC goes idle by *design*, not by accident |
| `stale-generation` | somebody else acquired while your command sat in the queue | it refuses rather than typing into another agent's turn |
| `deadman` | the command's `expires_ts` passed | a click that fires after the task moved on is worse than one that never fired |
| `no-scope` | a write with no `task` | the bridge's rule, not re-implemented here (a second copy would drift) |
| `not-allowlisted` | a verb the bridge does not define | the verb list is read from the bridge's own table |
| `bad-command` | a hostile or malformed id (`../escape`) | ids become filenames, so they are pattern-checked |
| `paused` | you said stop | checked before each command, from anywhere |

Everything the bridge already refuses stays refused — the four §7.1 refusals (elevated-action, credential
access, Ctrl-Alt-Del, and the captcha-project targets) are in the code path the runner uses, not beside it.

## 6. What this still cannot do

- **Look at pictures.** The agent can read a saved PNG on a later turn; it cannot watch a live stream. That is
  one image per turn, files on your Desktop.
- **React fast.** Every step is at least one loop interval plus one of my turns. Roblox Studio building with
  visual checks is exactly the right first test; CapCut (custom-drawn UI, often no element tree at all) is
  last, as planned.
- **Survive a credential expiring.** The push in `sync()` uses this clone's git credential — the same one
  `world.py pc acquire` needs. If it lapses, the loop prints `push-failed` and stops doing anything useful.
- **Work while you use the machine.** It drives your real mouse and keyboard. Do not type while it is
  executing.
