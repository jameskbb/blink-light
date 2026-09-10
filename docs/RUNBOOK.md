# Runbook

Operating blink-light on a Windows machine: setup, verification, troubleshooting,
teardown. Example output omits account identifiers. GitHub checks require a local
GitHub CLI login and an enabled integration.

Run everything from the repo root. `blink-light.bat` creates `.venv` and installs
`requirements.txt` on first use, so there is no separate install step.

---

## GitHub Actions checks

1. Run `blink-light.bat github status` to check the login and last poll.
2. Run `blink-light.bat github check --dry-run` to preview without a flash or state change.
3. Run `blink-light.bat github test` to play the orange-red heartbeat once (2.40 seconds).
4. After a config change, run `blink-light.bat autostart enable` to restart the scheduler.
5. Read `%LOCALAPPDATA%\BlinkLight\blink-light.log` for `GitHub: poll 200`, `GitHub: poll 304`, or `GitHub: workflow run failed`.

The integration is off by default. Add `"github": { "enabled": true }` to the local
`blink-light.json`. Run `blink-light.bat config validate` before the restart.
The first real poll records a silent baseline. During quiet hours, the poll
consumes events without a flash. `github test` bypasses these gates.

| Result | Cause | Action |
| --- | --- | --- |
| `reason: disabled` | Polling is off | Enable `github.enabled` in the local config. |
| Missing CLI or login | `gh auth token` failed | Install GitHub CLI, then run `gh auth login --hostname github.com`. |
| HTTP 401 | The token was rejected | Wait for the next poll. If rejection persists, log in again and restart the loop. |
| HTTP 403 | Missing notification access or a rate limit | Run `gh auth status`. The feed needs `repo` or `notifications` scope. |
| Network or server error | The service is unavailable | Wait for the next poll. Expected errors produce one INFO line per hour. |

Unexpected faults retain tracebacks. A missing device produces the existing
hourly device note. `github status` shows the last result, watermark, and last
successful flash. The state file is `%LOCALAPPDATA%\BlinkLight\github-state.json`.

For a live acceptance check, observe the next workflow failure you trigger.
Outside quiet hours, it must produce one heartbeat and one failure log line.
Normal latency is about 60 seconds. A larger server interval increases latency.
Only the 50 most recently updated notifications enter each poll.

## 1. Setup from scratch

### 1.1 Confirm the device is seen

```bat
blink-light.bat devices
```

```json
{ "serials": [ "12ab34cd" ] }
```

An empty list means Windows is not enumerating the blink(1). Unplug and replug it
before touching anything else — no amount of config fixes a device that is not
there.

### 1.2 Write the config

```bat
blink-light.bat config init
blink-light.bat config validate
```

`init` auto-fills `device.serial` when exactly one device is connected. `validate`
is worth running after any hand-edit; it checks scene references, preset cycles,
and the show's duration cap.

### 1.3 Prove the effects work before scheduling them

```bat
blink-light.bat chime test
blink-light.bat show test
```

`test` fires immediately and does **not** consume the slot, so it will not stop the
real one firing later. Watch the light: one white breath, then a ~10s rainbow.

### 1.4 Install the two tasks

```bat
blink-light.bat autostart enable
blink-light.bat chime install
```

```json
{ "installed": true, "started_now": true, "trigger": "at logon",
  "task_name": "BlinkLight Autostart", "replaced_running_loop": false }
```

```json
{ "installed": true, "runs_at_minute": 0, "task_name": "BlinkLight Hourly Chime" }
```

`autostart enable` also starts the runner immediately, so you do not have to log
out and back in to begin.

### 1.5 Verify

```bat
blink-light.bat chime status
```

The four things to check:

| Field | Expected |
|---|---|
| `autostart.installed` | `true` |
| `autostart.loop.running` | `true` — a live loop with a PID |
| `scheduled_task.installed` | `true` |
| `next_slot` | the coming hour |

---

## 2. Verifying it really fires

Watching for a real `:00` is slow. Move the slot instead.

```bat
blink-light.bat chime status
```

Note `minute`. Then set `chime.minute` in `blink-light.json` to a minute two ahead
of now, restart the runner, and watch:

