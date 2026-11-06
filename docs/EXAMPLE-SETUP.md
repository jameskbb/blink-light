# Example setup

A worked example of running blink-light on one desk, built from the shipped
defaults. It also explains why those defaults read like a working day: they
were shaped around a fixed morning standup, an hourly marker, and a desk that
empties in the evening.

Nothing here is required. Every time and colour below is a setting, and the
[README](../README.md#config) shows how to change them.

---

## An example day with the light

The times are the shipped defaults. Treat them as placeholders: the first thing
most people change is the standup time and the end of the day.

| When | What the light does | Why |
| --- | --- | --- |
| **07:00** | Comes back on | Quiet hours end |
| **08:13** | 3 gold flashes | Standup in 2 minutes |
| **08:15** | 6 faster gold flashes | Standup now |
| **:00** every hour | One white breath | A quiet marker that another hour has gone |
| 10 and 5 minutes before a Busy or Tentative meeting | 2 slow cyan blinks, then 4 quick magenta ones | Time to wrap up |
| During a Busy or Tentative meeting | Red | Visible to anyone walking up |
| An AI agent finishes | 2 slow blue breaths | Come back and review |
| An AI agent is blocked | 3 quicker dim-red breaths | It needs an answer |
| A GitHub Actions run you triggered fails | 2 pairs of dim orange-red heartbeat thumps | Review the failed run |
| Someone asks you to review a pull request | A dim violet pulse handed top to bottom, twice | Review it |
| Someone @mentions you on a pull request | A slow dim violet swell with a flicker, twice | They need an answer |
| Someone reviews your pull request | A two-step dim violet climb, twice | Read the feedback |
| **17:00** | Rainbow swirl, then dark | End of the working day |
| **17:15** | The rainbow again | A second chance at it for anyone still at the desk |
| **17:00 to 07:00** | Off and silent | Quiet hours: nothing flashes at an empty desk |

The calendar rows come from the watcher (`watch start`). Everything else runs
from the scheduler loop.

The same thing on paper, with every colour and hex on it, is
[`blink-light-reference-card.pdf`](blink-light-reference-card.pdf). It is built
from the defaults, so it matches this example exactly.

## What's installed

```bat
blink-light.bat autostart enable
blink-light.bat chime install
```

The logon task keeps the scheduler loop running, and the hourly task is the
backstop if the loop ever dies or the laptop sleeps through `:00`. See the
[Runbook](RUNBOOK.md) for how to check both.

## The hardware

In this example the light is plugged into a laptop dock, so undocking unplugs
it. That is treated as normal rather than as an error:

- Both scheduled tasks are allowed to start on battery, so the schedule keeps
  running while undocked.
- Missed flashes write one `device not connected` line to the log per effect
  per hour, not a stack trace per retry.
- Re-docking picks up any slot still inside its catch-up window.
- The watcher keeps running while undocked and repaints the light on re-dock.

## Calendar: Microsoft Graph

This example uses the `graph` provider, which is the right choice when the
calendar lives in Microsoft 365 and is read through the Outlook web app. The
`outlook` provider only reads the classic desktop client's local cache, and
that cache can miss meetings you were invited to but did not organise.

`graph` needs an Entra app registration with the delegated `Calendars.Read`
permission. Its client and tenant IDs go in `.env`, which is gitignored, and
the sign-in token is cached outside the repo. The README's
[Signing in with Microsoft 365](../README.md#signing-in-with-microsoft-365)
section is the exact procedure.

## AI agents: Herdr

Run AI coding agents in [Herdr](https://herdr.dev) with the plugin linked:

```bat
herdr plugin link <repo>\integrations\herdr
```

The done colour is Herdr's own blue at half brightness. Done and blocked differ
in rhythm as well as colour, so they can be told apart from across the room
without looking straight at the light. [Herdr](HERDR.md) covers the setup.

## How the example config differs from the defaults

Only in two keys. Everything else in this example's `blink-light.json` is the
shipped default:

| Key | Example | Default |
| --- | --- | --- |
| `calendar.provider` | `graph` | `outlook` |
| `device.serial` | the light's serial | `null`: detected automatically when one light is connected |

## Making it yours

1. Copy [`blink-light.example.json`](../blink-light.example.json) to
   `blink-light.json`, or run `blink-light.bat config init`.
2. Change what doesn't fit your day. Your standup time, your end of day, or
   `"alarms": []` if you don't want alarms at all.
3. Run `blink-light.bat config validate`, then `blink-light.bat autostart enable`
   so the running loop picks up the change.
