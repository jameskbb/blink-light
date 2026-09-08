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

| Agent goes | Light does | Event |
|---|---|---|
| → `idle` or `done` | Cyan/green double blink | `agent_done` |
| → `blocked` | Orange triple blink | `agent_blocked` |
| → `working` | Nothing | — |

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
15:27:53 payload event=pane.agent_status_changed json={"pane_id":"w4:p1",...,"agent_status":"idle",...}
15:27:56 fired: agent_done
15:28:10 skip: status 'working' is not notify-worthy
```

Herdr keeps its own record too: `herdr plugin log list`.

## The payload shape, and why the handler is defensive

Herdr's plugin docs specify the manifest and the injected environment variables
but **not** the JSON payload for this event, and Herdr is closed-source. Rather
than guess one schema, the handler reads the status tolerantly and logs every
payload verbatim.

The field name is nonetheless confirmed, from `herdr agent list`:

```json
{ "pane_id": "w4:p1", "workspace_id": "w4", "agent": "claude",
  "agent_status": "working", "tab_id": "w4:t1", "revision": 3 }
```

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
| No flash, nothing in the log | `herdr plugin list` — is it enabled? `herdr plugin log list` for Herdr's own record. |
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