```bat
blink-light.bat autostart enable
type %LOCALAPPDATA%\BlinkLight\chime-state.json
```

```json
{
  "last_fired_at": "2026-09-08T14:29:00.657391-05:00",
  "last_slot": "2026-09-08T14:29:00-05:00",
  "source": "chime-run"
}
```

`source` tells you which driver won: `chime-run` is the loop, `chime-now` is the
hourly task, `watcher` is the watcher. **Put `chime.minute` back to `0` and run
`autostart enable` again when you are done.**

To confirm the show's scene without waiting for 5pm, read it back off the device:

```bat
blink-light.bat show status
```

`scene_seconds` must be at or under `max_seconds`. Config validation enforces this,
so a scene over the cap fails `config validate` rather than running long.

---

### Verifying the daily alarms

```bat
blink-light.bat alarm status
blink-light.bat alarm test standup_now
```

`status` shows each alarm's `next_slot` and when it last fired. `test` plays one
without consuming the slot, so it will not stop the real one firing.

`reason` explains why an alarm is not firing right now:

| `reason` | Meaning |
|---|---|
| `due` | Would fire on the next wake. |
| `already-fired` | Working as intended; it fires once per day. |
| `outside-catch-up-window` | The slot passed more than `catch_up_window_seconds` ago. Expected most of the day. |
| `wrong-day` | A `days` filter excludes today. |
| `quiet-hours` | Inside `settings.quiet_hours`. |
| `disabled` | `enabled: false`. |

**After editing alarms, restart the runner** — it reads config once at startup:

```bat
blink-light.bat autostart enable
```

## 3. Troubleshooting

### Start with the log

The loop is detached and has no console, so this is the first place to look:

```bat
type %LOCALAPPDATA%\BlinkLight\blink-light.log
```

```
15:03:57 INFO [chime] Loop started (PID 151948)
15:04:01 INFO [chime] Loop stopped (stop-file) after 1 wakes, 0 chimes, 0 shows
```

A `Loop started` with no matching `Loop stopped` means it is still running. A
stack trace means an effect failed — the loop logs and carries on rather than
dying, so one bad device call does not cost you the day's schedule.

An undocked machine is the exception, and gets one line rather than a
traceback:

```
09:00:00 INFO [chime] Chime skipped, device not connected: Configured blink(1) serial '12ab34cd' is not connected.
```

One line per hour, however many times the catch-up window retries that slot.
It is a note, not a fault: plug the light back in and the next slot chimes.
Anything that is *not* a missing device still gets its full stack trace, which
is the point — the tracebacks left in the log are the ones worth reading.

### The light does nothing at the top of the hour

Work down this list:

1. `blink-light.bat devices` — is the device enumerated at all?
2. `blink-light.bat chime status` — is `enabled` true, and is `reason` something
   other than `due`?
3. `reason: "quiet-hours"` — expected between 17:00 and 07:00. Set
   `chime.respect_quiet_hours` to `false` to chime overnight anyway.
4. `reason: "already-fired"` — something already chimed this hour. Working as
   intended.
5. `reason: "outside-catch-up-window"` — the machine was asleep or off through the
   slot and is now more than 5 minutes late. Raise
   `chime.catch_up_window_seconds` if you would rather have a late pulse.

### The loop is not running

```bat
blink-light.bat autostart status
```

`installed: true` with `loop.running: false` means the task exists but the loop
died. Check the log for why, then re-run `autostart enable` — it is idempotent and
restarts cleanly.

`autostart enable` reports `started_now` only after it has confirmed the loop
actually wrote its PID file, so a `started_now: false` with `installed: true` means
the runner failed to come up rather than that the task is missing. The log will say
why. The most common cause is a loop that is already running, which is the guard
doing its job:

```
15:01:20 ERROR [chime] Refusing to start: loop already running with PID 23820
```

### Duplicate pulses

Should be impossible; all drivers share the slot-keyed state file. If you do see
doubles, check whether two *repo checkouts* are installed — they would have
different `project_root` paths but write to the same `%LOCALAPPDATA%\BlinkLight`.

### "Two python.exe processes" is normal

