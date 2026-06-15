#!/usr/bin/env python3

# Microsoft Azure Linux Agent
#
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

#
# Validates that the ext-handler shuts down gracefully on the natural exit paths
# (AgentUpgradeExitException / ExitException). The agent's UpdateHandler.run() loop catches these
# exceptions, calls UpdateHandler._shutdown() and then sys.exit(0). _shutdown() must signal every
# worker thread to stop and wait (with a bounded timeout) for each of them to finish.
#
# How the test works:
#   1. We use the same self-update setup as the AgentUpdate suite: a custom older agent package is
#      built, installed on the VM, and AutoUpdate.UpdateToLatestVersion=y is configured. The setup
#      script also rotates /var/log/waagent.log so we capture only logs from this test run.
#   2. We enable log collection (Logs.Collect=y) so the CollectLogsHandler thread is started in
#      addition to the four threads always launched by run() (MonitorHandler, EnvHandler,
#      SendTelemetryHandler, TelemetryEventsCollector).
#   3. We wait for the agent to self-upgrade to the latest published version. The upgrade path
#      raises AgentUpgradeExitException, which run() catches and which triggers _shutdown().
#   4. We read /var/log/waagent.log and validate that, around the upgrade event, each worker
#      thread has both a "Signaling X thread to stop..." line and a terminal line
#      ("X thread stopped successfully" or "X thread did not stop within the timeout").
#

import re

from typing import Any, Dict, List

from assertpy import fail

from tests_e2e.tests.agent_update.self_update import SelfUpdateBvt
from tests_e2e.tests.lib.logging import log
from tests_e2e.tests.lib.retry import retry_if_false


# Threads always started by UpdateHandler.run().
_ALWAYS_RUNNING_THREADS = [
    "MonitorHandler",
    "EnvHandler",
    "SendTelemetryHandler",
    "TelemetryEventsCollector",
]

# Started only when log collection is enabled.
_LOG_COLLECTOR_THREAD = "CollectLogsHandler"

# Emitted by collect_logs.is_log_collection_allowed() during agent startup. The agent logs
#   "Log collection is supported. All three conditions must be met: ..."     when allowed
#   "Log collection is not supported. All three conditions must be met: ..." when not allowed
_LOG_COLLECTION_ALLOWED_RE = re.compile(r"Log collection is supported\.")

# AgentUpgradeExitException reason produced by self_update_version_updater.proceed_with_update().
# We use this as an anchor in the log to locate the AgentUpgrade-driven shutdown sequence.
_UPGRADE_MARKER_RE = re.compile(
    r"completed all update checks, exiting current process to upgrade to the new Agent version")

# Patterns emitted by UpdateHandler._shutdown(); see azurelinuxagent/ga/update.py.
_SIGNAL_LINE_RE = re.compile(r"Signaling (\S+) thread to stop\.\.\.")
_STOPPED_OK_RE = re.compile(r"(\S+) thread stopped successfully")
_STOPPED_TIMEOUT_RE = re.compile(r"(\S+) thread did not stop within the timeout")


