# docs/BRIDGE.md — the desktop bridge (the Director's Windows PC)

*Frozen v1 interface, 2026-10-15. The only part of the colony that touches a real machine.
The Director's call: uploads ride his already-logged-in browser sessions — **no API
paperwork, no cookie export, no password ever touches this project.***

---

## 1. What it is

A tiny local service (Python, stdlib where possible) running on the Director's Windows PC.
It is the **executor**: the world grants a lock; the bridge is the only thing that can *act*.

It does exactly three things:

1. **Reports status** — is the browser reachable, which channel is active, is it idle or
   mid-action. (Read-only.)
2. **Performs allowlisted actions** — only for the current lock holder, only with the
   matching token **and generation** (fencing, docs/WORLD.md §4). A stale token is refused
   with the reason in the receipt.
3. **Writes receipts** — one per action: timestamp, action, args digest, lock generation,
   outcome, and the observable result (e.g. the published url). Receipts are returned to
   the caller; the requesting agent records them in the world. **The bridge never writes
   the world state** (one writer per fact).

## 2. The browser road (over CDP)

The bridge attaches to a real browser over the Chrome DevTools Protocol. Because Chrome 136+
ignores the debug port on the default profile, the required setup is a **dedicated profile
the Director logs into once**:

```
# one-time, from a shortcut / Run box (paths are examples):
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir="C:\mars-profile" --remote-debugging-port=9222
# (Edge works identically with msedge.exe if that is the browser in use)
```

In that window, the Director logs into the Google account(s) that own the channels **once**.
Sessions persist in that profile. The agents never see a password; the bridge never reads or
exportes cookies; account switching between brand channels happens in the YouTube Studio UI
and the bridge **verifies the active channel chip** before any upload.

## 3. The protocol (v1)

```
POST /status                    -> {browser_up, active_channel, lock: {holder, generation, ...}, busy}
POST /act                       -> body {token, generation, action, args:{...}}  -> receipt
   refusal: {ok:false, reason:"stale-generation"|"wrong-token"|"not-allowlisted"|"no-lock", ...}
```

**Allowlist (M1):** `upload_file` (path + target channel) · `set_metadata` (title,
description, thumbnail for a given video) · `read_published_url` (confirm a video is live
and return its url). Everything else is refused by name.

**Fail-closed:** browser not reachable, page not where it should be, or an unexpected dialog
⇒ do nothing, return `ok:false`, write the receipt. The lock is not released by the bridge —
only the holder (or expiry/sweep) releases it.

**Evidence discipline:** receipts carry page state (title, and the video id once known) and
timestamps. Screenshots are optional and **stay on the Director's machine** — they can show
account details and are never pushed.

## 4. How a publication actually runs (end to end)

1. Video reaches stage 9 with its approval recorded (`directive` / `director` /
   `manager-fallback`, CHARTER §5).
2. The owning agent runs `world.py pc acquire --by <name> --purpose "upload vid-... to ch-..."`.
3. Granted ⇒ the agent calls `upload_file` with its token+generation.
4. The bridge switches to the right channel, uploads, waits for processing, calls
   `read_published_url`, returns the receipt.
5. The agent advances stage 11 `uploaded` → 12 `verified` (url + receipt) → 13 `recorded`
   (deposit to storagecode) and **releases the lock immediately**.
6. Failure ⇒ receipt says so, the video stays where it was, the failure is recorded — the
   colony never assumes an upload happened because a script ran.

## 5. Testing without Windows

The crew builds and tests against a **simulator**: a mock executor implementing the same
protocol (correct gen ⇒ ok; stale gen ⇒ refused; wrong token ⇒ refused) plus a dry-run mode
in the bridge itself. The real end-to-end test happens on the Director's PC as the first M1
act, with the Director watching.

## 6. The Director's one-time setup (written for a non-programmer)

1. Create the profile shortcut (the command in §2). Put it on the desktop: **"Mars browser"**.
2. Open it once; log into the account(s) that own the channels; close it.
3. Later, the colony will ask: start the bridge (a second desktop shortcut) — a small black
   window appears and says `bridge: listening`. Leave it open while work is running.
4. If in doubt: `POST /status` — the map shows the same thing.

*If the browser or bridge is off, nothing happens — the lock expires, the queue moves on,
and the failure is on the record. Nothing is ever assumed.*

