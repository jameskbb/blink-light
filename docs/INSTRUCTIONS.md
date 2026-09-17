# Integrating with blink-light

**For an agent or application that wants to make the light do something.** This
page is self-contained: paste the whole thing into another agent's context and
it has everything it needs. Nothing here is specific to one integration.

## The whole interface

```bat
blink-light.bat notify run <event>
```

One command. It plays the flash configured for `<event>` and exits.

**There is no HTTP endpoint, no port, no daemon and no queue.** A tool triggers
the light by running a command, which is why there is nothing to keep alive, no
socket to secure and no auth to get wrong. The cost of that trade: **the caller
must run on the same machine as the light.** A service somewhere else has to
get a signal to this machine first, by whatever means it already has.

## Before you write anything

Run these. They answer, in order: is there a light, what can I already fire,
and is the config valid?

| Command | Tells you |
|---|---|
| `blink-light.bat devices` | Serials of connected blink(1) devices. Empty list means nothing is plugged in |
| `blink-light.bat notify list` | Every event name and what it resolves to. **Use an existing name if one fits** |
| `blink-light.bat status` | Device, calendar, watcher, and what the light should be showing now |
| `blink-light.bat config validate` | Whether `blink-light.json` parses and passes validation |

Every command prints one JSON object on stdout. Parse that, not the log.

## The contract

| | |
|---|---|
| Command | `blink-light.bat notify run <event>` |
| Exit `0` | Fired. stdout: `{"notified": true, "event": "...", "action": {...}}` |
| Exit `1` | Failed. stderr: `error: <reason>`. Unknown event, no device, bad config |
| Exit `2` | Bad usage (argparse) |
| Duration | Blocks for the scene's length — 1 to 3 seconds — then exits |
| Dedupe | **None.** Three calls, three flashes |
| Concurrency | **Serialize your own calls.** See below |

Two flags exist for callers that are not a person at a keyboard:

- `--quiet-missing` — exit `0` when the event is not defined, instead of
  failing. For a tool shipped before the config that names its event.
- `--respect-quiet-hours` — skip the flash inside `settings.quiet_hours`
  (17:00–07:00 by default). Off by default, because a notification usually
  means "this just happened" and should fire whatever the hour. Use it when
  the source is automated and fills overnight, so the light is not flashing at
  an empty desk until morning.

### One flash at a time

The scheduled effects — chime, show, alarms — claim their slot under a lock, so
two of the scheduler's own drivers can never play the same moment on top of
each other. **A notification takes no such lock while it plays.** Two processes
writing pattern lines to one blink(1) at the same instant interleave, and the
light can be left holding a colour with nothing left to fade it down.

So: do not fire two notifications at the same moment from different processes.
In practice that means one call per event, queued on your side if events can
arrive together — which is the same advice as "flash once per batch, not once
per item". If it does happen, `blink-light.bat light off` clears a stuck light.

### Speed

`blink-light.bat` bootstraps the venv and checks whether `requirements.txt`
changed on every call, which costs a few hundred milliseconds. Use it for setup
and one-off calls. On a hot path, call the interpreter directly and skip the
bootstrap:

```
<repo>\.venv\Scripts\python.exe -m blink_light notify run <event>
```

## Adding a signal of your own

1. **Check an existing event does not already fit** — `notify list`. Reusing
   `agent_done` beats inventing a second "finished" flash.
2. **Design a scene** in `blink-light.json` under `scenes`, or reuse one.
   Constraints below. [EFFECTS.md](EFFECTS.md) has pasteable examples.
3. **Name the event** under `notify`:

   ```json
   "notify": {
     "my_event": { "scene": "my_event_scene" }
   }
   ```

4. **Verify by hand** — `blink-light.bat notify run my_event`. It plays
   immediately; no scheduling, no waiting.
5. **Wire the call in.** See below.

An event's action can be any of these forms:

| Form | Example |
|---|---|
| Scene | `{"scene": "my_event_scene"}` |
| Solid colour | `{"color": "#00E5FF", "fade_ms": 200}` |
| Preset | `{"preset": "focus"}` |
| Off | `{"off": true}` |

Exactly one of `scene`, `color`, `preset`, `off` — config validation rejects an
action with two, or none. Anything that blinks, breathes or repeats is a
**scene**: a list of steps the light fades through. There is no `flash` or
`pulse` key in config; those are CLI verbs for one-off experiments
(`light flash`, `light pulse`), and pinning one at a call site is the thing
event names exist to avoid.

Scene constraints, all of which matter:

- **Non-looping** (`"loop": false`). A looping notification never hands the
  light back to whatever it was showing.
