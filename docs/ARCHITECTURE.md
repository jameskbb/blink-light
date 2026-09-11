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
| **Scheduler loop** (`chime run`) | Always on, started at logon | Every ≤60s | Chime, show, alarms, and opt-in GitHub polling |
| **Hourly task** (`chime now`) | ~1s, then exits | Once at `:00` | Hourly chime only. Backstop. |
| **Watcher** (`watch start`) | Always on, opt-in | Every 5s | Calendar / rules / timers / presets |

The scheduler loop and the watcher are separate processes with separate jobs. The
watcher paints *ongoing state* (are you in a meeting?). The scheduler fires
*moments* (it is 5pm).

There is a third path with no process of its own: **notifications**. An external
tool runs `notify run <event>`, the light flashes, the process exits. No slot, no
dedupe, no daemon — see [Herdr](HERDR.md) for the shipped example. Three kinds of
trigger, then:

| | Trigger | Dedupe |
|---|---|---|
| Scheduler | A time arrives | Once per slot |
| Watcher | State changed | Once per distinct action |
| Notify | Someone asked | None — every call flashes |

## How the scheduler loop actually works

`run_chime_loop` in `blink_light/chime.py`:

```
wake
├── chime due?   date math + read chime-state.json   → pulse if due
├── show due?    date math + read show-state.json    → play scene if due
├── alarms due?  date math + read alarm-state.json   → flash each that is due
├── GitHub due?  one conditional GET, plus a reviews read for each new update
│                on your own pull request, inside a 5s budget          → notify
│                new failures and pull-request activity
└── sleep  min(next chime, next show, next alarm, next GitHub poll, 60s) + 0.5s
         (in 2s slices, checking only for a stop request)
```

The alarms line is the proof of the claim below about extension cost: adding two
daily standup alerts cost one more file read per wake and no new process. Each
effect is a cheap no-op away from its slot.

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

### GitHub polling

`GitHubPoller` lives for the scheduler lifetime. A disabled integration does no
authentication, network I/O, or state I/O. Enabled polling uses at most one GET per
due wake. The next deadline joins the sleep candidates.

`gh auth token --hostname github.com` supplies the token through a hidden process
with a 5-second timeout. The token remains in memory. After HTTP 401, the next poll
refreshes it once. A second rejection requires a new login and loop restart.

HTTP calls have a 5-second timeout. The interval is the larger of the config value
and `X-Poll-Interval`, with a 60-second minimum. `Retry-After` can extend the wait.
The fixed URL preserves conditional requests through `If-Modified-Since`.

The first successful feed becomes a silent baseline. `github-state.json` stores
the newest update timestamp and the 200 most recent notification/update keys.
The integration saves that state atomically before device access. Quiet hours
and missing devices cannot replay consumed events. The token never enters state.

The CLI `github check` shares this state. Use `--dry-run` while the scheduler runs
to avoid concurrent state writes. The feed covers the 50 newest notifications,
so a burst of more than 50 updates can exceed its coverage.

**Pull-request detection** rides the same feed, at no extra request for most of
it:

- A review request or an @mention/team mention comes straight off the feed -
  no extra call. GitHub documents that a notification thread's `reason` can
  change on its latest update and may otherwise stick to a subscription, so
  each thread's reason is remembered (`threads` in the state file) and an
  unread thread that keeps its reason does not replay; it flashes again once
  you've read it on GitHub or once the reason changes.
- A review on a pull request you authored is not visible in the feed itself,
  so it costs one `GET {pull request}/reviews` per new update on your own
  pull request. The lower bound for a qualifying review is
  `max(previous watermark - 120s, review_floor)` - the 120s covers the lag
  between a review landing and GitHub bumping the thread - and the upper
  bound is that thread's `updated_at`. Your own login (read once per
  scheduler lifetime, like the token) and any `ignore_logins` are excluded.
- Matched review ids are deduped (`seen_reviews`, 200 most recent) so a later
  comment on the same thread does not re-flash an already-seen review. A full
  100-review first page reads the Link header's `rel="last"` page number and
  refetches from a URL built locally from that integer - never from the
  header's own URL, so the bearer token only ever reaches a GitHub API
  pull-request path.
