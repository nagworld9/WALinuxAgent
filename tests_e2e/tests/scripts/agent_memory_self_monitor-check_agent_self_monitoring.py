#!/usr/bin/env pypy3
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
"""
End-to-end validation of the agent's anon-memory self-monitoring + clean-exit
mitigation flow.

Approach
--------
1. Force the agent into a "always breach" configuration:
     - Debug.AgentAnonMemoryQuota=1 (bytes)             -> any anon usage breaches
     - Debug.AgentMemoryConsecutiveBreachCount=2    -> exit after 2 consecutive
     - Debug.CgroupCheckPeriod=30                   -> ~30s between samples
     - Debug.AgentMemoryMinRestartIntervalSeconds=300  -> block re-restart shortly
     - Debug.AgentMemoryMaxRestartsPerVersion=1     -> guardrail after first exit
     - Debug.EnableAgentMemoryUsageCheck=y
2. Restart the agent and wait for extensions to converge.
3. Verify the log shows two consecutive breach records, then a "sustained anon
   memory breach ... exiting" line.
4. Verify the persisted restart-history file exists and contains an entry for
   the current agent version.
5. Wait for the daemon to relaunch the ext-handler; verify the next breach
   cycle emits the "self-restart skipped" line (guardrails block it).
6. Cleanup: reset all Debug knobs and restart the agent service.
"""
import datetime
import json
import os
import sys

from assertpy import fail

from azurelinuxagent.common.future import UTC
from azurelinuxagent.common.utils import shellutil
from azurelinuxagent.ga import state_dir
from azurelinuxagent.ga.agent_memory_restart_history import HISTORY_FILE_NAME
from azurelinuxagent.ga.update import CHILD_LAUNCH_INTERVAL

from tests_e2e.tests.lib.cgroup_helpers import check_log_message, using_cgroupv2
from tests_e2e.tests.lib.logging import log
from tests_e2e.tests.lib.remote_test import run_remote_test
from tests_e2e.tests.lib.retry import retry_if_false


# -----------------------------------------------------------------------------
# Config knobs used to force a deterministic breach in a short window.
# -----------------------------------------------------------------------------
_TEST_CONF = [
    "Debug.EnableAgentMemoryUsageCheck=y",
    "Debug.AgentAnonMemoryQuota=1",
    "Debug.AgentMemoryConsecutiveBreachCount=2",
    "Debug.AgentMemoryMaxRestartsPerVersion=1",
    "Debug.AgentMemoryMinRestartIntervalSeconds=300",
    "Debug.CgroupCheckPeriod=30",
    "Debug.CgroupLogMetrics=y",
]

_DEFAULT_CONF = [
    "Debug.EnableAgentMemoryUsageCheck=n",
    "Debug.AgentAnonMemoryQuota={0}".format(300 * 1024 ** 2),
    "Debug.AgentMemoryConsecutiveBreachCount=3",
    "Debug.AgentMemoryMaxRestartsPerVersion=5",
    "Debug.AgentMemoryMinRestartIntervalSeconds={0}".format(3 * 24 * 60 * 60),
    "Debug.CgroupCheckPeriod=300",
]


def _skip_if_unsupported():
    if not using_cgroupv2():
        log.info("Skipping anon-memory self-monitor e2e test: distro is not cgroup v2")
        sys.exit(0)


def _apply_conf(kv_pairs):
    log.info("Applying waagent.conf overrides: %s", kv_pairs)
    shellutil.run_command(["update-waagent-conf"] + kv_pairs)


def _restart_agent():
    log.info("Restarting the agent service")
    shellutil.run_command(["agent-service", "restart"])


def _wait_for_agent_startup(after):
    log.info("Waiting for the agent to be up (enabling cgroups) after %s", after.isoformat())
    if not retry_if_false(lambda: check_log_message("Agent cgroups enabled: True", after_timestamp=after)):
        fail("The agent did not report 'Agent cgroups enabled: True' after restart")


