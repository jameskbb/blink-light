"""Keep docs/INSTRUCTIONS.md true, because it is handed to agents verbatim.

The page is written to be pasted whole into another tool's context, so a stale
command name there does not get read past and shrugged off - it gets followed,
by something with no way to tell that the repo moved on. These check the
load-bearing claims against the real CLI and the real defaults: every command
it names exists, every action form it advertises validates, and the promises
it makes about exit codes are the ones the CLI actually returns.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import re
import unittest

import argparse

from blink_light.cli import _build_parser, main
from blink_light.config import validate_config
from blink_light.defaults import default_config
from blink_light.paths import build_paths
import tempfile

INSTRUCTIONS = Path(__file__).resolve().parents[1] / "docs" / "INSTRUCTIONS.md"
REPO_ROOT = INSTRUCTIONS.parent.parent

# `blink-light.bat notify run <event>`, `notify list`, and so on, wherever they
# appear - prose, tables or code blocks.
COMMAND = re.compile(r"`(?:blink-light\.bat |)([a-z]+(?: [a-z]+)?)[^`]*`")
LINK = re.compile(r"\]\((?!https?://)([^)#]+)(?:#[^)]*)?\)")


def _subcommands(parser: argparse.ArgumentParser) -> dict:
    """A parser's subcommand choices. argparse exposes no public view of these."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


class DocumentedCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = INSTRUCTIONS.read_text(encoding="utf-8")
        cls.top = _subcommands(_build_parser())

    def _known(self, command: str, subcommand: str | None) -> bool:
        if command not in self.top:
            return False
        return subcommand is None or subcommand in _subcommands(self.top[command])

    def test_every_command_the_page_names_exists(self) -> None:
        mentioned = set()
        for match in COMMAND.findall(self.text):
            parts = match.split()
            if parts[0] in self.top:
                mentioned.add((parts[0], parts[1] if len(parts) > 1 else None))

        self.assertGreaterEqual(len(mentioned), 5, "the page should name several real commands")
        for command, subcommand in sorted(mentioned, key=lambda pair: (pair[0], pair[1] or "")):
            with self.subTest(command=f"{command} {subcommand or ''}".strip()):
                self.assertTrue(self._known(command, subcommand))

    def test_the_notify_flags_it_promises_are_real(self) -> None:
        for flag in ("--quiet-missing", "--respect-quiet-hours"):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.text)
                exit_code, output = _run(["notify", "run", "made_up_event", flag])
                # Both flags are defined on `notify run`: an unknown flag would
                # be a usage error (2) long before the event was looked at.
                self.assertNotEqual(exit_code, 2, output)

    def test_every_action_form_it_advertises_validates(self) -> None:
        forms = [
            {"scene": "agent_done_scene"},
            {"color": "#00E5FF", "fade_ms": 200},
            {"preset": "focus"},
            {"off": True},
        ]
        for form in forms:
            with self.subTest(form=next(iter(form))):
                self.assertIn(next(iter(form)), self.text)
                config = default_config()
                config["notify"] = {**config["notify"], "documented_form": form}
                validate_config(config)

    def test_its_internal_links_point_at_files_that_exist(self) -> None:
        for target in LINK.findall(self.text):
            with self.subTest(link=target):
                self.assertTrue((INSTRUCTIONS.parent / target).resolve().exists())

    def test_an_unknown_event_fails_the_way_the_page_says(self) -> None:
        exit_code, output = _run(["notify", "run", "made_up_event"])
        self.assertEqual(exit_code, 1)

        exit_code, output = _run(["notify", "run", "made_up_event", "--quiet-missing"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["notified"])
        self.assertEqual(payload["reason"], "unknown-event")


class SilentController:
    def __init__(self, serial: str | None = None):
        self.serial = serial

    def apply_action(self, action: dict, scenes: dict, persistent: bool) -> None:
        pass

    def close(self) -> None:
        pass


def _run(argv) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        paths = build_paths(
            config_path=root / "blink-light.json",
            project_root=root,
            runtime_dir=root / "runtime",
            startup_dir=root / "startup",
        )
        out = io.StringIO()
        exit_code = main(argv, paths=paths, controller_cls=SilentController, out=out, err=io.StringIO())
        return exit_code, out.getvalue()


if __name__ == "__main__":
    unittest.main()