- `github-state.json` gains `pr_initialized`, `threads`, `seen_reviews`, and
  `review_floor` alongside INT-01's fields.
- Upgrading from an Actions-only state, or enabling pull requests for the
  first time, sends one unconditional read (skipping `If-Modified-Since` once)
  so thread memory is built from a real read rather than baselining on
  nothing, then goes back to conditional requests.

That slicing is not cosmetic. `stop_chime_loop` waits 8 seconds for the loop to
retire itself before resorting to a kill, and on Windows that kill is
`TerminateProcess` — no cleanup, no shutdown log. With a single 60-second sleep the
graceful path almost never won. With slices it reliably does: measured stop time is
**1.1s**, and the shutdown is logged.

**The pull-request review budget** exists for the same reason. A poll can now
make several extra requests - a login read, a reviews read per new update on
your own pull request, sometimes a second page - and every flash already
blocks for its scene length. All of that shares one 5-second deadline set at
poll start; each extra request's timeout is capped at whatever is left of it,
with a 0.5-second floor below which a request is skipped rather than sent.
`stop_requested` is checked before every extra request and before every flash
after the first, so a scheduler stop during a slow poll still lands inside
`stop_chime_loop`'s 8-second grace instead of falling back to a kill.

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
Each alarm is attempted and reported on its own, so an unplugged light cannot
hide one alarm's miss behind another's.

The watcher appends to the same file, but only when the light's action changes;
at a 5-second tick, a line per tick would bury the scheduler's lines.

Neither process rotates the file while running. Windows will not rename a file
another process holds open, so a size-triggered rotation would fail mid-run and
drop lines. Instead each start moves a log over 5 MB aside to `blink-light.log.1`,
replacing the previous one; if the other process has it open, the move is
skipped and a later start tries again.

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
| Another scheduled moment (a 9am pulse, a Friday show) | An entry in `alarms` | **Zero.** No code — this is what `alarms` is for. |
| Reacting to system state continuously | The watcher | Moderate — 5s ticks, and `psutil` enumerates processes each one. |
| Calendar network or COM access | The watcher, behind a cache | Outlook COM and Graph use the calendar poll interval. |
| GitHub Actions failures and pull request activity | The scheduler, behind a 5s deadline | One conditional GET per ≥60s, plus one reviews GET per new update on your own pull request and one login GET per scheduler run, all inside the same budget. Measured: 1.512s for the first dry run, 0.897s for a cached-token HTTP 304 poll, 1.861s for the INT-02 dry run below. No extra permanent process. |
| Reacting to an event from another tool | A `notify` event plus that tool's own hook | **Zero standing cost.** Nothing polls; the other tool pays for the trigger. |

GitHub measurement (2026-09-10): the first live dry run took **1.512s**, including
the GitHub CLI token lookup and HTTP 200 response. It found eight baseline failures
and performed no state writes or flashes. This is one sample, not a latency guarantee.
The next scheduler poll returned HTTP 304 in **0.897s**, measured from its saved
start timestamp to its log entry. It reused the token and wrote one small state
file. Neither sample measures long-term CPU or memory use.

INT-02 measurement (2026-09-10): a `github check --dry-run` on this same feed,
after the pull-request code shipped but before the running loop's own state was
upgraded, took **1.861s** and reported `pr_baseline: true` — expected, since that
state predates pull-request tracking. Baselining a state does not read reviews,
so this sample measures the feed cost, not the reviews cost; the reviews GET has
not yet been measured against the owner's live account.

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
| `blink_light/alarms.py` | Named daily alarms; reuses the show's slot arithmetic |
| `blink_light/notify.py` | Named one-shot notifications; the integration entry point |
| `blink_light/github.py` | Conditional GitHub polling, authentication, dedupe, status, and pull-request review detection |
| `integrations/herdr/` | Herdr plugin manifest and event handler |
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
