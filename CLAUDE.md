# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Reverse-engineered BLE control of a Sony soundbar (HT-B600 / BRAVIA Theatre Bar 6, MAC `54:80:FC:3E:F7:67`). There is no vendor API — every byte sequence in this repo was derived from btsnoop captures of the iOS app. The analysis scripts that produced them are not kept; re-deriving the protocol means capturing new btsnoop logs.

Everything lives in `bravia_control.pyw` (Python 3.13 + `bleak` + `pywin32`): an always-on Windows daemon, a CLI, and an on-screen volume display. It autostarts via `bravia_daemon.xml`.

## Commands

uv-managed, `.venv/` at repo root:

```powershell
uv sync                                   # install deps
uv run python bravia_control.pyw daemon   # run daemon in foreground (prints logs)
uv run python bravia_control.pyw status   # read volume
uv run python bravia_control.pyw set 20   # set volume (0-100)
uv run python bravia_control.pyw up 2     # relative change
```

The `.pyw` extension matters: in production the daemon is launched with `pythonw.exe` so no console window appears. Running it with plain `python` for debugging is fine and is how you see the `print()` diagnostics.

In daemon mode `sys.stdout` is replaced by `DaemonLog`, which timestamps every existing `print()` into `bravia_daemon.log` (truncated past 512 KB). This exists because `pythonw.exe` leaves `sys.stdout` as `None`, making `print()` a silent no-op — which is why every historical failure left zero evidence. **Read that log first when the daemon misbehaves.**

The script calls `site.addsitedir('.venv/Lib/site-packages')` before any third-party import, so the scheduled task can run a bare interpreter with no venv activation. Do not swap this back to `PYTHONPATH`: only a real site directory gets its `.pth` files processed, and `pywin32.pth` is what puts `win32/` and `win32/lib/` on `sys.path`. Under `PYTHONPATH` alone `import win32gui` fails and the OSD silently loses its click-through styling. Pointing the task at `.venv\Scripts\pythonw.exe` is also wrong — that trampoline spawns a console `python.exe` and a black window appears.

The daemon autostarts at logon via a Windows Scheduled Task. `bravia_daemon.xml` is a template with `{{USER_SID}}`, `{{PYTHONW}}` and `{{REPO_DIR}}` placeholders; `install_task.ps1` (needs elevation) fills them from the current user, the `home` entry of `.venv\pyvenv.cfg`, and its own location, then runs `schtasks /Create /XML`. Re-run it after moving the repo or changing the Python version. Keep machine-specific paths and SIDs out of the template.

Two settings in there are load-bearing and were previously wrong: `ExecutionTimeLimit` must stay `PT0S`, because the old `PT72H` had the scheduler kill the daemon after three days of uptime; and the action must invoke `pythonw.exe` **directly** rather than wrapping it in `powershell.exe -Command`, because a wrapped daemon is a grandchild that `schtasks /End` cannot stop — it reports success and leaves the process running, still holding both port 9105 and its BLE handles.

Managing it: `schtasks /End /TN bravia_daemon` and `schtasks /Run /TN bravia_daemon` both work unelevated; `Stop-Process` does not, since `RunLevel=HighestAvailable` puts the daemon above a normal shell. Run `schtasks` from PowerShell, never Git Bash — MSYS rewrites `/TN` into a path (use `//TN` if you must).

There are no tests.

## Protocol (the part that matters)

Volume lives behind a Sony proprietary GATT service. Write to `5b833c66-…`, subscribe to notifications on `5b833c67-…`.

**Reading** — write the 17-byte `MINI_VOL_QUERY` (`3e070000000010000001000a025661db3c`), then scan the *accumulated* notification hex for the marker `0a025661120208`; the volume is the byte at marker+7. This tiny query deliberately replaces the ~1.2 KB query the iOS app sends — the large one overloads the soundbar's BT processor and makes it mute itself. Do not "restore fidelity" to the iOS packet.

**Writing** — a mandatory 4-step sequence, not a single write:

1. 20-byte header
2. `[0x02, 0x08, vol, (vol & 0xFE) + 0x30, 0x3c]` — that 4th byte is the checksum
3. `3e0100000000023c` (echo ACK), after ~200 ms
4. `3e0100000000013c` (close transaction), after another ~200 ms

Skipping TX3/TX4 leaves the soundbar's state machine open and subsequent commands fail. The same TX4 also terminates a read transaction.

**Feedback suppression** — after a write the soundbar emits a burst of stale volume notifications that would fight the local cache. The notification handler is gated behind an `ignore_ble_until` deadline (9 s during the write, then 6 s) and *clears* the buffer on each suppressed packet rather than just dropping it. Shortening these windows reintroduces volume flapping.

**Windows BLE quirk** — after connecting, sleep ~2 s, call `clear_cache()`, and touch `client.services` before any I/O, or characteristic discovery silently returns stale/empty results. (`clear_cache()` does not exist on the pinned bleak and is swallowed by the bare `except`; the settle sleep is what actually matters.)

