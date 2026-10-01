"""Keep the printed reference card honest.

The card is meant to be pinned above a desk, which is the one place a stale
colour is worst: nobody rereads a printout to check it still agrees with the
code. It is generated from the defaults rather than typed, so the only drift
left is forgetting to regenerate it - and that is what this catches.

Same spirit as test_readme_schedule.py: the committed artefact has to match
what the current defaults produce.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import build_reference_card  # noqa: E402
from blink_light.defaults import BUILTIN_PRESETS, STANDUP_COLOR  # noqa: E402


class ReferenceCardTests(unittest.TestCase):
    def test_the_committed_card_matches_what_the_defaults_produce_now(self):
        committed = build_reference_card.OUTPUT.read_bytes()
        self.assertEqual(
            committed,
            build_reference_card.build(),
            "docs/blink-light-reference-card.pdf is stale - rerun tools/build_reference_card.py.",
        )

    def test_the_card_is_rebuilt_byte_for_byte(self):
        """No timestamps or ids, or the test above would fail on every run."""
        self.assertEqual(build_reference_card.build(), build_reference_card.build())

    def test_every_colour_on_the_card_is_read_from_the_defaults(self):
        rows = build_reference_card.SCHEDULE_ROWS + build_reference_card.EVENT_ROWS
        standup = [row for row in rows if "Standup" in row[1]]
        self.assertTrue(standup)
        for row in standup:
            self.assertEqual(row[2]["hex"], STANDUP_COLOR.upper())

    def test_the_preset_strip_only_names_presets_that_exist(self):
        for name, _ in build_reference_card.PRESET_SWATCHES:
            self.assertIn("color", BUILTIN_PRESETS[name])


if __name__ == "__main__":
    unittest.main()
