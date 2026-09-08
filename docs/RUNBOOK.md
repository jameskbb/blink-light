# Runbook

Operating blink-light on a Windows machine: setup, verification, troubleshooting,
teardown. Every command here was run on the machine this repo was built on, and
the outputs are real.

Run everything from the repo root. `blink-light.bat` creates `.venv` and installs
`requirements.txt` on first use, so there is no separate install step.

---

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

### The light does nothing at the top of the hour

Work down this list:

1. `blink-light.bat devices` — is the device enumerated at all?
2. `blink-light.bat chime status` — is `enabled` true, and is `reason` something
   other than `due`?
3. `reason: "quiet-hours"` — expected between 22:30 and 07:00. Set
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

`schtasks` cannot set every property the Task Scheduler GUI can. Two worth knowing:

- **Run task as soon as possible after a missed start** is off. The
  `catch_up_window_seconds` gate covers the common case instead.
- **Start the task only if on AC power** is on by default, so a laptop on battery
  may skip the hourly task. Clear it under *Conditions* in `taskschd.msc` if that
  matters. The always-on loop is unaffected.
