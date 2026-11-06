# blink-light agent rules

`CLAUDE.md` and `AGENTS.md` are the same document under two names, so Claude
Code and Codex follow identical rules. Change both together; `scripts/check.sh`
fails if they differ.

## What this is

A Windows-first Python utility that drives a `blink(1)` USB light on a desk: an
hourly chime, a daily rainbow light show, named daily alarms (standup), AI-agent
notifications via the Herdr plugin, Outlook/Microsoft Graph calendar colours
with pre-meeting warnings, GitHub notifications, timers, and local rules.

`README.md` is the feature and command reference. `docs/ARCHITECTURE.md` and
`docs/RUNBOOK.md` explain how scheduling works and how to operate it;
`docs/EFFECTS.md` has scene-design ideas with pasteable JSON; `docs/INSTRUCTIONS.md`
is handed to other agents verbatim. `docs/MY-SETUP.md` is the author's own
schedule and setup: keep it in step with the defaults and free of anything
identifying (no serials, IDs, meeting names or paths).

## Install and run

- Windows: `blink-light.bat <command>` from the repo root. The first run creates
  `.venv` and installs `requirements.txt`; later runs reinstall when it changes.
- Direct: `.venv\Scripts\python.exe -m blink_light <command>`.
- Top-level commands (`blink_light/cli.py` is the authoritative list):
  `devices`, `status`, `light`, `preset`, `override`, `timer`, `watch`,
  `chime`, `alarm`, `notify`, `show`, `autostart`, `calendar`, `github`, `config`,
  `startup`. Most have subcommands (`status`, `test`, `run`, `now`, ...); check
  `cli.py` before assuming one exists.

## Test

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

One module, or one test:

```bat
.venv\Scripts\python.exe -m unittest tests.test_notify
.venv\Scripts\python.exe -m unittest tests.test_cli.CliTests.test_notify_run_fires_the_named_event
```

The full gate is `scripts/check.sh` (bash; Linux, macOS, WSL or Git Bash): no em
dashes in tracked files, `CLAUDE.md` equal to `AGENTS.md`, then the test suite.
CI runs it on Ubuntu. It needs `requirements.txt` installed in the Python it
uses (set `PYTHON` to pick one); without `blink1` and `msal` two tests fail.

Tests need no hardware or network: device I/O and GitHub HTTP calls are mocked.
There is no linter or formatter; the test suite is the gate.

## How the pieces fit

`docs/ARCHITECTURE.md` is the full account. The orientation worth having before
you read any module:

**Three ways the light is driven, and they are independent.**

| | Trigger | Dedupe | Lives in |
|---|---|---|---|
| Scheduler loop (`chime run`) | a time arrives | once per slot | `chime.py` |
| Watcher (`watch start`) | state changed | once per distinct action | `watcher.py` |
| Notify (`notify run <event>`) | someone asked | none: every call flashes | `notify.py` |

The watcher paints *ongoing state* (are you in a meeting?); the scheduler fires
*moments* (it is 5pm); a notification is a one-shot process with no daemon
behind it. The scheduler loop also supervises the watcher, restarting one that
has gone missing, so two long-lived processes exist, not one.

Because two drivers can wake on the same second, a scheduled effect **claims**
its slot under an OS lock before touching the device (`slot_lock.py`); the
loser stands down. Never bypass that to "just fire it".

**The device layer has two playback paths** (`device.py`). A short,
non-looping scene is written into the blink(1)'s pattern RAM and played by the
firmware in one `play()` call; anything looping or longer than 31 steps is
played step by step from a host thread instead. `persistent=True` leaves the
light in that state and dedupes against the last scene; `persistent=False`
plays once and lets the watcher repaint. Which path a scene takes changes its
timing and its failure modes, so check `can_run_scene_on_device` before
blaming the firmware.

Runtime state (pid files, locks, the once-per-slot dedupe files, the log) all
lives in `%LOCALAPPDATA%\BlinkLight`, never in the repo.

## Documentation generated from the defaults

