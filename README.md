# blink-light

Turn a [`blink(1)`](https://blink1.thingm.com/) USB light into an ambient status display for your desk. It warns you before meetings and glows red while you're in one, chimes on the hour, plays a short rainbow at the end of the day, fires named daily alarms, and lights up when an AI coding agent finishes or gets stuck.

Windows-first, written in Python, no admin rights needed. One batch file sets everything up.

> **The defaults are one person's working day** — standup alerts at 08:13, dark from 17:00. Change any of them in your own config (see [Config](#config)). [My setup](docs/MY-SETUP.md) shows how the author runs it and why.

---

## Requirements

- Windows 10 or 11
- A blink(1) USB light. Effects that use both LEDs need an mk2 or newer.
- Python 3.12 or newer (tested on 3.12 and 3.13), on `PATH` or through the `py` launcher
- Optional, for calendar colours: a Microsoft 365 account (Graph), or the classic Outlook desktop app (COM)

## Install

```bat
git clone https://github.com/jameskbb/blink-light.git
cd blink-light
blink-light.bat devices
```

Everything runs through `blink-light.bat` from the repo root. The first run creates `.venv`, installs `requirements.txt`, then runs the command — there is no separate install step.

## Quick Start

```bat
blink-light.bat devices
blink-light.bat light color "#00C853"
blink-light.bat config init
blink-light.bat status
```

`devices` confirms the light is seen. `light color` proves it works. `config init` writes `blink-light.json`, auto-filling `device.serial` when exactly one device is connected. That file is yours: it is gitignored, so your serial and schedule stay on your machine. Without one, the built-in defaults apply. [`blink-light.example.json`](blink-light.example.json) shows every key at its default. `status` shows what the watcher would do right now.

Running `blink-light.bat` with no arguments prints a welcome — or, if `calendar.enabled` and `calendar.auto_watch_on_launch` are both `true`, starts the background watcher immediately.

For full first-time setup — installing the scheduled tasks, verifying the chime and show actually fire, troubleshooting — see the [Runbook](docs/RUNBOOK.md).

## Docs

| Page | Read it when you want to |
| --- | --- |
| [Runbook](docs/RUNBOOK.md) | set up the scheduled tasks, check they fire, troubleshoot, or remove everything |
| [Architecture](docs/ARCHITECTURE.md) | understand how the scheduling works, or add a feature |
| [Effects](docs/EFFECTS.md) | design your own flashes — with pasteable scenes |
| [Herdr](docs/HERDR.md) | make the light react when an AI coding agent finishes or gets blocked |
| [My setup](docs/MY-SETUP.md) | see how the author runs it day to day |

## What the light does

Everything a fresh install does, in one place. Values are the built-in defaults from
[`blink_light/defaults.py`](blink_light/defaults.py), mirrored in
[`blink-light.example.json`](blink-light.example.json); your own
`blink-light.json` can override any of them.

### On a schedule

| When | What | Colour | Length | Preview it |
| --- | --- | --- | --- | --- |
| **08:13** daily | Standup in 2 minutes — 3 flashes | 🟡 `#FFD700` gold | 0.96s | `alarm test standup_warning` |
| **08:15** daily | Standup now — 6 faster flashes | 🟡 `#FFD700` gold | 1.44s | `alarm test standup_now` |
| **:00** every hour | One white breath | ⚪ `#FFFFFF` | 0.8s | `chime test` |
| **17:00** daily | Rainbow swirl across both LEDs | 🌈 28 hues | 9.92s | `show test` |

### When something happens

| Trigger | What | Colour | Length | Preview it |
| --- | --- | --- | --- | --- |
| AI agent finishes ([Herdr](docs/HERDR.md)) | 2 slow breaths | 🔵 `#28627C` — Herdr blue at 50% | 1.80s | `notify run agent_done` |
| AI agent blocked | 3 quicker breaths | 🔴 `#801E00` — dim red | 1.26s | `notify run agent_blocked` |
| GitHub Actions run you triggered fails (opt-in) | 2 pairs of heartbeat thumps | 🟠 `#803C00` — dim orange-red | 2.40s | `notify run ci_failed` |
| A pull request review is requested from you (opt-in) | A pulse handed from the top LED to the bottom, twice | 🟣 `#442C72` — dim violet | 2.24s | `notify run pr_review_requested` |
| You're @mentioned on a pull request (opt-in) | A slow swell with a flicker at its peak, twice | 🟣 `#442C72` — dim violet | 2.56s | `notify run pr_mentioned` |
| Someone reviews your pull request (opt-in) | A two-step climb, twice | 🟣 `#1B112E` — dimmer violet | 1.98s | `notify run pr_review_received` |

### The resting colour (watcher only, opt-in)

Only while `watch start` is running. This is the *background* state the light
returns to; everything above plays on top of it.

| Situation | Colour |
| --- | --- |
| Nothing scheduled | 🟢 `#00C853` green |
| In a Busy or Tentative meeting | 🔴 `#D50000` red |
| Meeting in 10 minutes | 🟡 `#FDD835` — 2 slow blinks |
| Meeting in 5 minutes | 🟠 `#FB8C00` — 4 quick blinks |
| Idle 10+ minutes | 🔵 `#2962FF` blue |

Meetings marked **Free**, **Out of Office** or **Working Elsewhere** are ignored
entirely — no colour, no warning. Only `alert_statuses` (Tentative and Busy by
default) moves the light.

### Quiet hours

**17:00 – 07:00** by default the light is off, and the chime and alarms stay
silent. Each effect can opt out with `respect_quiet_hours: false`, and the
window itself is `settings.quiet_hours`.

The daily show is the one that opts out. It fires at exactly 17:00 and the
window is inclusive of its start, so respecting quiet hours would retire the
rainbow permanently; instead it still plays, as the last thing the light does
before going dark.

Because 08:13 and 08:15 sit outside the window, the standup alerts are
unaffected.

### Check what is live right now

```bat
blink-light.bat alarm status
blink-light.bat chime status
blink-light.bat show status
blink-light.bat notify list
```

Each prints `next_slot` and when it last fired, plus a `reason` when something is
not going to fire. Nothing here fires twice for the same moment — see
[Architecture](docs/ARCHITECTURE.md) for why.

## Hourly Chime

One white pulse every hour, on top of whatever the watcher is showing.

```bat
blink-light.bat chime test
blink-light.bat chime install
blink-light.bat chime status
blink-light.bat chime uninstall
```

`chime install` registers a Windows scheduled task named **BlinkLight Hourly
Chime** that fires at `:00` regardless of whether the watcher or the
`autostart` loop is running.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch. |
| `color` / `secondary_color` | `#FFFFFF` / `#000000` | Pulse colour, and the colour it fades back to. |
| `on_ms` / `off_ms` | `400` | Fade-up / fade-down duration. |
| `count` | `1` | Pulses per chime. |
| `minute` | `0` | Minute past the hour. Re-run `chime install` after changing it. |
| `catch_up_window_seconds` | `300` | How late a missed slot may still fire (covers a laptop waking up). |
| `respect_quiet_hours` | `true` | Skip inside `settings.quiet_hours`. |

`chime now` fires only if this hour's pulse is still due; `--force` ignores
quiet hours, the catch-up window, and the already-fired guard. `chime run`
runs the loop in the foreground; `chime stop` asks it to exit. See
[Architecture](docs/ARCHITECTURE.md) for how the loop, the hourly task and the
watcher all share one dedupe file without double-pulsing.

## Daily Light Show

A rainbow swirl at **17:00**, capped at 10 seconds.

```bat
blink-light.bat show test
blink-light.bat show now
blink-light.bat show status
```

`test` plays it now without consuming today's slot. It runs on the same
scheduler loop as the chime, so nothing extra to install. It is a hue
rotation, not a strobe: both LEDs stay half a turn apart in colour, and every
step is a fade, brightness swelling from 45% to full and back before a clean
fade to black — 29 steps, 9.92s, uploaded to the device so playback stays
smooth even when the machine is busy.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Master switch. |
| `at` | `"17:00"` | Time of day, `HH:MM`. |
| `scene` | `"rainbow_swirl"` | Any non-looping scene in `scenes`. |
| `max_seconds` | `10` | Hard cap, enforced at `config validate` time — not by truncating playback. A longer scene fails validation with its measured duration. |
| `catch_up_window_seconds` | `300` | How late a missed show may still play. |
| `respect_quiet_hours` | `false` | Off by default here: 17:00 is the first minute of quiet hours, so respecting them would mean the show never plays. |

`show now --force` ignores quiet hours, the catch-up window and today's guard.
Want a different show? [Effects](docs/EFFECTS.md) has a sunrise ramp that fits the cap.

## Daily Alarms

Named flashes at wall-clock times. Two ship by default (see the schedule
table above): full-brightness gold on purpose, since unlike the Herdr
notification these are meant to catch your eye across the room.

```bat
blink-light.bat alarm status
blink-light.bat alarm test <name>
blink-light.bat alarm run <name>
```

`test` plays one now without consuming today's slot; `run` consumes it.
`alarm now` fires whatever is currently due — that is what the scheduler loop
calls. Each alarm dedupes on its own key, so the 08:13 and 08:15 pair never
swallow each other.

Alarms live in a top-level `alarms` list, so adding a third is a config entry,
not a code change:

```json
"alarms": [
  {
    "name": "standup_warning",
    "at": "08:13",
    "action": { "scene": "standup_warning_scene" },
    "enabled": true,
    "catch_up_window_seconds": 120,
    "respect_quiet_hours": true
  }
]
```

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique. Keys the once-per-day dedupe. |
| `at` | yes | `HH:MM`, 24-hour. |
| `action` | yes | Any action: `scene`, `preset`, `color`, or `off`. |
| `days` | no | e.g. `["mon","tue","wed","thu","fri"]`. Omit for every day. |
| `enabled` | no | Default `true`. |
| `catch_up_window_seconds` | no | Default `300`. |
| `respect_quiet_hours` | no | Default `true`. |

Setting `alarms` in your config **replaces** the defaults rather than merging,
so `"alarms": []` disables both shipped ones. Add `"days"` to either default
to skip weekends.

## Notifications

One-shot named flashes for external tools. No slots, no dedupe — it fires
whenever you call it.

```bat
blink-light.bat notify list
blink-light.bat notify run agent_done
```

Events live under `notify` in `blink-light.json`, so an integration names an
event and the config decides how it looks:

```json
"notify": {
  "agent_done": { "scene": "agent_done_scene" },
  "agent_blocked": { "scene": "agent_blocked_scene" }
}
```

Add `--quiet-missing` for callers that must not fail when an event is
undefined.

**Shipped integration:** [Herdr](docs/HERDR.md) — flashes when an AI agent
finishes or gets blocked.

```bat
herdr plugin link <repo>\integrations\herdr
```

## GitHub Actions Failures

The scheduler can flash `ci_failed` for failed workflow runs you triggered, across
repositories. This integration is off by default. It uses your existing GitHub CLI
login and adds no Python dependency.

1. Run `gh auth status --hostname github.com` to check your login.
2. Add `"github": { "enabled": true }` to your local `blink-light.json`.
3. Run `blink-light.bat config validate`.
4. Run `blink-light.bat github check --dry-run` to preview the feed without a flash or state change.
5. Run `blink-light.bat autostart enable` to restart the scheduler with the new config.

The first successful poll records existing notifications as a baseline and flashes
nothing. Later polls detect new failures by notification ID and update timestamp.
A failed rerun can flash again. Quiet hours consume failures silently, so they
do not replay in the morning. An absent device also consumes the event.

| Config key | Default | Meaning |
| --- | --- | --- |
| `github.enabled` | `false` | Enable polling in the scheduler. |
| `github.poll_seconds` | `60` | Minimum interval in seconds. GitHub can require a longer interval. |
| `github.respect_quiet_hours` | `true` | Consume events silently during quiet hours. |
| `github.actions_failed_event` | `"ci_failed"` | Event name in `notify`. |

`github status` shows the login source, last poll, watermark, and last flash.
`github check` polls immediately when enabled. `--dry-run` shows `matches` and
`would_flash` without saving state or touching the light. `github test` plays the
configured event once, even when polling is disabled.

The feed contains the 50 most recently updated notifications. More than 50 updates
between polls can hide a failure. GitHub must create a notification for the run.
The integration never marks notifications read. The token stays in memory and
never enters the config or state file.

Conditional requests use `Last-Modified`. Unchanged feeds return HTTP 304 without
consuming the API rate limit. See [GitHub notifications](https://docs.github.com/en/rest/activity/notifications).

## Start It At Boot

```bat
blink-light.bat autostart enable
blink-light.bat autostart status
blink-light.bat autostart disable
blink-light.bat autostart enable --no-start
```

Registers a scheduled task named **BlinkLight Autostart** that starts the
chime loop at every logon, and starts it immediately so you do not have to
reboot to begin. Trigger is logon rather than machine start, because the
blink(1) is a USB device in your interactive session, not something the
pre-logon SYSTEM context can see. `disable` stops the running loop and removes
the task; `--no-start` registers the task but waits for the next logon.

Keep both `autostart enable` (the live runner) and `chime install` (the
stateless backstop) — they cannot double-pulse, and together they cover a
dead loop or a machine asleep at the top of the hour. Both tasks are allowed
to start on battery, so an undocked laptop still keeps the schedule. See
[Architecture](docs/ARCHITECTURE.md#the-three-moving-parts) for why.

`startup enable` is the older Startup-folder mechanism; it launches the full
calendar watcher instead of just the chime loop, so use it if you want
calendar colours without a login task.

## Calendar

The default config is Outlook-first. Which provider it reads from decides how
the resting-colour table above gets its data.

### Picking a provider

| Provider | Reads | Setup |
| --- | --- | --- |
| `graph` | Your Microsoft 365 mailbox on the server — the same data the Outlook web app shows | One-time Entra app registration, then `calendar login` |
| `outlook` | The classic Outlook desktop client's local cache, over COM | None |

**Use `graph` if you live in the Outlook web app.** The COM provider only sees
what the classic desktop profile has cached locally, which can silently omit
meetings — in particular ones you were invited to but did not organise. Graph
reads the mailbox itself, so what the light sees is what the web app sees.

### Signing in with Microsoft 365

One-time, about five minutes. You register your own app, so nothing here
depends on anyone else's tenant:

1. Open [Entra app registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) → **New registration**. Name it `blink-light`, and under **Supported account types** pick *Accounts in this organizational directory only*.
2. Under **Redirect URI**, choose **Public client/native** and enter `http://localhost`.
3. Register, then copy the **Application (client) ID** and **Directory (tenant) ID** into `.env` (see Secrets below) — not into `blink-light.json`, which holds behaviour and gets shared as a template.
4. **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated** → `Calendars.Read` → Add. If your tenant requires it, click **Grant admin consent** (or ask an admin to).
5. Fill in `.env`, set `"provider": "graph"` in `blink-light.json`, then run:

```bat
copy .env.template .env
rem edit .env, then:
blink-light.bat calendar login
blink-light.bat calendar upcoming --hours 24
```

`calendar login` opens a browser, falling back to a device code if it cannot,
and caches the refresh token at
`%LOCALAPPDATA%\BlinkLight\graph-token-cache.json` — outside the repo, so it
cannot be committed by accident. `calendar logout` removes it. The only scope
requested is `Calendars.Read`; the light never writes to your calendar.

### Which meetings count

`calendar.alert_statuses` is a list of Outlook `OlBusyStatus` values, and only
those events do anything at all:

| Outlook status | Value | Default |
| --- | --- | --- |
| Free | `0` | ignored |
| Tentative | `1` | ✅ colour + warnings |
| Busy | `2` | ✅ colour + warnings |
| Out of Office | `3` | ignored |
| Working Elsewhere | `4` | ignored |

An ignored event cannot colour the light or fire a warning, and it cannot hide
a real meeting either: a Free block overlapping a Busy one is skipped, and the
Busy meeting still gets its warnings. To make Out of Office count too, set
`"alert_statuses": [1, 2, 3]`.

Notes:

- All-day events are ignored by default; cancelled meetings are dropped (Graph provider).
- Graph reports availability as words (`busy`, `oof`, …); they are mapped onto the same Outlook status numbers above, so `alert_statuses` means one thing regardless of provider.
- Each warning is one-shot per meeting, and the warnings own windows rather than deadlines: starting the watcher three minutes before a meeting gets you the five-minute warning only, never a ten-minute warning that is already false.
- `blink-light.bat status` reports `event_count` alongside `alertable_event_count`, which is usually the answer to "why is my light still green?"

## Secrets

Credentials never live in the repo. Three layers enforce that:

| Layer | What it does |
| --- | --- |
| `.env` (gitignored) | Where ids and credentials actually live. `.env.template` documents the keys and is the only `.env.*` file that is committed. |
| `.gitignore` | Ignores `.env`, `blink-light.json`, `*.local.json`, `*token-cache*.json`, `*.token`. |
| `.githooks/pre-commit` | Blocks a commit that stages one of those files — even with `git add -f` — or that adds a credential-shaped value to any file. |

Install the hook once per clone:

```bat
git config core.hooksPath .githooks
```

The rules live in `blink_light/secret_scan.py`, and the test suite runs the
same scan over every tracked file, so a leak fails the build as well as the
commit. Run it by hand any time:

```bat
.venv\Scripts\python -m blink_light.secret_scan --tracked
```

Recognised variables (all optional except the first two, and only for the
Graph provider):

| Variable | Sets |
| --- | --- |
| `BLINK_LIGHT_GRAPH_CLIENT_ID` | `calendar.graph.client_id` |
| `BLINK_LIGHT_GRAPH_TENANT_ID` | `calendar.graph.tenant_id` |
| `BLINK_LIGHT_CALENDAR_PROVIDER` | `calendar.provider` |
| `BLINK_LIGHT_DEVICE_SERIAL` | `device.serial` |

Real environment variables win over `.env`, so a scheduled task can override
the file without editing it.

## Command Reference

```bat
blink-light.bat light color "#00C853"
blink-light.bat light flash "#D50000" --count 4
blink-light.bat light pulse "#FFB300" --count 0
blink-light.bat light scene timer_done_scene

blink-light.bat preset list
blink-light.bat preset run focus

blink-light.bat override set --preset busy --expires-in 30m --reason "heads down"
blink-light.bat override clear
blink-light.bat override status

blink-light.bat timer start 25m
blink-light.bat timer start pomodoro
blink-light.bat timer pause
blink-light.bat timer resume
blink-light.bat timer stop

blink-light.bat watch once
blink-light.bat watch run
blink-light.bat watch start
blink-light.bat watch stop

blink-light.bat calendar login
blink-light.bat calendar status
blink-light.bat calendar upcoming --hours 24
blink-light.bat calendar logout

blink-light.bat github status
blink-light.bat github check --dry-run
blink-light.bat github check
blink-light.bat github test

blink-light.bat config init
blink-light.bat config validate
blink-light.bat config show

blink-light.bat autostart enable
blink-light.bat autostart status

blink-light.bat startup enable
blink-light.bat startup status
```

## Config

Your config file is `blink-light.json` in the repo root. It is machine-local:
`.gitignore` excludes it and the secret scan refuses to let it be committed, so
a device serial or a personal schedule cannot end up in a push. Start from
[`blink-light.example.json`](blink-light.example.json) — exactly what
`config init` writes with no device attached — or leave the file out and run on
the built-in defaults.

You only need the keys you want to change. The file is merged over the
defaults, so this is a complete config that moves the standup alerts and keeps
the light on until 18:00:

```json
{
  "alarms": [
    { "name": "standup_warning", "at": "09:28", "action": { "scene": "standup_warning_scene" } },
    { "name": "standup_now", "at": "09:30", "action": { "scene": "standup_now_scene" } }
  ],
  "settings": { "quiet_hours": { "start": "18:00" } }
}
```

After editing, run `blink-light.bat config validate`, then
`blink-light.bat autostart enable` so the running loop picks it up.

Its top-level keys:
`device`, `calendar`, `chime`, `show`, `alarms`, `notify`, `presets`,
`scenes`, `routines`, `rules`, `settings`.

The watcher's precedence, highest first:

1. manual override
2. calendar (when `calendar.enabled`)
3. active timer/routine phase
4. first matching local rule
5. quiet hours
6. `settings.default_action`

Supported local rule conditions: `idle_seconds_gte`, `process_running_any`,
`file_exists`, `file_contains`, `time_between`, `battery_below_percent`,
`charging`, `timer_active`, `routine_phase`.

The chime, show, alarms and notify sit outside that chain entirely: each is a
one-shot played on top of whatever the light is already showing, and the
watcher repaints the underlying state on its next tick.

## Repo Layout

| Path | Purpose |
| --- | --- |
| `blink-light.bat` | Launcher. Bootstraps `.venv`, installs `requirements.txt`, runs the CLI. |
| `blink-light-chime.bat` | Runner invoked by the hourly task; calls `chime now`. |
| `blink-light-chime.vbs` | Hides the console window for the hourly task. |
| `blink-light-autostart.bat` | Long-running runner started at logon; calls `chime run`. |
| `blink-light-autostart.vbs` | Hides the console and waits on the loop (single-instance guard). |
| `blink-light.example.json` | Config template: every key at its default. Committed. |
| `blink-light.json` | Your config. Gitignored; created by `config init` or copied from the template. |
| `blink_light/chime.py` | Hourly chime: slots, dedupe, scheduler loop, scheduled-task management. |
| `blink_light/show.py` | Daily light show, same slot/dedupe shape as the chime. |
| `blink_light/alarms.py` | Named daily alarms; shares slot logic with the show. |
| `blink_light/notify.py` | Named one-shot notifications for external tools. |
| `integrations/herdr/` | Herdr plugin: manifest plus its event handler. |
| `blink_light/watcher.py` | Background loop, action precedence. |
| `blink_light/cli.py` | Argument parsing and command dispatch. |
| `docs/` | Runbook, architecture, effects, the Herdr integration, and the author's setup. |
| `tests/` | `unittest` suite, no hardware required. |

## Working on it

```bat
git config core.hooksPath .githooks
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite needs no hardware — device I/O is mocked — and runs in about five
seconds. It also checks the docs against the code: the schedule tables above
must match the defaults, and `blink-light.example.json` must equal
`default_config()`. Change a default and the suite tells you which docs to
update.

## License

[MIT](LICENSE).