---

## 7. M1 amendment (2026-10-16, `fabius`) — the bridge becomes the general desktop executor

*The Director resumed this project and widened the brief: the agent may drive the whole desktop —
editors, engines, email, any website — bounded by **the task's scope**, not by an app allowlist.
§1–§6 above are unchanged and still binding: **one executor, fencing on token+generation, fail-closed,
one receipt per action, the bridge never writes the world, screenshots stay local.** What follows is
additive; where it conflicts with §3's M1 allowlist, this section wins and §3 is kept as history.*

### 7.1 The two laws that stay in code, not in the prompt

The repo's own reasoning applies (`tests/test_world_lock.py`, `chatcode`'s `test_no_touch.py`):
*a rule that lives only in an agent's memory is a rule with N chances to fail.* So the bridge keeps
exactly **three permanent refusals** and refuses **by name**, before any backend call:

| id | refuses | why it is code, not guidance |
|---|---|---|
| `elevated-action` | `runas`, `elevate`, `uac-accept`, `yes-on-consent-dialog` | Windows UIPI makes elevation *silently* undrivable ("0 elements, click reports success"); and a consent dialog is the OS asking **the human** |
| `credential-access` | reading/typing paths matching `.env`, `id_rsa`, `*.pem`, `known_hosts`, `login data`, `cookies`, `logins.json`, `key3.db`, `signons.sqlite`, `credentials`, `~/.private`, `vault` | the desktop agent is the thing that gets phished; a fleet-shared token visible in its pixels is one compromise for the whole crew |
| `not-a-verb` | `ctrl-alt-del` and any secure-attention sequence | the human's escape hatch must never be reachable by an automation verb |

Everything else is **permitted and scoped**. `deny` is not a policy list any more; it is four ids — the
three above plus `forbidden-target`, which refuses `captchaproject`/`captchaprogram` in a URL, a path, an
app argument or a window title. It was added *because* the Director chose to keep one shared crew token
(`ledger/DECISIONS.md` D-01): his own client admits its guard there "is not a wall - it is the promise written
down where it cannot be forgotten", and a desktop agent with a browser open is one misclick from a repo it
never meant to visit. The id grants nothing; it removes the accident.

### 7.2 Scope is mandatory and auditable

Every **write** action must name the card it serves: `--task t-…`. No scope ⇒ `refused: "no-scope"`
— not because we distrust the agent, but because an unattributable action cannot be reviewed, and the
review queue is the only real backstop once the allowlist is gone. Reads need no scope.

### 7.3 Permission per task (the Director's call, each task, at hand-off)

`ask = important` (default) · `ask = all` · `ask = none`

| action | class | perm |
|---|---|---|
| `status` `windows` `tree` `capture` `wait_for` `outcomes` `verify-chain` | read | `none` (always runs) |
| `invoke` `set_text` `key_chord` `launch` `open_url` `save_as` `tidy` | write | `important` |
| `upload_file` `set_metadata` `read_published_url` `send_email` `submit_form` | write | `important` |
| the ids in §7.1 (+ `forbidden-target`) | — | **`never`** (mode cannot relax this; only a Director edit of this file can) |

`ask=none` is exactly what it says: `important` writes run without a wait — **the read verbs still run,
so the agent can always look at what it did.** Approval is not a pause on observation.

### 7.4 Tidying is a verb, and it is encouraged

`tidy(kind=memory|close-idle|temp)` exists on purpose: keeping RAM low and the session stable is part of
the job, not a detour. Budgeted (`tidy_max_per_hour`, default 12 — a tidy loop is a stuck loop), audited
like any write, and **refused inside other agents' working files** (`git` dirs, another agent's
`agent/<name>/*` branch checkout, `world/`, `PUSHES.md`). Cleaning is welcome; cleaning up somebody
else's work is not.

### 7.5 Receipts are a chain, so "what it did" survives an edited log

Each line: `seq, prev, sha256(seq|ts|action|task|state), action, args_digest, task, backend, state,
approval, before{win,tree_digest,img_sha,bytes}, after{…}, error, why`. `outcomes --verify-chain`
recomputes it and reports the first broken link **by number**. `after` is how a receipt proves something
changed instead of proving a command ran.

