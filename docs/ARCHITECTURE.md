# Architecture

How blink-light decides what the light should do, and what it costs to add more.

## The short answer

**Both, and they are independent.**

There is an always-running loop *and* a stateless hourly trigger. They do the same
job by different means and cannot double-fire, because both consult the same
slot-keyed state file before acting.

The loop does **not** fire on minute change. It sleeps until the next scheduled
moment, waking at least once a minute to re-check its arithmetic.

## The three moving parts

| Component | Lifetime | Wakes | What it does |
|---|---|---|---|
| **Scheduler loop** (`chime run`) | Always on, started at logon | Every ≤60s | Hourly chime + daily show |
| **Hourly task** (`chime now`) | ~1s, then exits | Once at `:00` | Hourly chime only. Backstop. |
| **Watcher** (`watch start`) | Always on, opt-in | Every 5s | Calendar / rules / timers / presets |

The scheduler loop and the watcher are separate processes with separate jobs. The
watcher paints *ongoing state* (are you in a meeting?). The scheduler fires
*moments* (it is 5pm).

## How the scheduler loop actually works

`run_chime_loop` in `blink_light/chime.py`:

```
wake
├── chime due?   date math + read chime-state.json   → pulse if due
├── show due?    date math + read show-state.json    → play scene if due
└── sleep  min(next chime slot, next show slot, 60s) + 0.5s
         (in 2s slices, checking only for a stop request)
```

Three consequences worth internalising:

1. **Away from a slot the sleep is a flat 60s.** At 14:05 the next chime is 55
   minutes out, so it sleeps 60s, wakes, does two file reads, sleeps again.
2. **Approaching a slot the sleep lands exactly on it.** At 14:59:10 the next slot
   is 50s away, so it sleeps 50.5s and wakes just past the hour. Measured latency
   in practice: **0.657s** after the slot.
3. **The sleep is interruptible.** It is served in 2-second slices that check only
   for the stop file — the effects are still evaluated once per wake, so
   responsiveness costs nothing on the hot path.

The 60s cap exists so that a laptop resuming from sleep or a DST shift is noticed
within a minute rather than at the end of a 55-minute sleep.

That slicing is not cosmetic. `stop_chime_loop` waits 8 seconds for the loop to
retire itself before resorting to a kill, and on Windows that kill is
`TerminateProcess` — no cleanup, no shutdown log. With a single 60-second sleep the
graceful path almost never won. With slices it reliably does: measured stop time is
**1.1s**, and the shutdown is logged.

## Logging

The loop runs detached behind `wscript` with no console, so its only other failure
signal would be a scheduled-task result code. It logs to
`%LOCALAPPDATA%\BlinkLight\blink-light.log`:

```
15:03:57 INFO [chime] Loop started (PID 151948)
15:04:01 INFO [chime] Loop stopped (stop-file) after 1 wakes, 0 chimes, 0 shows
```

A failure in one effect is caught, logged, and stepped over rather than taking the
loop down — one bad device call should not cost you the rest of the day's schedule.

### Why a slot, not a timestamp

Every effect resolves the current wall-clock time to a **slot** — the most recent
scheduled moment at or before now.

```python
current_slot(14:37, minute=0)  ->  14:00
current_slot(16:20, at=17:00)  ->  yesterday 17:00
```

The slot is then checked against three gates before anything lights up:

| Gate | Purpose |
|---|---|
| `catch_up_window_seconds` (300) | A slot more than 5 min stale is skipped. A machine that wakes at 14:40 does not get a surprise 14:00 pulse. |
| `respect_quiet_hours` | Skip inside `settings.quiet_hours`. |
| `last_slot` in the state file | The dedupe. Already fired this slot? Do nothing. |

That last gate is what makes the design safe. The state file is keyed by slot, not
by "last run time", so **any number of drivers can race and you still get exactly
one pulse per hour.** The loop, the scheduled task, and the watcher can all be
running at once — first one there wins, the rest no-op.

## What it costs (measured on this machine)

The full logon-started process chain:

| Process | Working set | Why it exists |
|---|---|---|
| `wscript.exe` | 36.2 MB | Hides the console window |
| `cmd.exe` | 19.4 MB | `blink-light.bat`, bootstraps `.venv` |
| `python.exe` (venv redirector) | 15.4 MB | venv shim, spawns the real interpreter |
| `python.exe` (the loop) | 34.4 MB | The actual work |
| **Total** | **105.4 MB** | |