**Truncated service lists** — Windows sometimes returns only `1800`/`1801` for the soundbar, with the whole `5b833e…` Sony profile missing, so `start_notify(NOTIFY_CHAR)` fails. This is process-scoped: once a BLE session in the daemon has leaked its GATT handles, *every* later connect sees the truncated list — including connects from unrelated processes — and it stays that way until the owning process dies. Reconnecting can never fix it, which is how the daemon used to end up listening on 9105 forever while answering `ERROR: Soundbar not connected`. `_connect_once` now checks `services.get_characteristic(NOTIFY_CHAR)` right after discovery and raises `StaleServicesError`; after `STALE_SERVICE_MAX_TRIES` in a row the daemon respawns itself via `_restart_process()` + `os._exit(0)`, rate-limited across restarts by the mtime of the `.daemon_restart` stamp file. One or two truncated attempts per reconnect are normal and recover on their own; the log shows them.

## Python daemon architecture

Four concurrent asyncio tasks in `BraviaDaemon.run()`:

- `connect_loop` — scan → connect → subscribe → seed the volume cache; reconnects automatically via `disconnected_callback`. `connected` is only set True *after* the cache is seeded, both so clients never see a half-ready daemon and so no command can reach `set_volume` while `_connect_once` still holds `conn_lock`.
- `poll_volume_loop` — 50 ms poll of the *in-memory* cache (no BLE traffic); when the value changes from something other than a local command, it pushes the new volume to the OSD. This is what makes physical-remote changes appear on screen.
- `heartbeat_loop` — see below.
- `asyncio.start_server` on `127.0.0.1:9105` — line protocol, one command per connection, replies `OK <vol>` or `ERROR: …`.

### Watchdog

The daemon's historical failure mode was going unresponsive while still holding the TCP port: either the soundbar stopped answering while Windows still reported "connected", or a `write_gatt_char` blocked forever inside `ble_lock` and every later command queued behind it. Three mechanisms prevent that, and removing any one of them brings the hang back:

- **Every BLE write goes through `_write()`**, which imposes `WRITE_TIMEOUT`. Never call `client.write_gatt_char` directly in daemon code. `connect()` and the teardown calls are bounded the same way.
- **`heartbeat_loop`** probes only after the link has been silent for `HEARTBEAT_INTERVAL` (the handler stamps `last_rx` on *every* notification, including ones the debounce window discards, so ordinary traffic suppresses probing entirely). After `HEARTBEAT_MAX_MISSES` unanswered probes — or if `client.is_connected` goes false without the callback firing — it forces a reconnect.
- **Any failed or timed-out command triggers `force_reconnect()`** rather than just returning an error, so a dead link is rebuilt instead of failing every subsequent command. Commands are additionally capped at `COMMAND_TIMEOUT`, which must stay below the CLI's 10 s socket read timeout.
- **Every background loop runs under `_supervise()`**, which restarts it if it raises. A bare `create_task()` swallows exceptions until GC, so a dead watchdog is indistinguishable from a healthy one from the outside. `find_device()` sits outside `_connect_once`'s `try`, so a WinRT scan error used to kill `connect_loop` outright and leave a daemon that never reconnected.
- **Failed connects back off exponentially** (capped at 30 s) instead of retrying once a second for days, and `run()` retries the port bind ten times so a self-restart can overlap the process it replaces.

`force_reconnect` only tears the link down; `connect_loop` rebuilds it on its next pass. Lock order is **always `conn_lock` → `ble_lock`**; taking `conn_lock` while holding `ble_lock` deadlocks the connect and reconnect paths, which is why `set_volume` *schedules* `force_reconnect` as a task instead of awaiting it.

`_query_volume_from_soundbar` deliberately lets write errors propagate — a query that cannot be sent means the link is dead and the connection must not be announced as usable.

The notification handler parses volume *inline* as bytes arrive, so `cached_vol` is always current and `status` never touches the radio. `ble_lock` serializes transactions; the notification buffer is trimmed at 1024 bytes to survive multi-day uptime.

The CLI frontend always tries the daemon socket first (instant) and falls back to the `direct_*` functions, which open their own BLE connection and take 3–5 s. The fallback covers both a missing daemon and a daemon that is listening but has no BLE link — without the latter, a wedged daemon would leave the user with no way to change volume at all. The `direct_*` path duplicates the protocol logic — fix bugs in both places.

`VolumeOSD` is a Tk window on its own thread, fed by a `queue.Queue` (Tk is not thread-safe; never call widget methods from the asyncio side). It is made click-through and Alt-Tab-invisible via `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW`, using `int(root.frame(), 16)` to get the true top-level HWND. `deiconify()` resets those extended styles, so they are re-applied on every show.

Volume range is 0–100 (`MAX_VOL = 100`), the hardware's real maximum.
