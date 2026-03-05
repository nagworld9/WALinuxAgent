# -*- coding: utf-8 -*-
# Copyright 2018 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.6+ and Openssl 1.0+
#

import json
import subprocess

from azurelinuxagent.common.event import WALAEventOperation
from azurelinuxagent.ga.kernel_event_monitor import MonitorKernelSoftLockup
from tests.lib.tools import AgentTestCase, patch, MagicMock


class TestMonitorKernelSoftLockup(AgentTestCase):
    """
    Tests for MonitorKernelSoftLockup.

    AgentTestCase provides self.tmp_dir and mocks conf.get_lib_dir.
    We additionally mock _get_boot_id so tests work on Windows.
    """

    _TEST_BOOT_ID = "abcdef01-2345-6789-abcd-ef0123456789"

    SAMPLE_DMESG_WITH_LOCKUPS = (
        "[    0.000000] Linux version 5.4.0-42-generic\n"
        "[12345.123456] BUG: soft lockup - CPU#0 stuck for 22s! [kworker/0:1:1234]\n"
        "[12345.234567] Modules linked in: module1 module2\n"
        "[12346.345678] BUG: soft lockup - CPU#0 stuck for 23s! [kworker/0:1:1234]\n"
        "[12347.456789] watchdog: BUG: soft lockup - CPU#2 stuck for 25s! [process:5678]\n"
        "[12350.567890] Normal kernel message\n"
        "[12351.678901] BUG: soft lockup - CPU#1 stuck for 21s! [another:9999]\n"
    )

    SAMPLE_DMESG_NO_LOCKUPS = (
        "[    0.000000] Linux version 5.4.0-42-generic\n"
        "[12345.123456] Normal kernel message\n"
        "[12347.345678] eth0: link up\n"
    )

    def _create_monitor(self, boot_id=None):
        if boot_id is None:
            boot_id = self._TEST_BOOT_ID
        with patch("azurelinuxagent.ga.kernel_event_monitor.conf") as mock_conf:
            with patch.object(MonitorKernelSoftLockup, "_get_boot_id", return_value=boot_id):
                mock_conf.get_monitor_kernel_soft_lockup_period.return_value = 300
                mock_conf.get_lib_dir.return_value = self.tmp_dir
                monitor = MonitorKernelSoftLockup()
        return monitor

    # -- Regex ---------------------------------------------------------------

    def test_soft_lockup_regex_should_match_and_extract_groups(self):
        line = "[12345.123456] BUG: soft lockup - CPU#0 stuck for 22s! [kworker/0:1:1234]"
        match = MonitorKernelSoftLockup._SOFT_LOCKUP_PATTERN.search(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "0")
        self.assertEqual(match.group(2), "22")

    def test_timestamp_regex_should_match_kernel_monotonic_format(self):
        match = MonitorKernelSoftLockup._DMESG_TIMESTAMP_PATTERN.match("[    0.000000] Linux version")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "0.000000")

    # -- Parse and aggregate -------------------------------------------------

    def test_parse_should_detect_and_aggregate_lockup_events(self):
        monitor = self._create_monitor()
        monitor._parse_and_aggregate_soft_lockup_events(self.SAMPLE_DMESG_WITH_LOCKUPS)

        # 4 lockup events across 3 CPUs: CPU#0 (x2), CPU#2 (x1), CPU#1 (x1)
        self.assertEqual(len(monitor._event_aggregates), 3)
        for key in monitor._event_aggregates:
            self.assertIsInstance(key, int)

        self.assertEqual(monitor._event_aggregates[0]["count"], 2)
        self.assertEqual(monitor._event_aggregates[0]["max_stuck_seconds"], 23)
        self.assertEqual(monitor._event_aggregates[1]["count"], 1)
        self.assertEqual(monitor._event_aggregates[2]["max_stuck_seconds"], 25)
        self.assertAlmostEqual(monitor._last_processed_timestamp, 12351.678901, places=4)

    def test_parse_should_skip_events_before_watermark(self):
        monitor = self._create_monitor()
        monitor._last_processed_timestamp = 12346.0
        monitor._parse_and_aggregate_soft_lockup_events(self.SAMPLE_DMESG_WITH_LOCKUPS)

        self.assertEqual(monitor._event_aggregates[0]["count"], 1)
        self.assertEqual(monitor._event_aggregates[0]["max_stuck_seconds"], 23)

    def test_parse_should_advance_watermark_even_without_lockups(self):
        monitor = self._create_monitor()
        monitor._parse_and_aggregate_soft_lockup_events(self.SAMPLE_DMESG_NO_LOCKUPS)

        self.assertEqual(len(monitor._event_aggregates), 0)
        self.assertAlmostEqual(monitor._last_processed_timestamp, 12347.345678, places=4)

    # -- Report events -------------------------------------------------------

    def test_report_should_send_correct_telemetry_and_clear_aggregates(self):
        monitor = self._create_monitor()
        monitor._event_aggregates = {
            0: {"count": 2, "max_stuck_seconds": 23, "last_timestamp": 12346.345678},
            1: {"count": 1, "max_stuck_seconds": 21, "last_timestamp": 12351.678901},
        }

        with patch("azurelinuxagent.ga.kernel_event_monitor.add_event") as mock_add_event:
            monitor._report_events()

            self.assertEqual(mock_add_event.call_count, 1)
            call_kwargs = mock_add_event.call_args[1]
            self.assertEqual(call_kwargs["op"], WALAEventOperation.KernelSoftLockup)
            self.assertFalse(call_kwargs["is_success"])
            self.assertFalse(call_kwargs["log_event"])

            payload = json.loads(call_kwargs["message"])
            self.assertEqual(payload["totalSoftLockups"], 3)
            self.assertEqual(payload["affectedCpuCount"], 2)
            self.assertEqual(payload["cpuDetails"][0]["cpuId"], 0)
            self.assertEqual(payload["cpuDetails"][0]["count"], 2)
            self.assertEqual(payload["cpuDetails"][0]["maxStuckTimeSec"], 23)

        self.assertEqual(len(monitor._event_aggregates), 0)

    def test_report_should_clear_aggregates_even_on_failure(self):
        monitor = self._create_monitor()
        monitor._event_aggregates = {
            0: {"count": 1, "max_stuck_seconds": 22, "last_timestamp": 12345.0},
        }

        with patch("azurelinuxagent.ga.kernel_event_monitor.add_event", side_effect=Exception("send failed")):
            monitor._report_events()

        self.assertEqual(len(monitor._event_aggregates), 0)

    def test_report_should_not_send_if_no_events(self):
        monitor = self._create_monitor()
        with patch("azurelinuxagent.ga.kernel_event_monitor.add_event") as mock_add_event:
            monitor._report_events()
            mock_add_event.assert_not_called()

    # -- State persistence ---------------------------------------------------

    def test_save_and_load_state_should_preserve_watermark(self):
        monitor = self._create_monitor()
        self.assertEqual(monitor._last_processed_timestamp, 0.0)  # no state file yet

        monitor._last_processed_timestamp = 12345.678
        monitor._save_state()

        monitor2 = self._create_monitor()
        self.assertEqual(monitor2._last_processed_timestamp, 12345.678)

    def test_load_state_should_reset_watermark_on_boot_id_change(self):
        monitor = self._create_monitor(boot_id="old-boot-id-0000-0000-000000000000")
        monitor._last_processed_timestamp = 99999.0
        monitor._save_state()

        monitor2 = self._create_monitor(boot_id="new-boot-id-1111-1111-111111111111")
        self.assertEqual(monitor2._last_processed_timestamp, 0.0)

    def test_load_state_should_reset_watermark_when_boot_id_is_falsy(self):
        """Null or empty boot_id (saved or current) should reset watermark."""
        monitor = self._create_monitor()
        state = {"last_timestamp": 99999.0, "boot_id": None}
        with open(monitor._state_file_path, 'w') as f:
            json.dump(state, f)

        monitor2 = self._create_monitor()
        self.assertEqual(monitor2._last_processed_timestamp, 0.0)

    def test_load_state_should_handle_corrupt_state_file(self):
        monitor = self._create_monitor()
        with open(monitor._state_file_path, 'w') as f:
            f.write("{invalid json")

        monitor2 = self._create_monitor()
        self.assertEqual(monitor2._last_processed_timestamp, 0.0)

    # -- dmesg output --------------------------------------------------------

    def test_get_dmesg_should_return_output_on_success(self):
        monitor = self._create_monitor()
        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.communicate.return_value = (b"[0.000000] test line\n", b"")
            mock_process.returncode = 0
            mock_popen.return_value = mock_process
            self.assertIn("test line", monitor._get_dmesg_output())

    def test_get_dmesg_should_return_empty_on_nonzero_exit(self):
        monitor = self._create_monitor()
        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.communicate.return_value = (b"", b"dmesg: read kernel buffer failed")
            mock_process.returncode = 1
            mock_popen.return_value = mock_process
            self.assertEqual(monitor._get_dmesg_output(), "")

    def test_get_dmesg_should_return_empty_on_timeout(self):
        monitor = self._create_monitor()
        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.communicate.side_effect = subprocess.TimeoutExpired(cmd="dmesg", timeout=60)
            mock_popen.return_value = mock_process
            self.assertEqual(monitor._get_dmesg_output(), "")
            self.assertEqual(mock_process.kill.call_count, 1)

    def test_get_dmesg_should_return_empty_on_generic_exception(self):
        monitor = self._create_monitor()
        with patch("subprocess.Popen", side_effect=OSError("no such file")):
            self.assertEqual(monitor._get_dmesg_output(), "")

    # -- Full operation ------------------------------------------------------

    def test_operation_should_parse_and_report(self):
        monitor = self._create_monitor()
        with patch.object(monitor, "_get_dmesg_output", return_value=self.SAMPLE_DMESG_WITH_LOCKUPS):
            with patch("azurelinuxagent.ga.kernel_event_monitor.add_event") as mock_add_event:
                monitor._operation()
                self.assertEqual(mock_add_event.call_count, 1)
                payload = json.loads(mock_add_event.call_args[1]["message"])
                self.assertEqual(payload["totalSoftLockups"], 4)
                self.assertEqual(payload["affectedCpuCount"], 3)

    def test_operation_watermark_persists_across_runs(self):
        """Second run with same dmesg should not report -- watermark filters old events."""
        monitor = self._create_monitor()
        with patch.object(monitor, "_get_dmesg_output", return_value=self.SAMPLE_DMESG_WITH_LOCKUPS):
            with patch("azurelinuxagent.ga.kernel_event_monitor.add_event"):
                monitor._operation()

        with patch.object(monitor, "_get_dmesg_output", return_value=self.SAMPLE_DMESG_WITH_LOCKUPS):
            with patch("azurelinuxagent.ga.kernel_event_monitor.add_event") as mock_add_event:
                monitor._operation()
                mock_add_event.assert_not_called()