- **Under ~3 seconds.** Everything else on the light runs 1–2.6s. A long one
  holds the light through whatever arrives next.
- **32 steps or fewer.** At 32 or under the pattern runs on the device itself;
  past that it is played from the host, a thread and a USB write per step.

## Calling it from code

The pattern in every language is the same: **fire and forget, never fail your
own process because a light is unplugged.**

**Python**

```python
import subprocess
from pathlib import Path

BLINK_LIGHT = Path(r"<repo>\blink-light.bat")

def flash(event: str) -> None:
    """Ask the desk light for one flash. Never raises, never waits."""
    try:
        subprocess.Popen(
            [str(BLINK_LIGHT), "notify", "run", event, "--quiet-missing"],
            cwd=str(BLINK_LIGHT.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,  # Windows: no console pop
        )
    except OSError:
        pass  # No light today. Not a reason to fail the thing that called us.
```

**PowerShell**

```powershell
Start-Process -FilePath "<repo>\blink-light.bat" `
  -ArgumentList "notify","run","<event>","--quiet-missing" `
  -WindowStyle Hidden
```

**Node**

```js
const { spawn } = require("child_process");

function flash(event) {
  try {
    spawn("<repo>\\blink-light.bat", ["notify", "run", event, "--quiet-missing"],
      { detached: true, stdio: "ignore", windowsHide: true }).unref();
  } catch {
    // A light that is not there is not this process's problem.
  }
}
```

**From a hook, git hook, or CI step** — just run the command. Exit code `1`
means it did not fire; decide whether your step cares (usually it should not).

`CREATE_NO_WINDOW` / `windowsHide` / `-WindowStyle Hidden` matter: without
them a console window flickers on screen at every call. The shipped Herdr
plugin solves the same problem from VBScript with `wscript.exe` — see
[HERDR.md](HERDR.md#why-wscriptexe).

## Rules

**Do:**

- Name an **event**, and let `blink-light.json` decide how it looks. This is
  the whole point of the indirection: restyling then never touches your code.
- Flash **once per thing that happened**, not once per item in a batch. Twenty
  items arriving together is one event worth flashing.
- Let the call fail silently in your process.
- Pick a look that is not already taken — one LED carries a dozen meanings and
  the palette is grouped on purpose. The key is in the
  [README](../README.md#telling-them-apart).

**Do not:**

- **Hard-code a colour at the call site** (`light flash "#00E5FF"`). That pins
  appearance in your integration, which is exactly what event names avoid.
- **Block on the call.** It runs for as long as the scene plays.
- **Retry.** There is no dedupe, so a retry is simply another flash.
- **Use `light color` / `light off` for a notification.** Those set a *resting*
  state and fight the watcher, which repaints its own state seconds later.
  `notify run` is non-persistent by design.
- **Write to anything in `%LOCALAPPDATA%\BlinkLight`.** Those files are owned
  by the running processes — the dedupe state especially. Read them if you
  must; never edit them.
- **Put credentials in `blink-light.json`.** It is machine-local and
  gitignored, but secrets belong in `.env`.

## Failure modes

| Situation | What happens |
|---|---|
| Light unplugged | Exit `1`, `error: Configured blink(1) serial '...' is not connected.` Nothing else breaks |
| Event not defined | Exit `1` listing known events — or exit `0` with `{"notified": false, "reason": "unknown-event"}` under `--quiet-missing` |
| Inside quiet hours with the flag | Exit `0`, `{"notified": false, "reason": "quiet-hours"}` |
| Config invalid | Exit `1` naming the offending key. Fix with `config validate` |
| Watcher running | Fine. Your flash plays on top and the watcher repaints its resting colour on its next tick |
| Two calls at the same instant | Their writes to the device interleave and the light can stick on a colour. Serialize on your side; `light off` clears one that stuck |

## A prompt to hand your agent

Copy this, fill in the two placeholders:

> The desk light in this office is driven by **blink-light**, a local CLI at
> `<repo>`. There is no HTTP endpoint — you trigger it by running a command on
> the same machine:
>
> `<repo>\blink-light.bat notify run <event>`
>
> Read `<repo>\docs\INSTRUCTIONS.md` in full before writing any code; it is the
> integration contract. In short: run `notify list` to see which events already
> exist and reuse one if it fits, otherwise add an event plus a scene to
> `blink-light.json` and verify it with `notify run` before wiring anything in.
> Call it fire-and-forget with stdout and stderr discarded and no console
> window, never block on it, never retry it, and never let a missing light fail
> your own process. Name the event — never hard-code a colour at the call site.
> Flash once per event, not once per item in a batch.
