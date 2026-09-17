# Flashing the light from your own tool

There is no server, no port and no daemon. A tool makes the light flash by
running a command:

```bat
blink-light.bat notify run agent_done
```

That is the whole interface. Nothing listens on the machine, there is no
process to keep alive, and there are no credentials to get wrong — which is
also why the tool has to be *on the same machine as the light*. A script
running elsewhere needs something of its own to reach this machine first.

## The contract

| | |
|---|---|
| Command | `blink-light.bat notify run <event>` |
| Exit code | `0` fired, non-zero failed (unknown event, no device) |
| Output | One JSON object on stdout |
| Duration | The scene's length — 1 to 3 seconds. The process exits after it |
| Dedupe | **None.** Three calls, three flashes |

The caller names an **event**, never a colour. Appearance lives in
`blink-light.json`, so restyling never means editing the integration:

```json
"notify": {
  "triage_request": { "scene": "triage_request_scene" }
}
```

`blink-light.bat notify list` prints what each event currently resolves to.

Useful flags:

- `--quiet-missing` — exit `0` when the event is not defined, for a caller
  that must not fail because a config has not been updated yet.
- `--respect-quiet-hours` — skip the flash inside `settings.quiet_hours`, for a
  queue that fills overnight. Off by default: a notification usually means
  "this just happened" and wants to fire whatever the hour.

## Calling it from Python

Fire and forget. Never block your own work on the light — the call takes as
long as the scene plays, and a light that is unplugged must not be your
problem:

```python
import subprocess
from pathlib import Path

BLINK_LIGHT = Path(r"C:\path\to\blink-light\blink-light.bat")

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

`CREATE_NO_WINDOW` matters on Windows: without it a console window flickers on
screen every time. The shipped Herdr plugin solves the same problem from
VBScript with `wscript.exe` — see [HERDR.md](HERDR.md#why-wscriptexe).

## Adding an event of your own

1. **Design the scene** in `blink-light.json` under `scenes`. Keep it under
   ~3 seconds and non-looping so it finishes on its own and the watcher
   repaints the underlying state. [EFFECTS.md](EFFECTS.md) has pasteable
   examples.
2. **Name it** under `notify`, as above.
3. **Check it:** `blink-light.bat notify run <event>` — it plays immediately.

Pick a look that is not already taken. One LED carries a dozen meanings, and
the palette is grouped on purpose; the key is in the
[README](../README.md#telling-them-apart). Nothing else on the light is a
rainbow, so `triage_request` uses a short rainbow sweep.

## What not to do

- **Don't hard-code a colour at the call site** (`light flash "#00E5FF"`).
  That pins appearance in the integration, which is exactly what the named
  event exists to avoid.
- **Don't wait on the call.** It runs for as long as the scene plays.
- **Don't retry.** There is no dedupe, so a retry is another flash.
- **Don't flash per item in a batch.** Twenty requests arriving at once is one
  event worth flashing, not twenty.
