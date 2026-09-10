# Repository Agent Guide

Windows-first Python utility that drives a `blink(1)` USB light on a desk: an
hourly chime, a daily rainbow light show, named daily alarms (standup), AI-agent
notifications via the Herdr plugin, Outlook/Microsoft-Graph calendar colours
with pre-meeting warnings, timers, and local rules. See `README.md` for the
full feature/command reference and `docs/ARCHITECTURE.md` /
`docs/RUNBOOK.md` for how scheduling works and how to operate it, and
`docs/EFFECTS.md` for scene-design ideas with pasteable JSON.

## Running it

- Interpreter: `.venv\Scripts\python.exe` (already created; `blink-light.bat`
  bootstraps it and installs `requirements.txt` if it's missing or stale).
- Direct: `.venv\Scripts\python.exe -m blink_light <command>`.
- Via launcher: `blink-light.bat <command>` — same thing, plus the bootstrap.
- Top-level commands (see `blink_light/cli.py` for the authoritative list):
  `devices`, `status`, `light`, `preset`, `override`, `timer`, `watch`,
  `chime`, `alarm`, `notify`, `show`, `autostart`, `calendar`, `config`,
  `startup`. Most have subcommands (`status`, `test`, `run`, `now`, ...) —
  check `cli.py` before assuming one exists.

## Tests

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Verified: 246 tests, all passing, ~5s. No hardware required — device I/O is
mocked in tests.

## Shipping

Commit straight to `main` and push. This is a single-maintainer repo; a PR
here only parks finished work behind a gate nobody reads. The test suite is
the gate — run it before pushing.

The repo is kept ready to be public. In commit messages keep
`Co-Authored-By`, but leave out `Claude-Session:` lines — they link to private
sessions. Author commits with the GitHub no-reply address (set in this repo's
local git config), never a work email.

## Conventions

- Comments explain *why*, not *what* (see `blink_light/defaults.py`,
  `blink_light/config.py`, `blink_light/secret_scan.py` for the style). Don't
  add a comment that restates the line below it.
- Test names are full sentences describing behaviour, not
  `test_<method>_<case>` shorthand — e.g. `test_a_bom_does_not_swallow_the_first_key`,
  `test_the_hook_is_wired_to_the_shared_scan`. Match that style for new tests.
- Config lives in `blink-light.json`, which is machine-local and gitignored
  (`config init` writes it; the secret scan refuses to let it be tracked).
  `blink-light.example.json` is the committed template and must stay equal to
  `default_config()` — `tests/test_config.py` checks that, so regenerate it
  after changing a default. Every default lives in
  `blink_light/defaults.py` (`default_config()`), and `config.py` deep-merges
  the user file over it. `alarms`, `notify`, and `rules` are replaced
  wholesale by a user override, not merged by key — that's deliberate, so an
  entry can be deleted by omitting it.
- Every scheduled effect (chime, show, alarm) must appear in the "What the
  light does" table in `README.md`, and `tests/test_readme_schedule.py` checks
  the table's times/colours/durations against the real config defaults. Adding
  or changing a schedule without updating that table fails the suite.

## Secrets

- Credentials go in `.env` (gitignored), never in `blink-light.json` or its
  committed template. `.env.template` documents the recognised variable names
  and is the one `.env.*` file that's tracked.
- Both `.githooks/pre-commit` and `tests/test_secrets_guardrail.py` run the
  same scan, defined once in `blink_light/secret_scan.py`. Install the hook
  with `git config core.hooksPath .githooks`. Run the scan by hand with
  `.venv\Scripts\python.exe -m blink_light.secret_scan --tracked`.
- The Microsoft refresh token lives outside the repo, at
  `%LOCALAPPDATA%\BlinkLight\graph-token-cache.json`.

## Windows realities

- Use the PowerShell tool for git in this repo — the Bash tool hangs on git
  here.
- `Set-Content -Encoding utf8` (and Notepad) write a UTF-8 BOM.
  `blink_light/env_file.py` strips it on `.env` reads; don't assume a plain
  read of a PowerShell-written file skips the BOM.
- Commands like `light color`, `light flash`, `chime test`, `show test`,
  `alarm test`, and `notify run` talk to the real USB device and flash the
  light on the user's actual desk. Don't fire them repeatedly just to check
  output shape — run them once, or read `status`/`*  status` instead.

## Never

- Never commit `.env`, `*.local.json`, `*token-cache*.json`, or `*.token` —
  `.gitignore` and the secret scan both block this; don't work around either.
- Never end the chime loop or watcher via `schtasks /End` or Task Manager —
  it orphans the process. Use `chime stop` / `autostart disable`, which signal
  it cleanly (see `docs/RUNBOOK.md`).
- Never hand-edit `%LOCALAPPDATA%\BlinkLight\*-state.json` — these are the
  once-per-slot dedupe files; editing them can cause a double-fire or a
  permanently skipped slot.
- Never change `README.md`'s schedule table without running the test suite —
  `test_readme_schedule.py` is the thing that catches drift.