### 7.6 The human is in the loop over the existing wires (nothing new to run)

`ask=important|all` parks a write as `pending-approval` and the agent asks on the bus
(`chatcode dm <director> "approve <id> …"`); `bridge.py approve <id>` executes it. The pending line is
also pushed to the doorbell topic (default `pccontrol`) — **a title and an id, never a capture, never a
window title that names an account**: ntfy is public by design here, so treat it as an untrusted wire and
keep the substance in the repo, per §5 of `SYSTEM.md`.

### 7.7 Auto-pause (the stability rule the Director asked for)

`hold_timeout_min` (default **10**): a write parked for approval past that deadline pauses the bridge —
not the machine, not the session, and not a failure on the agent's part. `bridge.py resume` clears it.
`bridge.py pause` is the human's instant stop, on localhost only. The bridge **never restarts itself
into a running state after a pause**: silence is a decision, and it should look like one.

### 7.8 Backends

`sim` — deterministic windows/tree/capture, **the CI backend**; §5's mock contract holds: right
token+generation ⇒ ok, stale generation ⇒ refused, wrong token ⇒ refused. `null` — every action refused
`backend-unreachable`, for wiring tests. `windows` — the real one: UIA element-first (`InvokePattern`,
`ValuePattern.SetValue`, `WindowPattern`), pixels as a labelled fallback, `PrintWindow`-then-screen
capture, downscale to ≤`capture_max_width` and **≤2 MB** (ntfy.sh's attachment cap), DPI-safe by using
physical pixels from the tree rather than guessing a scale factor. §7.1's refusals and §7.5's chain are
backend-independent, so all of it is testable on any box.

**Status of `windows` (built, not yet field-proven).** `tools/winuia.py` implements it: one enumeration
feeds both the tree an agent reads and the element a click resolves to (`_flat`), which is the difference
between a stale id that refuses and a stale id that clicks the wrong button. Captures are refused — not
downscaled, not guessed — when `PrintWindow` yields no pixels or when the frame would break ntfy's 2 MB
cap; a 0-element tree on a *visible* window is flagged as probable UIPI rather than left to read as "there
is nothing in this app"; unimplemented side effects raise instead of returning ok. `tests/test_winuia.py`
(18 cases) proves those decisions against a fake transport on Linux. **The real `uiautomation` calls are
unexecuted until the first run on the PC**, so the honest rung for `windows` is logic-tested, not
field-proven — PCB-01 exists to change that and nothing before it claims otherwise.
### 7.9 What M1's implementation added to §7.1–§7.8 (recorded, not silently changed)

Written after `tools/bridge.py` + `tests/test_bridge.py` ran on the sim backend. Four of these came
out of writing the tests, not out of design; they are recorded here because §1–§6 are frozen and the
Director reads this file, not the source, to know what the bridge will do to his machine.

1. **A read needs no permission but does need a connection.** Reads skip the scope, fence and approval
   gates (§7.3), and they keep working while paused — the lens is how a human checks on a machine they
   just stopped. They are *not* allowed to report success against a dead backend: `backend-unreachable`
   on a `capture` is honest, an empty tree is not (that is UIPI's silent-zero failure in a new costume).
2. **Pause is a file, not a variable.** `pause` writes `pc/PAUSE` *before* anything else and `act` reads
   it first, because the human's `bridge.py pause` and the agent's `bridge.py act` are different
   processes. A halt that resets on the next command is not a halt.
3. **The availability gate sits *above* the approval gate.** On `ask=important` a parked write waits for
   a human yes; if the backend is unreachable, the yes would be spent on nothing and the queue would
   fill with fakes. The bridge refuses first.
4. **`stale-generation` is only ever the world's own verdict.** `tools/world.py` answers a bad grant with
   its dedicated `EXIT_STALE` (5); any *other* non-zero exit — a parser error, a moved CLI flag, an
   unreadable `world/lock.json` — is `fence-unreachable`. A broken referee must never be dressed up as
   "your grant expired, go re-acquire and retry", because that is the one message that teaches an agent
   to retry against a fence that has stopped working. (`--fence static --lock-file <f>` lets a test move
   the world's answer on purpose; `tests/test_bridge.py` also runs the real `world.py` end to end.)

Also: `--token/--generation` are accepted on `act` itself (the grant belongs to the call that needs it),
the tidy budget is counted from the receipt log rather than memory, and `status` reports `ask`,
`pending`, `pending_stale_min`, `auto_pause_due`, `hold_timeout_min`, `tidy_max_per_hour`, `fence`,
`receipts`, `paused`, `pause_reason`.

Evidence: `python3 tests/test_bridge.py` — 30 cases (+ `tests/test_winuia.py`, 18 cases, for the Windows backend's decisions), all five refusal ids (§7.1's three, `no-scope`,
`not-allowlisted`, plus `bad-args`/`bad-lock`), fence stale/wrong/unreachable, `ask` ×3, tidy budget and
`tidy-elsewhere`, chain tamper detection, approve/deny, auto-pause, fail-closed backends, the localhost
HTTP surface. Stdlib only; no network, no Windows required. `tools/winuia.py` is the sole file that imports
Windows-only modules, and it imports them inside `build_transport()` so the policy layer stays importable
and testable anywhere.

---

## 8. First run on a real PC (the short version; the long one is the manager's `RUN-ON-THE-PC.md`)

```powershell
git clone https://github.com/dreamer2664/mars.git $HOME\mars; cd $HOME\mars
git fetch origin agent/fabius/pccontrol-m1; git checkout -b pc-01 FETCH_HEAD   # the BRANCH, not the PR ref
# (a second run: `git fetch origin agent/fabius/pccontrol-m1; git reset --hard FETCH_HEAD` —
#  fetching into a checked-out branch is refused, and refs/pull/N/head lags the branch by a beat)
python tools\bridge.py selftest                                 # ~60 s, nothing real: PASS-POLICY-ONLY
pip install uiautomation
python tools\bridge.py --backend windows selftest               # the desktop answer: PASS or FAIL
```

`python tools\bridge.py --backend windows --token <t> --generation <n> act capture --window id-0 --save-to lens.png`
(window ids read `id-<n>` on both backends; `--window 0`, `--window id-0` and `--window Roblox` all resolve.)

**Every answer names its own source.** `status`/`selftest` print `checkout` and every receipt carries `at` —
the short SHA of the code that answered. This was added because a second field run produced output *identical*
to the first: not a failed fix, a `git fetch` that had failed, and nothing on the page could distinguish the
two. A total that moves with the backend is the same class of trap, so `selftest` is **13 checks on every
backend**, sim included.

**Field result, 2026-10-16 19:06Z — the first real Windows run (Carlo's desktop).** `--backend windows
selftest` came back `FAIL 9/12`, and it was right to: `observe:tree` raised `TypeError: 'bool' object is
not callable` and `observe:capture` raised `'WindowControl' object has no attribute 'CaptureToRect'`. Three
API names in `UiaTransport` had been written against memory instead of the package — `IsEnabled`/`IsOffscreen`
are *properties* (no parentheses), the capture call is `ToBitmap()` → `Bitmap.ToBytes("png")` (no PIL
needed), and `WaitUntilReady` does not exist. All three corrected against the source of the published wheel
(read from PyPI, not assumed), and `window enumeration` now reports 4 top-level windows where the sim shows 3,
which is the sim being a toy and not a bug. `probe()` checks the module and method shapes at construction and
`verify_shapes()` checks the property shapes against a live control, so a renamed or re-kinded API is a
sentence about the library instead of a red check with a stack trace. **This is E5 in the only honest sense
available here: an observed live process on the owner's machine, and it found what 82 sandbox tests could
not.** The re-run on the PC is PCB-01 proper.
is then one frame of your own desktop, through the lease, with a digest in the chain.

Three properties of this section are load-bearing and are enforced in code, not in prose:

* `--backend windows` **refuses to run with `--fence off`** (`no-fence`). The default fence is chosen from
  what the bridge would act *on*: `world` for a real desktop, `off` for the sim. An unfenced desktop bridge
  is a remote control with extra steps.
* `selftest` has three verdicts, not two. `PASS-POLICY-ONLY` means "the gates work, no machine was touched" —
  reading it as "it works" is the misjudgement the third verdict exists to prevent.
* Every refusal is printed to **stdout as one JSON object** with exit code 1, so an agent, a script and the
  phone bridge all read the same answer. A tool that reports success on failure is worse than no tool.