CPU, measured on the running loop:

```
uptime 0:26   CPU 0.391s
uptime 2:00   CPU 0.391s     <- unchanged
```

**All of it was interpreter startup.** Steady-state CPU is zero to three decimal
places. A wake costs some `datetime` arithmetic and two small JSON reads.

Note the shape of that table: **the Windows launcher scaffolding costs more than
the Python does.** 71 MB of the 105 MB is wscript + cmd + the venv shim, paid once
whether the loop does one thing or fifty.

## So: will more logic make it heavier?

Mostly no. The daily rainbow show is the proof — it was added to the existing loop
and cost **zero** additional processes, zero additional wakes, and one extra JSON
read per minute.

What actually costs something is *per-wake work*, and only some kinds:

| You want to add | Where it goes | Cost |
|---|---|---|
| A new condition on existing signals (idle, process running, file exists, time range, battery) | `rules` in `blink-light.json` | **Zero.** No code. |
| Another scheduled moment (a 9am standup pulse, a Friday show) | Scheduler loop, next to the show | **Near zero.** One more date check and file read per minute. |
| Reacting to system state continuously | The watcher | Moderate — 5s ticks, and `psutil` enumerates processes each one. |
| Anything network, COM, or subprocess (Outlook, an HTTP API, `git`) | The watcher, behind a cache | **This is the real cost.** Outlook COM is why the watcher caches its calendar poll to every 30s rather than every tick. |

### The rule of thumb

Put it in the scheduler loop if it fires **at a time**. Put it in the watcher if it
reflects **a state**. Cache anything that leaves the process.

Adding a fourth, fifth, and sixth scheduled effect to the loop would not
meaningfully change any number in this document. Adding one uncached network call
on a 5s tick would.

## Precedence (watcher only)

The scheduler's effects are one-shots played *over* whatever is showing; they do
not participate in precedence and the watcher repaints underlying state on its next
tick. The watcher's own chain, highest first:

1. Manual override
2. Calendar (when `calendar.enabled`)
3. Active timer / routine phase
4. First matching local rule
5. Quiet hours
6. `settings.default_action`

## Where things live

| Path | Role |
|---|---|
| `blink_light/chime.py` | Hourly chime, the scheduler loop, scheduled-task management |
| `blink_light/show.py` | Daily show — same slot/dedupe shape as the chime |
| `blink_light/watcher.py` | The state loop and its precedence chain |
| `blink_light/device.py` | blink(1) I/O, scene playback, on-device patterns |
| `blink_light/config.py` | Merge + validation, including the show's duration cap |
| `blink_light/defaults.py` | Built-in presets, scenes, and the generated rainbow swirl |
| `%LOCALAPPDATA%\BlinkLight\` | Runtime state: PID files, slot state, log |

## Scenes and the 32-line limit

A blink(1) stores up to **32 pattern lines** on the device. When a scene fits and
does not loop, `device.py` uploads it and lets the hardware run it — no Python in
the timing path, so playback is smooth even if the machine is busy. Scenes that
loop, or exceed 32 lines, fall back to a host-driven thread.

The rainbow swirl is built to stay on the fast path: 29 lines, non-looping.

Each line is a *fade* to a color over a duration, not a jump, which is what makes
the swirl read as motion rather than a strobe. The two LEDs (`led: 1` and `led: 2`)
are written alternately and held half a hue-rotation apart, so the pair always
shows complementary colors travelling around the wheel. A brightness envelope
swells from 45% to 100% and back:

```
idx  0  ( 52,  0,  0)  #####
idx  9  (218,  0,218)  #####################
idx 13  (255,  0,  3)  ########################   <- peak
idx 19  (123,202,  0)  ###################
idx 27  (  0, 52, 52)  #####
idx 28  (  0,  0,  0)  fade out
```

(Read back off the device with `read_pattern_line`. Values are gamma-corrected by
the `blink1` library on write, so they differ from the authored hex.)

`config.py` measures the scene at validation time and refuses to start if it runs
longer than `show.max_seconds`, so the 10-second cap is structural rather than a
comment.
