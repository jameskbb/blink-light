"""Guardrails: credentials must not be able to enter the repo.

The rules themselves live in blink_light/secret_scan.py, which the pre-commit
hook also runs - these tests check the rules are right and that the repo is
currently clean under them. A gitignore entry only helps the person who
remembered to add it; a failing test travels.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from blink_light.env_file import ENV_CONFIG_MAP, apply_env_overrides, parse_env_text, resolve_env
from blink_light.secret_scan import find_secrets, path_is_forbidden, scan_tracked

ROOT = Path(__file__).resolve().parents[1]


def git_available() -> bool:
    result = subprocess.run(["git", "rev-parse"], cwd=ROOT, capture_output=True, check=False)
    return result.returncode == 0


class SecretRuleTests(unittest.TestCase):
    def test_credential_assignments_are_detected(self) -> None:
        samples = [
            '"client_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"',
            "BLINK_LIGHT_GRAPH_TENANT_ID=f0e1d2c3-b4a5-4968-8776-5a4b3c2d1e0f",
            "password = hunter2hunter2hunter2",
            "access_token: ya29.aVeryLongOpaqueTokenValue",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(find_secrets(sample), f"missed: {sample}")

    def test_placeholders_and_prose_are_not_flagged(self) -> None:
        samples = [
            '"client_id": ""',
            "BLINK_LIGHT_GRAPH_CLIENT_ID=",
            "BLINK_LIGHT_GRAPH_TENANT_ID=organizations",
            "Copy the Application (client) ID from the Overview page.",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(find_secrets(sample), [], f"false positive: {sample}")

    def test_forbidden_paths(self) -> None:
        for path in (".env", ".env.local", "graph-token-cache.json", "id.pem", "blink-light.local.json"):
            with self.subTest(path=path):
                self.assertTrue(path_is_forbidden(path))

    def test_the_template_is_allowed(self) -> None:
        self.assertFalse(path_is_forbidden(".env.template"))


class RepoIsCleanTests(unittest.TestCase):
    def setUp(self) -> None:
        if not git_available():
            self.skipTest("git is not available here")

    def test_no_tracked_file_carries_a_credential(self) -> None:
        """The check that would have caught a client id pasted into the config."""
        self.assertEqual(scan_tracked(ROOT), [])

    def test_git_actually_ignores_env_files_and_token_caches(self) -> None:
        """Asserting on git's behaviour, not on the text of .gitignore."""
        for candidate in (".env", "graph-token-cache.json", "blink-light.local.json"):
            result = subprocess.run(
                ["git", "check-ignore", "-q", candidate],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            with self.subTest(candidate=candidate):
                self.assertEqual(result.returncode, 0, f"{candidate} is not ignored")

    def test_the_hook_is_wired_to_the_shared_scan(self) -> None:
        hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
        self.assertIn("blink_light.secret_scan", hook)
        # Fail-closed: no Python must mean no commit, not a silent pass.
        self.assertIn("exit 1", hook)


class EnvFileTests(unittest.TestCase):
    def test_parsing_handles_comments_quotes_and_export(self) -> None:
        values = parse_env_text(
            "\n".join(
                [
                    "# a comment",
                    "",
                    "BLINK_LIGHT_GRAPH_CLIENT_ID=abc-123",
                    'export BLINK_LIGHT_GRAPH_TENANT_ID="tenant-guid"',
                    "IGNORED_LINE_WITHOUT_EQUALS",
                ]
            )
        )
        self.assertEqual(values["BLINK_LIGHT_GRAPH_CLIENT_ID"], "abc-123")
        self.assertEqual(values["BLINK_LIGHT_GRAPH_TENANT_ID"], "tenant-guid")
        self.assertNotIn("IGNORED_LINE_WITHOUT_EQUALS", values)

    def test_a_bom_does_not_swallow_the_first_key(self) -> None:
        """Set-Content -Encoding utf8 and Notepad both write one."""
        values = parse_env_text("﻿BLINK_LIGHT_GRAPH_CLIENT_ID=abc-123\n")
        self.assertEqual(values.get("BLINK_LIGHT_GRAPH_CLIENT_ID"), "abc-123")

    def test_a_bom_written_file_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            env_path = Path(name) / ".env"
            env_path.write_text("BLINK_LIGHT_GRAPH_CLIENT_ID=abc-123\n", encoding="utf-8-sig")
            resolved = resolve_env(env_path, environ={})
        self.assertEqual(resolved.get("BLINK_LIGHT_GRAPH_CLIENT_ID"), "abc-123")

    def test_a_real_environment_variable_beats_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            env_path = Path(name) / ".env"
            env_path.write_text("BLINK_LIGHT_GRAPH_CLIENT_ID=from-file\n", encoding="utf-8")
            resolved = resolve_env(env_path, environ={"BLINK_LIGHT_GRAPH_CLIENT_ID": "from-environ"})
        self.assertEqual(resolved["BLINK_LIGHT_GRAPH_CLIENT_ID"], "from-environ")

    def test_overrides_land_in_the_config_tree(self) -> None:
        config = {"calendar": {"graph": {"client_id": "", "tenant_id": "organizations"}}}
        apply_env_overrides(
            config,
            {"BLINK_LIGHT_GRAPH_CLIENT_ID": "abc", "BLINK_LIGHT_GRAPH_TENANT_ID": "xyz"},
        )
        self.assertEqual(config["calendar"]["graph"], {"client_id": "abc", "tenant_id": "xyz"})

    def test_an_empty_value_does_not_clobber_the_config(self) -> None:
        config = {"calendar": {"graph": {"client_id": "already-set", "tenant_id": "organizations"}}}
        apply_env_overrides(config, {"BLINK_LIGHT_GRAPH_CLIENT_ID": "  "})
        self.assertEqual(config["calendar"]["graph"]["client_id"], "already-set")

    def test_every_mapped_variable_is_documented_in_the_template(self) -> None:
        template = ROOT / ".env.template"
        if not template.exists():
            self.skipTest(".env.template has not been created yet")
        text = template.read_text(encoding="utf-8")
        for key in ENV_CONFIG_MAP:
            with self.subTest(key=key):
                self.assertIn(key, text)


if __name__ == "__main__":
    unittest.main()
