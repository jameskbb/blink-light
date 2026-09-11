# My setup

How I actually run blink-light, as a worked example. It also explains why the
defaults read like one person's working day: most of them are mine.

Nothing here is required. Every time and colour below is a setting, and the
[README](../README.md#config) shows how to change them.

---

## A day with the light

| When | What the light does | Why |
| --- | --- | --- |
| **07:00** | Comes back on | Quiet hours end |
| **08:13** | 3 gold flashes | Standup in 2 minutes |
| **08:15** | 6 faster gold flashes | Standup now |
| **:00** every hour | One white breath | A quiet marker that another hour has gone |
| 10 and 5 minutes before a Busy or Tentative meeting | Yellow, then orange blinks | Time to wrap up what I'm doing |
| During a Busy or Tentative meeting | Red | Visible to anyone walking up |
| An AI agent finishes | 2 slow blue breaths | Come back and review |
| An AI agent is blocked | 3 quicker dim-red breaths | It needs an answer from me |
| A GitHub Actions run I triggered fails | 2 pairs of dim orange-red heartbeat thumps | Review the failed run |
| Someone asks me to review a pull request | A dim violet pulse handed top to bottom, twice | Review it |
| Someone @mentions me on a pull request | A slow dim violet swell with a flicker, twice | They need an answer |
| Someone reviews my pull request | A two-step dim violet climb, twice | Read the feedback |
| **17:00** | Rainbow swirl, then dark | End of the working day |
| **17:00 – 07:00** | Off and silent | I'm out of office, so nothing flashes at an empty desk |

The calendar rows come from the watcher (`watch start`). Everything else runs
from the scheduler loop.

## What's installed

```bat
blink-light.bat autostart enable
blink-light.bat chime install
```

The logon task keeps the scheduler loop running, and the hourly task is the
backstop if the loop ever dies or the laptop sleeps through `:00`. See the
[Runbook](RUNBOOK.md) for how to check both.

## The hardware

The light is plugged into a laptop dock, so undocking unplugs it. That is
treated as normal rather than as an error:

- Both scheduled tasks are allowed to start on battery, so the schedule keeps
  running while undocked.
- Missed flashes write one `device not connected` line to the log per effect
  per hour, not a stack trace per retry.
- Re-docking picks up any slot still inside its catch-up window.
- The watcher keeps running while undocked and repaints the light on re-dock.

## Calendar: Microsoft Graph

My calendar lives in the Outlook web app, so my config uses the `graph`
provider. The `outlook` provider only reads the classic desktop client's local
cache, and that cache missed meetings I was invited to but didn't organise.

I registered my own Entra app with the delegated `Calendars.Read` permission.
Its client and tenant IDs live in `.env`, which is gitignored, and the sign-in
token is cached outside the repo. The README's
[Signing in with Microsoft 365](../README.md#signing-in-with-microsoft-365)
section is the exact procedure I followed.

## AI agents: Herdr

I run AI coding agents in [Herdr](https://herdr.dev) with the plugin linked:

```bat
herdr plugin link <repo>\integrations\herdr
```

The done colour is Herdr's own blue at half brightness. Done and blocked differ
in rhythm as well as colour, so I can tell them apart from across the room
without looking straight at the light. [Herdr](HERDR.md) covers the setup.

## How my config differs from the defaults

Only in two keys. Everything else in my local `blink-light.json` is the shipped
default:

| Key | Mine | Default |
| --- | --- | --- |
| `calendar.provider` | `graph` | `outlook` |
| `device.serial` | this light's serial | `null` — detected automatically when one light is connected |

## Making it yours

1. Copy [`blink-light.example.json`](../blink-light.example.json) to
   `blink-light.json`, or run `blink-light.bat config init`.
2. Change what doesn't fit your day. Your standup time, your end of day, or
   `"alarms": []` if you don't want alarms at all.
3. Run `blink-light.bat config validate`, then `blink-light.bat autostart enable`
   so the running loop picks up the change.
