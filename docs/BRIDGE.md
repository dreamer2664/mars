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

> **Run box / shortcut users:** type it as ONE line — the `^` above is only for
> a terminal. In a shortcut, put the chrome.exe path in *Target*'s front and
> everything from `--user-data-dir` onward at the end of the same line.
> *(Setup-honesty review, 2026-10-15, scipio — interface sections untouched.)*

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
