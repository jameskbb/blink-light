# blink-light

Windows-first [`blink(1)`](https://blink1.thingm.com/) utility with a self-bootstrapping batch launcher, an hourly chime, one-shot light controls, Outlook-calendar watching, timers, local rule watching, persistent overrides, and opt-in startup registration.

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

### How the runner works

The scheduled task runs `wscript.exe "<repo>\blink-light-chime.vbs"`, which launches
`blink-light-chime.bat` with a hidden console. That calls `blink-light.bat chime now`,
which bootstraps `.venv` if needed and pulses.

Three things can drive the chime and all three are safe to run together:

| Driver | Command | Notes |
| --- | --- | --- |
| Scheduled task | `chime install` | Survives reboot, no daemon. The recommended runner. |
| Foreground loop | `chime run` | Sleeps until each slot. Good for a terminal you keep open. |
| Background watcher | `watch start` | Chimes between watcher ticks, alongside calendar colors. |

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

blink-light.bat startup enable
blink-light.bat startup status
```

## Config

The repo-local config file is `blink-light.json`. It contains:

- `device.serial`
- `chime`
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
| `blink-light-chime.bat` | Runner invoked by the scheduled task; calls `chime now`. |
| `blink-light-chime.vbs` | Hides the console window for the scheduled task. |
| `blink-light.json` | Config. |
| `blink_light/chime.py` | Hourly chime: slots, dedupe, loop runner, scheduled-task management. |
| `blink_light/watcher.py` | Background loop, action precedence. |
| `blink_light/cli.py` | Argument parsing and command dispatch. |
| `tests/` | `unittest` suite, no hardware required. |

## Tests

```bat
python -m unittest discover -s tests -v
```
