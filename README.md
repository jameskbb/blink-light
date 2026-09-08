# blink-light

Windows-first [`blink(1)`](https://blink1.thingm.com/) utility with a self-bootstrapping batch launcher, an hourly chime, a daily light show, one-shot light controls, Outlook-calendar watching, timers, local rule watching, persistent overrides, and opt-in startup registration.

**Docs:** [Architecture](docs/ARCHITECTURE.md) — how the scheduling works and what it costs to extend · [Runbook](docs/RUNBOOK.md) — setup, verification, troubleshooting, teardown.

## Hourly Chime (start here)

One white pulse at the top of every hour. Two commands:

```bat
blink-light.bat chime test
blink-light.bat chime install
```

`chime test` pulses right now so you can confirm the device works. `chime install`
registers a Windows scheduled task named **BlinkLight Hourly Chime** that fires at
`:00` every hour, whether or not the watcher is running.

Verify and remove:

```bat
blink-light.bat chime status
blink-light.bat chime uninstall
```

## Daily Light Show

A rainbow swirl at **17:00** every day, capped at 10 seconds.

```bat
blink-light.bat show test
blink-light.bat show status
```

`test` plays it now without consuming today's slot. It runs on the same scheduler
loop as the chime, so nothing extra to install.

It is a hue rotation, not a strobe: both LEDs are held half a turn apart in color
and every step is a *fade*, with brightness swelling from 45% to full and back
before a clean fade to black. 29 steps, 9.92s, uploaded to the device so playback
stays smooth even when the machine is busy.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch. |
| `at` | `"17:00"` | Time of day, `HH:MM`. |
| `scene` | `"rainbow_swirl"` | Any non-looping scene in `scenes`. |
| `max_seconds` | `10` | Hard cap. A longer scene fails `config validate`. |
| `catch_up_window_seconds` | `300` | How late a missed show may still play. |
| `respect_quiet_hours` | `true` | Skip inside `settings.quiet_hours`. |

The cap is enforced at validation time, not by truncating playback — point `scene`
at something longer and the CLI refuses it with the measured duration.

```bat
blink-light.bat show now
blink-light.bat show now --force
```

## Start It At Boot

```bat
blink-light.bat autostart enable
```

Registers a scheduled task named **BlinkLight Autostart** that starts the chime
loop at every logon, and starts it immediately so you do not have to reboot to
begin. Console window is hidden.

```bat
blink-light.bat autostart status
blink-light.bat autostart disable
blink-light.bat autostart enable --no-start
```

`disable` stops the running loop and removes the task. `--no-start` registers the
task but waits for the next logon.

**Trigger is logon, not machine start.** The blink(1) is a USB device on your
desk, so the runner belongs in your interactive session rather than in the
pre-logon SYSTEM context. In practice that means it comes up when you sign in.

**Only one runner can exist.** The launcher waits on the loop, so the task action
stays alive as long as the loop does, and Task Scheduler's default *do not start a
new instance* policy makes a second logon a no-op. Even if a second one did start,
the shared dedupe file means you would still get exactly one pulse per hour.

### Do I need both tasks?

No, but both is the sturdier setup and they cannot double-pulse:

- **BlinkLight Autostart** is the runner. A live process, chimes on the minute.
- **BlinkLight Hourly Chime** is the backstop. Stateless, so it still fires if the
  loop died, the machine was asleep at the top of the hour, or you never logged in.

Keep both unless you have a reason not to. To go loop-only, `chime uninstall`.

### How the runner works

The scheduled task runs `wscript.exe "<repo>\blink-light-chime.vbs"`, which launches
`blink-light-chime.bat` with a hidden console. That calls `blink-light.bat chime now`,
which bootstraps `.venv` if needed and pulses.

Three things can drive the chime and all three are safe to run together:

| Driver | Command | Notes |
| --- | --- | --- |
| Logon task | `autostart enable` | Runs `chime run` from every logon, console hidden. Also drives the daily show. |
| Hourly task | `chime install` | Stateless backstop, no daemon. Fires even if the loop is dead. |
| Foreground loop | `chime run` | Sleeps until each slot. Good for a terminal you keep open. |
| Background watcher | `watch start` | Chimes between watcher ticks, alongside calendar colors. |

Stop a running loop with `chime stop`. Never end the scheduled task by hand — see
[the runbook](docs/RUNBOOK.md#orphaned-loops-after-killing-the-task-by-hand).

Every driver writes the hour it fired to `%LOCALAPPDATA%\BlinkLight\chime-state.json`
and skips a slot that is already recorded, so the chime fires **once per hour** no
matter how many drivers are active.

### Chime config

Lives under the top-level `chime` object in `blink-light.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch. |
| `color` | `"#FFFFFF"` | Pulse color. |
| `secondary_color` | `"#000000"` | Color faded back to after the pulse. |
| `on_ms` / `off_ms` | `400` | Fade-up and fade-down durations. |
| `count` | `1` | Pulses per chime. |
| `minute` | `0` | Minute past the hour to fire. Re-run `chime install` after changing it. |
| `catch_up_window_seconds` | `300` | How late a missed slot may still fire (covers a laptop waking up). |
| `respect_quiet_hours` | `true` | Skip chimes inside `settings.quiet_hours`. |

Other chime commands:

```bat
blink-light.bat chime now
blink-light.bat chime now --force
blink-light.bat chime run
```

`now` fires only if this hour's pulse is still due. `--force` ignores quiet hours, the
catch-up window, and the already-fired guard. `test` pulses without consuming the hour.

## Quick Start

Run everything through `blink-light.bat` from this repo root:

```bat
blink-light.bat
blink-light.bat devices
blink-light.bat status
blink-light.bat preset list
blink-light.bat preset run focus
blink-light.bat timer start pomodoro
blink-light.bat watch start
blink-light.bat chime install
```

The batch file creates `.venv`, installs `requirements.txt` when needed, and then runs `python -m blink_light`.

If `calendar.enabled` and `calendar.auto_watch_on_launch` are both `true`, running `blink-light.bat` with no arguments starts the watcher in the background immediately.

## Calendar Behavior

The default config is Outlook-first and maps your calendar like this:

- `green` when nothing is currently scheduled
- `yellow` single blink when the next meeting is 10 minutes out
- `orange` single blink when the next meeting is 2 minutes out
- `red` during an active booked meeting
- `purple` during an active meeting whose Outlook busy status is `Free`

Notes:

- The current implementation polls the default Outlook calendar on Windows via Outlook COM.
- All-day events are ignored by default.
- Reminder blinks are one-shot per event, so the light returns to its normal availability color after the blink.
- `watch start` and the no-argument launch path use the same watcher logic.

## Useful Commands

```bat
blink-light.bat light color "#00C853"
blink-light.bat light flash "#D50000" --count 4
blink-light.bat light pulse "#FFB300" --count 0
blink-light.bat light scene timer_done_scene

blink-light.bat override set --preset busy --expires-in 30m --reason "heads down"
blink-light.bat override clear

blink-light.bat timer start 25m
blink-light.bat timer start pomodoro
blink-light.bat timer pause
blink-light.bat timer resume
blink-light.bat timer stop

blink-light.bat watch once
blink-light.bat watch run
blink-light.bat watch start
blink-light.bat watch stop

blink-light.bat autostart enable
blink-light.bat autostart status

blink-light.bat startup enable
blink-light.bat startup status
```

`autostart` runs the chime loop at logon. `startup` is the older Startup-folder
mechanism and launches the full calendar watcher instead; use it if you want
calendar colors, not just the chime.

## Config

The repo-local config file is `blink-light.json`. It contains:

- `device.serial`
- `chime`
- `show`
- `presets`
- `scenes`
- `routines`
- `rules`
- `settings`

The watcher precedence is:

1. manual override
2. active timer/routine phase
3. first matching local rule
4. quiet hours
5. default action

Supported local rule conditions:

- `idle_seconds_gte`
- `process_running_any`
- `file_exists`
- `file_contains`
- `time_between`
- `battery_below_percent`
- `charging`
- `timer_active`
- `routine_phase`

Calendar config lives under the top-level `calendar` object in `blink-light.json`.

The chime is deliberately outside that precedence chain: it is a one-shot pulse
played on top of whatever the light is already showing, and the watcher repaints the
underlying state on the next tick.

## Repo Layout

| Path | Purpose |
| --- | --- |
| `blink-light.bat` | Launcher. Bootstraps `.venv`, installs `requirements.txt`, runs the CLI. |
| `blink-light-chime.bat` | Runner invoked by the hourly task; calls `chime now`. |
| `blink-light-chime.vbs` | Hides the console window for the hourly task. |
| `blink-light-autostart.bat` | Long-running runner started at logon; calls `chime run`. |
| `blink-light-autostart.vbs` | Hides the console and waits on the loop (single-instance guard). |
| `blink-light.json` | Config. |
| `blink_light/chime.py` | Hourly chime: slots, dedupe, scheduler loop, scheduled-task management. |
| `blink_light/show.py` | Daily light show, same slot/dedupe shape as the chime. |
| `blink_light/watcher.py` | Background loop, action precedence. |
| `blink_light/cli.py` | Argument parsing and command dispatch. |
| `docs/` | Architecture and runbook. |
| `tests/` | `unittest` suite, no hardware required. |

## Tests

```bat
python -m unittest discover -s tests -v
```
