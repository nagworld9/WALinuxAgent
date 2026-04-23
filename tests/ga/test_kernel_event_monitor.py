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

from azurelinuxagent.common.event import WALAEventOperation
from azurelinuxagent.ga.kernel_event_monitor import MonitorKernelSoftLockup
from tests.lib.tools import AgentTestCase, patch, MagicMock


class TestMonitorKernelSoftLockup(AgentTestCase):
    """
    Tests for MonitorKernelSoftLockup.

    AgentTestCase provides self.tmp_dir and mocks get_state_dir.
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

    @staticmethod
    def _get_soft_lockup_events(add_event_mock):
        return [kwargs for _, kwargs in add_event_mock.call_args_list if kwargs.get("op") == WALAEventOperation.KernelSoftLockup]

    @staticmethod
    def _create_monitor(boot_id=None):
        if boot_id is None:
            boot_id = TestMonitorKernelSoftLockup._TEST_BOOT_ID
        with patch.object(MonitorKernelSoftLockup, "_get_boot_id", return_value=boot_id):
            monitor = MonitorKernelSoftLockup()
        return monitor

    @staticmethod
    def _feed_dmesg(monitor, dmesg_output):
        """Feed dmesg lines into the parser (no mocking needed)."""
        for line in dmesg_output.strip().split('\n'):
            monitor._parse_and_aggregate_soft_lockup_events(line)

    # -- Regex ---------------------------------------------------------------

    def test_soft_lockup_regex_should_match_and_extract_groups(self):
        line = "[12345.123456] BUG: soft lockup - CPU#0 stuck for 22s! [kworker/0:1:1234]"
        match = MonitorKernelSoftLockup._SOFT_LOCKUP_PATTERN.search(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group('cpu_id'), "0")
        self.assertEqual(match.group('stuck_seconds'), "22")

    def test_timestamp_regex_should_match_kernel_monotonic_format(self):
        match = MonitorKernelSoftLockup._DMESG_TIMESTAMP_PATTERN.match("[    0.000000] Linux version")
        self.assertIsNotNone(match)
        self.assertEqual(match.group('timestamp'), "0.000000")

    # -- Parse and aggregate -------------------------------------------------

    def test_parse_should_detect_and_aggregate_lockup_events(self):
        monitor = self._create_monitor()
        self._feed_dmesg(monitor, self.SAMPLE_DMESG_WITH_LOCKUPS)

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
        self._feed_dmesg(monitor, self.SAMPLE_DMESG_WITH_LOCKUPS)

        self.assertEqual(monitor._event_aggregates[0]["count"], 1)
        self.assertEqual(monitor._event_aggregates[0]["max_stuck_seconds"], 23)

    def test_parse_should_advance_watermark_even_without_lockups(self):
        monitor = self._create_monitor()
        self._feed_dmesg(monitor, self.SAMPLE_DMESG_NO_LOCKUPS)

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

            events = self._get_soft_lockup_events(mock_add_event)
            self.assertEqual(len(events), 1)
            call_kwargs = events[0]
            self.assertEqual(call_kwargs["op"], WALAEventOperation.KernelSoftLockup)
            self.assertTrue(call_kwargs["is_success"])
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

    def test_save_and_get_saved_timestamp_should_preserve_watermark(self):
        monitor = self._create_monitor()
        self.assertEqual(monitor._last_processed_timestamp, 0.0)  # no state file yet

        monitor._last_processed_timestamp = 12345.678
        monitor._save_state()

        monitor2 = self._create_monitor()
        self.assertEqual(monitor2._last_processed_timestamp, 12345.678)

    def test_get_saved_timestamp_should_reset_watermark_on_boot_id_change(self):
        monitor = self._create_monitor(boot_id="old-boot-id-0000-0000-000000000000")
        monitor._last_processed_timestamp = 99999.0
        monitor._save_state()

        monitor2 = self._create_monitor(boot_id="new-boot-id-1111-1111-111111111111")
        self.assertEqual(monitor2._last_processed_timestamp, 0.0)

    def test_get_saved_timestamp_should_reset_watermark_when_boot_id_is_unknown(self):
        """Empty boot_id in saved state should reset watermark."""
        monitor = self._create_monitor()
        state = {"last_timestamp": 99999.0, "boot_id": ""}
        with open(monitor._state_file_path, 'w') as f:
            json.dump(state, f)

        monitor2 = self._create_monitor()
        self.assertEqual(monitor2._last_processed_timestamp, 0.0)

    def test_get_saved_timestamp_should_handle_corrupt_state_file(self):
        monitor = self._create_monitor()
        with open(monitor._state_file_path, 'w') as f:
            f.write("{invalid json")

        monitor2 = self._create_monitor()
        self.assertEqual(monitor2._last_processed_timestamp, 0.0)

    # -- dmesg read ------------------------------------------------------------

    def test_read_and_parse_dmesg_should_parse_on_success(self):
        monitor = self._create_monitor()
        dmesg_line = "[12345.123456] BUG: soft lockup - CPU#0 stuck for 22s! [kworker/0:1:1234]"
        mock_process = MagicMock()
        mock_process.stdout = [dmesg_line.encode('utf-8')]
        mock_process.wait.return_value = 0
        mock_process.pid = 12345
        with patch("azurelinuxagent.common.utils.shellutil.subprocess.Popen", return_value=mock_process):
            monitor._read_and_parse_dmesg()
            self.assertEqual(monitor._event_aggregates[0]["count"], 1)

    def test_read_and_parse_dmesg_should_not_crash_on_failure(self):
        monitor = self._create_monitor()
        with patch("azurelinuxagent.common.utils.shellutil.subprocess.Popen", side_effect=Exception("command failed")):
            monitor._read_and_parse_dmesg()
        self.assertEqual(len(monitor._event_aggregates), 0)

    # -- Full operation ------------------------------------------------------

    def test_operation_should_parse_and_report(self):
        monitor = self._create_monitor()
        with patch.object(monitor, "_read_and_parse_dmesg",
                          side_effect=lambda: self._feed_dmesg(monitor, self.SAMPLE_DMESG_WITH_LOCKUPS)):
            with patch("azurelinuxagent.ga.kernel_event_monitor.add_event") as mock_add_event:
                monitor._operation()
                events = self._get_soft_lockup_events(mock_add_event)
                self.assertEqual(len(events), 1)
                payload = json.loads(events[0]["message"])
                self.assertEqual(payload["totalSoftLockups"], 4)
                self.assertEqual(payload["affectedCpuCount"], 3)

    def test_operation_watermark_persists_across_runs(self):
        """Second run with same dmesg should not report -- watermark filters old events."""
        monitor = self._create_monitor()
        feed = lambda: self._feed_dmesg(monitor, self.SAMPLE_DMESG_WITH_LOCKUPS)
        with patch.object(monitor, "_read_and_parse_dmesg", side_effect=feed):
            with patch("azurelinuxagent.ga.kernel_event_monitor.add_event"):
                monitor._operation()

        with patch.object(monitor, "_read_and_parse_dmesg", side_effect=feed):
            with patch("azurelinuxagent.ga.kernel_event_monitor.add_event") as mock_add_event:
                monitor._operation()
                self.assertEqual(len(self._get_soft_lockup_events(mock_add_event)), 0)
