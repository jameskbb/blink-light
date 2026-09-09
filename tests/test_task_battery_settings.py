"""A scheduled task must survive an undocked laptop.

`schtasks /Create` switches on both battery conditions, which makes Windows
refuse to start the task on battery - silently, with the task parked in
`Queued` and a `LastTaskResult` of 0. Undocked is the normal case for this
light, so both installers clear the pair by round-tripping the task XML.

The round-trip is the fragile part: get the substitution wrong and the install
still reports success while the task stays battery-blocked. These pin it
without touching the real Task Scheduler.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from blink_light import chime

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2">
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Settings>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <Enabled>true</Enabled>
  </Settings>
</Task>
"""


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["schtasks"], returncode=returncode, stdout=stdout, stderr="")


class BatterySettingsTests(unittest.TestCase):
    def _run_with(self, query_result: subprocess.CompletedProcess, create_returncode: int = 0):
        """Drive _allow_task_on_battery, capturing the XML it writes back."""
        written: dict[str, str] = {}

        def fake_schtasks(arguments: list[str]) -> subprocess.CompletedProcess:
            if "/Query" in arguments:
                return query_result
            path = arguments[arguments.index("/XML") + 1]
            with open(path, encoding="utf-16") as handle:
                written["xml"] = handle.read()
            return completed(create_returncode)

        with mock.patch.object(chime, "_run_schtasks", side_effect=fake_schtasks):
            result = chime._allow_task_on_battery("BlinkLight Autostart")
        return result, written

    def test_both_battery_conditions_are_turned_off(self) -> None:
        ok, written = self._run_with(completed(stdout=TASK_XML))

        self.assertTrue(ok)
        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", written["xml"])
        self.assertIn("<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", written["xml"])

    def test_nothing_else_in_the_task_is_rewritten(self) -> None:
        # The trigger is the thing that would quietly break: a task that no
        # longer fires hourly looks identical to one that does.
        _, written = self._run_with(completed(stdout=TASK_XML))

        self.assertIn("<LogonTrigger><Enabled>true</Enabled></LogonTrigger>", written["xml"])
        self.assertIn("<Enabled>true</Enabled>\n  </Settings>", written["xml"])

    def test_a_task_that_cannot_be_exported_reports_failure_rather_than_raising(self) -> None:
        # Best-effort: a failure here costs a chime on battery, not the install.
        ok, written = self._run_with(completed(returncode=1))

        self.assertFalse(ok)
        self.assertEqual(written, {})

    def test_a_rejected_reimport_is_reported_as_failure(self) -> None:
        ok, _ = self._run_with(completed(stdout=TASK_XML), create_returncode=1)

        self.assertFalse(ok)

    def test_the_temporary_xml_file_does_not_outlive_the_call(self) -> None:
        paths: list[str] = []

        def fake_schtasks(arguments: list[str]) -> subprocess.CompletedProcess:
            if "/Query" in arguments:
                return completed(stdout=TASK_XML)
            paths.append(arguments[arguments.index("/XML") + 1])
            return completed()

        with mock.patch.object(chime, "_run_schtasks", side_effect=fake_schtasks):
            chime._allow_task_on_battery("BlinkLight Autostart")

        self.assertEqual(len(paths), 1)
        self.assertFalse(chime.Path(paths[0]).exists())


if __name__ == "__main__":
    unittest.main()