def verify_consecutive_breach_events(after):
    log.info("** Verifying that consecutive breach events are logged")
    # The first breach can only appear after CHILD_LAUNCH_INTERVAL has elapsed
    # since agent start (gate in _check_agent_memory_usage) plus one
    # CgroupCheckPeriod.
    first_breach_attempts = (CHILD_LAUNCH_INTERVAL // 60) + 4  # + headroom
    if not retry_if_false(lambda: check_log_message(r"Agent anon memory breach 1/2", after_timestamp=after),
                          attempts=first_breach_attempts, delay=60):
        fail("Did not observe the first anon-memory breach log line")
    if not retry_if_false(lambda: check_log_message(r"Agent anon memory breach 2/2", after_timestamp=after)):
        fail("Did not observe the second anon-memory breach log line")
    log.info("Successfully observed 2 consecutive breach log lines")


def verify_agent_exited_due_to_breach(after):
    log.info("** Verifying that the agent exited due to sustained anon memory breach")
    if not retry_if_false(
            lambda: check_log_message(r"sustained anon memory breach", after_timestamp=after),
            delay=15):
        fail("The agent did not log the 'sustained anon memory breach' exit reason")
    log.info("Successfully observed the ExitException reason in the agent log")


def verify_restart_history_recorded():
    log.info("** Verifying the persisted restart-history file")
    history_path = os.path.join(state_dir.get_state_dir(), HISTORY_FILE_NAME)

    def _exists():
        return os.path.exists(history_path)

    if not retry_if_false(_exists, delay=10):
        fail("Restart history file was not created at {0}".format(history_path))

    with open(history_path) as f:
        data = json.load(f)

    versions = data.get("versions") or {}
    if not versions:
        fail("Restart history file has no 'versions' entries: {0}".format(data))

    # Must contain exactly one entry for at least one version and it must include
    # a timestamp and anon_bytes field.
    total_entries = sum(len(v) for v in versions.values())
    if total_entries < 1:
        fail("Restart history file has zero recorded restarts: {0}".format(data))

    sample = next(iter(versions.values()))[0]
    if "timestamp" not in sample or "anon_bytes" not in sample:
        fail("Restart history entry is missing required fields: {0}".format(sample))
    log.info("Restart history file OK: %s", data)


def verify_guardrail_blocks_second_restart(after):
    """
    With MaxRestartsPerVersion=1 already recorded, the next breach cycle after
    the daemon relaunches the ext-handler must NOT exit again -- instead it must
    log the "self-restart skipped" reason from the guardrail check.
    """
    attempts = (CHILD_LAUNCH_INTERVAL // 60) + 4  # + headroom
    log.info("** Verifying that the guardrail blocks a second self-restart")
    if not retry_if_false(
            lambda: check_log_message(r"self-restart skipped", after_timestamp=after),
            attempts=attempts, delay=60):
        fail("The guardrail did not block the second breach cycle (no 'self-restart skipped' log)")
    log.info("Successfully observed the guardrail 'self-restart skipped' log")


def cleanup():
    log.info("Cleaning up: restoring default waagent.conf values and deleting restart history")
    history_path = os.path.join(state_dir.get_state_dir(), HISTORY_FILE_NAME)
    try:
        if os.path.exists(history_path):
            os.remove(history_path)
    except Exception as e:
        log.warning("Failed to remove %s: %s", history_path, e)
    _apply_conf(_DEFAULT_CONF)
    _restart_agent()
    _wait_for_agent_startup(datetime.datetime.now(UTC))


def main():
    _skip_if_unsupported()

    try:
        # Baseline: nothing recorded, capture the timestamp so all log assertions
        # ignore any prior test noise.
        start = datetime.datetime.now(UTC)

        _apply_conf(_TEST_CONF)
        _restart_agent()
        _wait_for_agent_startup(start)

        verify_consecutive_breach_events(start)
        verify_agent_exited_due_to_breach(start)
        verify_restart_history_recorded()

        # From here on, the daemon has relaunched the ext-handler. The new
        # process must observe the guardrail block on its next breach cycle.
        after_first_exit = datetime.datetime.now(UTC)
        verify_guardrail_blocks_second_restart(after_first_exit)

    finally:
        cleanup()


run_remote_test(main)
