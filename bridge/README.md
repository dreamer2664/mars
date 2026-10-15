# bridge/ — the desktop bridge (the colony's executor)

*The interface is frozen: [`docs/BRIDGE.md`](../docs/BRIDGE.md) v1. This README is
the operations side — how to run it, what each mode really does, and what has
and has not been proven.*

## What this is

A tiny local HTTP service that runs on the Director's Windows PC. The world
grants a lock (`world/lock.json`, written only by `tools/world.py`); the bridge
is the only thing that can *act*, and only for the current holder with the
matching token **and** generation. It performs only the M1 allowlist
(`upload_file`, `set_metadata`, `read_published_url`), returns one receipt per
action, and **never writes world state**.

## Run it

```
python3 bridge/serve.py                # the real road: attaches to the browser over CDP (port 9222)
python3 bridge/serve.py --dry-run      # no browser; same code path, receipts labelled "dry-run"
python3 bridge/serve.py --sim          # Linux simulator: mock browser, full protocol, for tests
python3 bridge/serve.py --self-test    # print the dry-run self-test and exit
```

Windows: make a desktop shortcut whose target is
`py C:\mars\bridge\serve.py` and name it **"Mars bridge"**. A small black
window prints `bridge: listening …` — leave it open while work is running
(docs/BRIDGE.md §6). It listens on `127.0.0.1:9333` only — nothing outside the
PC can reach it.

## Protocol

```
POST /status  -> {browser_up, active_channel, lock:{holder, generation, ...}, busy, allowlist}
POST /act     -> {token, generation, action, args:{...}} -> receipt
                 refusals: no-lock · stale-generation · wrong-token ·
                           not-allowlisted · missing-args:... ·
                           browser-unreachable · unexpected-dialog
```

Order of checks is fixed: **fence first** (token+generation), then allowlist,
then args, then act. A caller with no valid grant learns nothing about the
allowlist — and cannot act at all.

## Fail-closed, by construction

Browser unreachable, unexpected dialog, unknown channel, executor exception:
the bridge does nothing, returns `ok:false` with the reason, and still writes
the receipt. The bridge never releases the lock — only the holder, expiry, or
a sweep does. Receipts are written to `bridge/receipts/` on the PC (gitignored:
they are live-run evidence, not source; screenshots likewise never leave the
machine).

## What is proven, and what is not (honesty section)

**Proven here, on Linux, without Windows** (`tests/test_bridge_sim.py`, 16
checks): fencing equality including future generations; stale/wrong-token
refusals with reasons; allowlist refusals by name; fail-closed on browser-down
and unexpected-dialog; one receipt per action (same-second refusals keep
separate receipts); the bridge provably never writes `world/`; full lifecycle
against the real `world.py` — release kills the old grant, a new holder gets a
new generation, and the old token stays dead.

**Not proven yet, by design:** the YouTube Studio choreography over CDP
(`CdpExecutor`). Until the first M1 act on the Director's PC — with him
watching, per docs/BRIDGE.md §5 — it fail-closes: browser reachable or not,
every action returns `ok:false, reason:"cdp-choreography-not-yet-run"` rather
than claiming a result nobody observed. Nothing in this repo can fake a
publication.

## Files

| file | role |
|---|---|
| `serve.py` | the service: HTTP surface, check order, receipts, modes |
| `fencing.py` | the gate: token+generation equality against `world/lock.json` |
| `actions.py` | the allowlist, arg validation, receipt shape and writing |
| `executors.py` | simulator / dry-run / CDP executors, one interface |