Five committed artefacts are derived from or checked against
`blink_light/defaults.py`, each with a test that fails on drift. After changing
a default, expect to update:

- `blink-light.example.json`: must equal `default_config()` (`test_config.py`)
- `README.md`'s "What the light does" table (`test_readme_schedule.py`)
- `docs/EFFECTS.md`: every scene block is run through the real validator
  (`test_effects_doc.py`)
- `docs/INSTRUCTIONS.md`: every command and exit code it names is checked
  against the CLI (`test_instructions_doc.py`)
- `docs/blink-light-reference-card.pdf`: regenerate with
  `.venv\Scripts\python.exe tools/build_reference_card.py` (`test_reference_card.py`)

## Branches and landing

Single branch `main`, no prod branch, nothing deploys on push. Commit straight to
`main` and push after the test suite passes. The repository is public.

- Author commits with the GitHub no-reply address set in this repo's local git
  config, never a work email.
- No AI trailers in commit messages: no `Co-Authored-By` for an assistant, no
  "Generated with" line, no `Claude-Session:` line.

## Conventions

- Comments explain *why*, not *what* (see `blink_light/defaults.py`,
  `blink_light/config.py`, `blink_light/secret_scan.py` for the style).
- Test names are full sentences describing behaviour, not
  `test_<method>_<case>` shorthand, for example
  `test_a_bom_does_not_swallow_the_first_key`. Match that style.
- Config lives in `blink-light.json`, which is machine-local and gitignored
  (`config init` writes it; the secret scan refuses to let it be tracked).
  Every default lives in `blink_light/defaults.py` (`default_config()`), and
  `config.py` deep-merges the user file over it. `alarms` and `rules` replace
  their defaults wholesale, so omission removes an entry; `notify`, `presets`
  and `scenes` merge by key.
- Every scheduled effect (chime, show, alarm) must appear in the "What the
  light does" table in `README.md`; `tests/test_readme_schedule.py` checks it.
- The README's images live in `docs/images/`. When a change alters what an
  image shows, update the image in the same change.

## Secrets

- Credentials go in `.env` (gitignored), never in `blink-light.json` or its
  committed template. `.env.template` documents the recognised variable names
  and is the one `.env.*` file that is tracked.
- `.githooks/pre-commit` and `tests/test_secrets_guardrail.py` run the same
  scan, defined once in `blink_light/secret_scan.py`. Install the hook with
  `git config core.hooksPath .githooks`; run it by hand with
  `python -m blink_light.secret_scan --tracked`.
- The Microsoft refresh token lives outside the repo, at
  `%LOCALAPPDATA%\BlinkLight\graph-token-cache.json`.

## Windows realities

- On Windows, run git from PowerShell; agent Bash tools there have hung on git.
- `Set-Content -Encoding utf8` (and Notepad) write a UTF-8 BOM.
  `blink_light/env_file.py` strips it on `.env` reads; do not assume a plain
  read of a PowerShell-written file skips the BOM.
- `light color`, `light flash`, `chime test`, `show test`, `alarm test` and
  `notify run` drive the real USB device on the user's desk. Run them once, or
  read `status` instead; do not fire them repeatedly to check output shape.

## Never

- Never write an em dash (U+2014) anywhere: docs, comments, UI text, commit
  messages. Reword with a colon, semicolon, comma, parentheses or a new
  sentence; do not swap in a hyphen or en dash. `scripts/check.sh` enforces it.
- Never commit `.env`, `*.local.json`, `*token-cache*.json` or `*.token`;
  `.gitignore` and the secret scan both block this, so do not work around either.
- Never end the chime loop or watcher via `schtasks /End` or Task Manager: it
  orphans the process. Use `chime stop` or `autostart disable` (see
  `docs/RUNBOOK.md`).
- Never hand-edit `%LOCALAPPDATA%\BlinkLight\*-state.json`: these are the
  once-per-slot dedupe files, and editing them can cause a double fire or a
  permanently skipped slot.
- Never change `README.md`'s schedule table without running the test suite.
- Never rewrite published history or force-push `main`.
