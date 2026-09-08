# Herdr integration

Flash the blink(1) when a [Herdr](https://herdr.dev) agent finishes or gets
blocked — the same moments Herdr plays its notification sound, but visible from
across the room.

## Install

```bat
herdr plugin link <repo>\integrations\herdr
herdr plugin list
```

```
1 plugin installed:
- blinklight.agent-status (blink(1) agent status) enabled [local:...\integrations\herdr]
```

That is the whole setup. The plugin is linked from the repo, so a `git pull` updates
it in place — no reinstall.

Remove it with `herdr plugin unlink blinklight.agent-status`, or keep it installed
and silence it with `herdr plugin disable blinklight.agent-status`.

## What you get

| Agent finishes | Light does | Colour | Event |
|---|---|---|---|
| → `idle` or `done` | Two slow breaths | Claude orange at 50% (`#6C3C2C`) | `agent_done` |
| → `blocked` | Three quicker breaths | Red at 50% (`#801E00`) | `agent_blocked` |
| → `working` | Nothing | — | — |

Colours are derived, not hand-mixed: `scale_brightness("#D97757", 0.5)`. Scaling all
three channels by the same factor keeps the hue recognisable, where clamping or
blending toward grey would shift it. Full brightness reads as an alarm on a light
that sits in your peripheral vision all day, hence the 50%.

Done and blocked differ in **both hue and rhythm**, so they stay distinguishable
at a glance and to a colour-blind viewer. Every step is a fade rather than a jump,
so they read as breaths instead of blinks.

To change the brightness or colour, edit `CLAUDE_ORANGE` / `NOTIFY_BRIGHTNESS` in
`blink_light/defaults.py` and re-run `config init --force`, or just set the scene
colours directly in `blink-light.json`.

Both are short, non-looping scenes, so the light returns to whatever the watcher
was showing. Restyle them in `blink-light.json` under `notify`:

```json
"notify": {
  "agent_done": { "scene": "agent_done_scene" },
  "agent_blocked": { "scene": "agent_blocked_scene" }
}
```

Any action form works — `{"color": "#00FF00", "fade_ms": 200}`,
`{"preset": "focus"}`, or `{"scene": "..."}` — and `notify list` prints what each
event currently resolves to.

## How it works

```
Herdr server detects working → idle
   └── fires pane.agent_status_changed
         └── wscript.exe handler.vbs          (hides the console)
               └── python handler.py           (decides if it is worth a flash)
                     └── blink-light.bat notify run agent_done
```

Herdr does **not** learn about completion from a Claude Code hook — it
screen-scrapes the pane and runs its own state machine (the rules live in
`%LOCALAPPDATA%\herdr\agent-detection\remote\claude.toml`). This plugin hangs off
the result of that, so it fires for every agent Herdr can detect, not just Claude.

## When it fires, and when it stays quiet

Three filters, each fixing a real way the naive version over-fired. The first
version flashed **8 times in 17 minutes** across 6 sessions, which read as
constant.

**1. Only on a real transition.** Herdr has two finished-ish states, `idle` and
`done`, and one agent turn can emit both — the live log showed `working → idle →
done` for the same pane, which flashed twice for a single finish. The payload
carries no previous status, so the handler keeps its own `pane-state.json` and
fires only when a pane leaves `working`:

```
skip:  w7:p2 -> working (not notify-worthy)
fired: agent_done for w7:p2 (working -> idle)
skip:  w7:p2 idle -> done (not a finish)
```

**2. Not for the pane you are watching.** The focused pane needs no alert — you
can see it. This matches Herdr's own sound, which only plays for background
workspaces. The payload has no focus flag, so the handler asks
`herdr agent list --json`, and only when a flash is otherwise imminent, so the
subprocess cost is per notification rather than per event.

**3. A cooldown.** Six agents finishing together would otherwise machine-gun the
light. Default 8 seconds between flashes.

### Tuning

Drop a `config.json` into the plugin's config directory
(`herdr plugin config-dir blinklight.agent-status`):

```json
{
  "notify_focused": false,
  "cooldown_seconds": 8,
  "min_working_seconds": 0
}
```

| Key | Default | Effect |
|---|---|---|
| `notify_focused` | `false` | `true` also flashes for the pane you are looking at. |
| `cooldown_seconds` | `8` | Minimum gap between flashes. `0` disables. |
| `min_working_seconds` | `0` | Ignore turns shorter than this — raise it if quick turns are noisy. |

**Still too chatty?** Raise `cooldown_seconds`, or set `min_working_seconds` to
something like `30` so only substantial turns announce themselves. To limit it to
one workspace, add a `workspace_id` check in `handler.py` — the payload carries it.

### Why a `notify` verb instead of calling `light flash` directly

The plugin could have called `light flash "#00E5FF" --count 2`. Naming the event
instead keeps *appearance* in `blink-light.json` and *triggering* in the plugin, so
restyling never means editing an integration. It also means the next integration —
a git hook, CI, whatever — has an obvious entry point.

`notify` deliberately has **no dedupe and no slots**, unlike the chime and show.
Those fire once per scheduled moment; a notification fires when something happens,
and three finishes should give three flashes.

### Why `wscript.exe`

Herdr fires the hook on *every* status change, including `working`. Without a
hidden launcher a console window would flash on the desktop several times per
agent turn. The handler is also launched without waiting, so Herdr never blocks
on the light.

### Failure behavior

Every failure path logs and exits 0. A notifier that fails loudly in the middle of
someone's terminal is worse than one that quietly does nothing — a missing device,
a stale config, a malformed payload all end in a log line and a clean exit.

The log is at `%APPDATA%\herdr\plugins\state\blinklight.agent-status\blink-light-herdr.log`
(Herdr sets `HERDR_PLUGIN_STATE_DIR`; it falls back to `%TEMP%`):

```
15:49:44 skip: w7:p2 -> working (not notify-worthy)
15:49:49 fired: agent_done for w7:p2 (working -> idle)
15:49:50 skip: w7:p2 idle -> done (not a finish)
```

Every `skip:` line says which filter suppressed the flash, so "why didn't it
blink" and "why did it blink" are both answerable from the log alone.

Herdr keeps its own record too: `herdr plugin log list`.

## The payload shape, and why the handler is defensive

Herdr's plugin docs specify the manifest and the injected environment variables
but **not** the JSON payload for this event, and Herdr is closed-source. Rather
than guess one schema, the handler reads the status tolerantly and logs every
payload verbatim.

The shape is nonetheless now **confirmed**, captured from a real event:

```json
{"event": "pane_agent_status_changed",
 "data": {"type": "pane_agent_status_changed", "pane_id": "w7:p1",
          "workspace_id": "w7", "agent_status": "idle", "agent": "claude"}}
```

Note what is absent: no previous status and no focus flag. That absence is the
reason for `pane-state.json` and the `herdr agent list` lookup above — the handler
has to reconstruct both itself.

So `agent_status` is checked first, with `status`, `state`, `agent_state`,
`new_status` and `to` as fallbacks. Values are matched case-insensitively, and
both enum encodings Rust might emit are handled (`{"status": {"Done": {}}}` and
`{"status": {"type": "done"}}`). If none of that matches, it logs
`no recognisable status in payload` — and that log line plus the recorded payload
is enough to fix it in one edit.

## Verifying it without waiting for an agent

Fire the notification directly:

```bat
blink-light.bat notify list
blink-light.bat notify run agent_done
blink-light.bat notify run agent_blocked
```

Or drive the handler with a synthetic event, which exercises the whole chain:

```powershell
$env:HERDR_PLUGIN_EVENT = "pane.agent_status_changed"
$env:HERDR_PLUGIN_EVENT_JSON = '{"pane_id":"w4:p1","agent_status":"idle"}'
.\.venv\Scripts\python.exe integrations\herdr\handler.py
```

Expect a cyan/green double blink and a `fired: agent_done` line in the log.

## Troubleshooting

| Symptom | Check |
|---|---|
| **Flashing constantly** | Read the log. `fired:` lines name the pane and transition. Raise `cooldown_seconds` or `min_working_seconds`. |
| Nothing flashes any more | Are you looking at the pane? Focused panes are skipped by default — set `notify_focused: true`. |
| No flash, nothing in the log | `herdr plugin list` — is it enabled? `herdr plugin log list` for Herdr's own record. |
| `not a finish` for every event | The pane never registered `working`, so no transition is seen. Delete `pane-state.json` to reset. |
| `no recognisable status in payload` | The payload shape changed. The logged JSON shows the new key; add it to `STATUS_KEYS` in `handler.py`. |
| `skip: launcher missing` | The plugin was linked from a moved or deleted checkout. Re-link it. |
| `unknown-event` in the log | `blink-light.json` has no `notify` entry by that name. `notify list` shows what exists. |
| Flashes on every keystroke | Herdr's detection rules are matching too eagerly; that is Herdr's state machine, not this plugin. |

## Alternatives considered

- **`[ui.sound]` in Herdr's config** only substitutes mp3 files. There is no
  "run a command on finish" setting, which is why this is a plugin.
- **A Claude Code `Stop` hook** would fire on every session finish regardless of
  which pane you are watching — losing the thing that makes Herdr's own sound
  useful. Still the right choice if you want this independent of Herdr.
- **Editing `~/.claude/hooks/herdr-agent-state.ps1`** is a trap: Herdr overwrites
  it on integration update, and its own header says to add hooks beside it. It only
  handles `SessionStart` anyway and never reports completion.