The venv's `python.exe` is a 254 KB redirector that launches the real interpreter
as a child. A parent/child pair with identical command lines is **one** logical
runner, not two. Confirm by comparing `ExecutablePath`:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*blink_light*" } |
  Select-Object ProcessId, ParentProcessId, ExecutablePath
```

### Orphaned loops after killing the task by hand

**This is the one real trap.** `schtasks /End` stops the `wscript` launcher it
started, but the `cmd` and `python` descendants survive it. Ending the task by hand
leaves a loop running that Task Scheduler no longer knows about, and the next
`/Run` starts a second one beside it.

Always stop the loop through the CLI, which signals it via a stop file and waits
for it to retire itself:

```bat
blink-light.bat chime stop
blink-light.bat autostart disable
```

`autostart enable` also clears any orphan before starting, so it is safe to run
repeatedly. If you have already made a mess:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*blink_light chime run*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Then `blink-light.bat autostart enable` for a clean single runner.

### Console windows flashing

The scheduled tasks run through `.vbs` launchers specifically to suppress this. A
visible console means a task is pointing at a `.bat` directly — re-run
`autostart enable` / `chime install` to repoint it.

---

## 4. Teardown

```bat
blink-light.bat autostart disable
blink-light.bat chime uninstall
blink-light.bat light off
```

`autostart disable` stops the loop *before* deleting the task, in that order, so
nothing is left running. Verify:

```powershell
schtasks /Query /TN "BlinkLight Autostart"
schtasks /Query /TN "BlinkLight Hourly Chime"
```

Both should report that the task cannot be found. Runtime state under
`%LOCALAPPDATA%\BlinkLight\` is safe to delete by hand.

---

## 5. How the two tasks were registered

Recorded so the setup can be rebuilt or audited without reading the source.

Both are per-user tasks created with `schtasks`, so neither needs administrator
rights, and both invoke a `.vbs` launcher that hides the console window.

**BlinkLight Autostart** — the runner.

```
schtasks /Create /TN "BlinkLight Autostart"
         /TR "wscript.exe \"<repo>\blink-light-autostart.vbs\""
         /SC ONLOGON /F
```

Trigger is **logon, not machine start**, on purpose. The blink(1) is a USB device
in your session; the pre-logon SYSTEM context is the wrong place for it.

The launcher *waits* on the loop rather than firing and forgetting. That keeps the
task action alive for the life of the loop, which makes Task Scheduler's default
"do not start a new instance" policy a real duplicate guard. The loop's own PID
file is the stronger guard underneath it — Task Scheduler cannot protect against a
loop it has lost track of.

**BlinkLight Hourly Chime** — the stateless backstop.

```
schtasks /Create /TN "BlinkLight Hourly Chime"
         /TR "wscript.exe \"<repo>\blink-light-chime.vbs\""
         /SC HOURLY /ST 00:00 /F
```

`/ST 00:00` with `/SC HOURLY` anchors it to `:00` of every hour. Change
`chime.minute` and re-run `chime install` to move it.

### Known limitations

`schtasks` cannot set every property the Task Scheduler GUI can. One worth
knowing:

- **Run task as soon as possible after a missed start** is off. The
  `catch_up_window_seconds` gate covers the common case instead.

### The battery conditions, and why both installers clear them

`schtasks /Create` turns on **Start the task only if the computer is on AC
power** and **Stop if the computer switches to battery power**. Both are
exactly backwards here: undocking is when a chime is most likely to be missed,
and an undocked laptop is the normal case, not the exception.

The failure is silent and easy to misread. The task sits in `Queued` forever,
`LastTaskResult` stays `0`, nothing is written to the log, and
`autostart enable` reports:

```json
{ "installed": true, "started_now": false, "loop": { "running": false } }
```

which reads like a crashed runner rather than a task Windows declined to start.

`schtasks` has no flag for either setting, so `autostart enable` and
`chime install` round-trip the task through its own XML and clear both. Each
reports the result as `runs_on_battery`; if it ever comes back `false`, clear
the two boxes by hand under *Conditions* in `taskschd.msc`. Check the live
state with:

```powershell
(Get-ScheduledTask -TaskName "BlinkLight Autostart").Settings |
  Select-Object DisallowStartIfOnBatteries, StopIfGoingOnBatteries
```