class AgentGracefulShutdown(SelfUpdateBvt):
    """
    Verifies the graceful-shutdown sequence emitted by UpdateHandler._shutdown() when the
    ext-handler exits via AgentUpgradeExitException (the self-update path).
    """

    def run(self):
        log.info("Setting up the VM with a custom older-version agent package...")
        # _test_setup() is provided by SelfUpdateBvt. It rotates /var/log/waagent.log, installs
        # the custom older-version pkg, and configures AutoUpdate.UpdateToLatestVersion=y so the
        # agent will self-upgrade on the next iteration.
        self._test_setup()

        log.info("Enabling log collection so the CollectLogsHandler thread is started...")
        # Reduce the initial delay so the log collector thread has time to start before the
        # self-upgrade fires. 60s is the smallest value that still allows a single collection
        # cycle if the agent runs long enough; if it does not, we still validate the four threads
        # that are always started by run().
        self._ssh_client.run_command(
            "update-waagent-conf Logs.Collect=y Debug.LogCollectorInitialDelay=60",
            use_sudo=True)

        log.info("Waiting for the agent to self-upgrade to the latest published version...")
        # _verify_agent_updated_to_latest_version() polls waagent --version until the running
        # agent matches the latest version on the manifest. This is what guarantees that the
        # AgentUpgradeExitException path was taken on the original (custom) agent process and
        # therefore _shutdown() has run.
        self._verify_agent_updated_to_latest_version()

        log.info("Verifying the graceful-shutdown sequence in /var/log/waagent.log...")
        # The shutdown sequence is logged by the original (custom) agent process before it exits, and the
        # new (latest) agent process appends to the same /var/log/waagent.log after it starts. So we just
        # read the current file; the shutdown lines for the original process are still present in it.
        self._verify_shutdown_sequence()

    def _verify_shutdown_sequence(self) -> None:
        # Allow a short retry window in case the upgrade marker has not been flushed yet.
        success = retry_if_false(self._check_shutdown_sequence, attempts=5, delay=15)
        if not success:
            fail("Did not find the expected graceful-shutdown sequence in /var/log/waagent.log "
                 "after the agent self-upgraded. See the logs above for details.")

    def _check_shutdown_sequence(self) -> bool:
        # Read the current waagent.log via SSH. The setup script rotates the log, so this file
        # only contains log lines from after the test setup ran.
        contents = self._ssh_client.run_command("cat /var/log/waagent.log", use_sudo=True)
        lines = contents.splitlines()

        # Find the AgentUpgrade marker; if it is not yet present the upgrade has not started.
        upgrade_indices = [i for i, line in enumerate(lines) if _UPGRADE_MARKER_RE.search(line)]
        if not upgrade_indices:
            log.info("AgentUpgrade marker not found yet in waagent.log; will retry.")
            return False

        # Take the slice of the log starting from the upgrade marker. The shutdown sequence is
        # emitted between this marker and the call to sys.exit(0), so anything after the marker
        # (and before the next agent process starts logging) is the shutdown.
        shutdown_section = lines[upgrade_indices[0]:]

        signaled = set()
        terminal = set()  # threads that either stopped successfully or were reported as timed out
        for line in shutdown_section:
            m = _SIGNAL_LINE_RE.search(line)
            if m:
                signaled.add(m.group(1))
                continue
            m = _STOPPED_OK_RE.search(line)
            if m:
                terminal.add(m.group(1))
                continue
            m = _STOPPED_TIMEOUT_RE.search(line)
            if m:
                terminal.add(m.group(1))
                continue

        # Decide which threads we expect in the shutdown sequence. The four threads in
        # _ALWAYS_RUNNING_THREADS are launched unconditionally by UpdateHandler.run(), so we
        # always require them. CollectLogsHandler is launched only when is_log_collection_allowed()
        # returns True at startup (controlled by configuration AND runtime preconditions like
        # cgroup support, supported python, etc.). We asked for it via configuration, but if the
        # runtime preconditions are not met the thread will not be started and will not appear in
        # the shutdown sequence at all. So we look for the agent's startup log line
        # "Log collection is supported." to decide whether it is expected.
        expected_threads = set(_ALWAYS_RUNNING_THREADS)
        log_collection_allowed = any(_LOG_COLLECTION_ALLOWED_RE.search(line) for line in lines)
        if log_collection_allowed:
            expected_threads.add(_LOG_COLLECTOR_THREAD)
        else:
            log.info(
                "Log collection is not allowed on this VM (no 'Log collection is supported.' entry "
                "in waagent.log); CollectLogsHandler is not expected in the shutdown sequence.")

        missing_signals = expected_threads - signaled
        missing_terminal = expected_threads - terminal

        log.info(
            "Graceful-shutdown sequence:\n"
            "    expected threads      : %s\n"
            "    signaled to stop      : %s\n"
            "    stopped or timed out  : %s",
            sorted(expected_threads), sorted(signaled), sorted(terminal))

        if missing_signals:
            log.info("Missing 'Signaling ... to stop' entries for: %s", sorted(missing_signals))
            return False
        if missing_terminal:
            log.info("Missing terminal ('stopped successfully' or 'did not stop within the timeout') "
                     "entries for: %s", sorted(missing_terminal))
            return False

        log.info("All expected threads were signaled and either stopped or timed out as expected.")
        return True

    def get_ignore_error_rules(self) -> List[Dict[str, Any]]:
        # _shutdown() emits a WARNING when a thread does not exit within the join timeout. That is
        # an expected outcome under load (the worker may be busy when the signal arrives) and the
        # test treats it as a successful "did stop or timed out" result, so silence it in the log
        # error scan.
        return [
            {
                "message": r"\S+ thread did not stop within the timeout",
                "if": lambda r: r.level == "WARNING",
            },
        ]


if __name__ == "__main__":
    AgentGracefulShutdown.run_from_command_line()
