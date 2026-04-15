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
# This script verifies the agent's anon memory self-monitoring and restart behavior.
#
# It configures the agent with a low anon memory limit and verifies that the agent detects sustained
# anon memory breaches, logs the appropriate messages, exits, and is restarted by the daemon.
#
import datetime
import json
import os
import re
import sys
import time

from assertpy import fail

from azurelinuxagent.common import conf
from azurelinuxagent.common.future import UTC
from azurelinuxagent.common.utils import shellutil
from tests_e2e.tests.lib.agent_log import AgentLog
from tests_e2e.tests.lib.cgroup_helpers import check_log_message, using_cgroupv2, AGENT_SERVICE_NAME
from tests_e2e.tests.lib.logging import log
from tests_e2e.tests.lib.remote_test import run_remote_test
from tests_e2e.tests.lib.retry import retry_if_false

# The agent's normal anon memory usage is above 30MB, so setting the limit to 30MB triggers the breach.
LOW_MEMORY_LIMIT = 30 * 1024 ** 2  # 30 MB


def skip_if_not_cgroupv2():
    if not using_cgroupv2():
        log.info("Skipping memory limit breach test as the distro is not using cgroupv2")
        sys.exit(0)


def cleanup_restart_timestamps():
    """
    Remove any existing memory restart timestamps file so the test starts with a clean state.
    """
    timestamps_file = os.path.join(conf.get_lib_dir(), "memory_restart_timestamps")
    if os.path.exists(timestamps_file):
        log.info("Removing existing memory restart timestamps file: %s", timestamps_file)
        os.remove(timestamps_file)


def prepare_agent():
    """
    Configure the agent with a low memory limit, enable memory usage checks, and use a short check period and
    sustained check count so the breach is detected quickly.

    Settings:
      - AgentMemoryQuota = 30 MB (agent's normal usage exceeds this)
      - EnableAgentMemoryUsageCheck = y
      - CgroupCheckPeriod = 20 seconds
      - AgentMemorySustainedCheckCount = 2 (breach must persist for 2 consecutive checks)
      - AgentMemoryRestartCooldown = 0 (no cooldown between restarts for testing)
      - CgroupDisableOnProcessCheckFailure = n (prevent cgroups from being disabled)
      - CgroupDisableOnQuotaCheckFailure = n
      - CgroupLogMetrics = y
    """
    log.info("** Preparing agent with low memory limit to trigger breach detection")
    cleanup_restart_timestamps()

    check_time = datetime.datetime.now(UTC)
    result = shellutil.run_command([
        "update-waagent-conf",
        "Debug.AgentMemoryQuota={0}".format(LOW_MEMORY_LIMIT),
        "Debug.EnableAgentMemoryUsageCheck=y",
        "Debug.CgroupCheckPeriod=20",
        "Debug.AgentMemorySustainedCheckCount=2",
        "Debug.AgentMemoryRestartCooldown=0",
        "Debug.CgroupDisableOnProcessCheckFailure=n",
        "Debug.CgroupDisableOnQuotaCheckFailure=n",
        "Debug.CgroupLogMetrics=y"
    ])
    log.info("Agent configuration updated: %s", result)

    found: bool = retry_if_false(
        lambda: check_log_message("Agent cgroups enabled: True", after_timestamp=check_time))
    if not found:
        fail("Agent cgroups not enabled after configuration change")


def verify_agent_detects_memory_breach():
    """
    Verify that the agent detects the sustained anon memory breach and logs the expected messages.
    We look for:
      1. The sustained breach log: "Agent anon memory usage .* exceeds limit .* Sustained breach count:"
      2. The memory exceeded message: "The agent anon memory limit .* bytes exceeded"
    """
    log.info("** Verifying agent detects sustained anon memory breach")

    breach_messages = []
    exceeded_messages = []

    def check_for_breach_detection() -> bool:
        breach_messages.clear()
        exceeded_messages.clear()
        for record in AgentLog().read():
            if re.search(r"Agent anon memory usage \d+ bytes exceeds limit \d+ bytes.*Sustained breach count:", record.message):
                breach_messages.append(record.message)
            if re.search(r"The agent anon memory limit \d+ bytes exceeded", record.message):
                exceeded_messages.append(record.message)
        return len(breach_messages) >= 1 and len(exceeded_messages) >= 1

    # Allow up to 5 minutes for the agent to detect the breach.
    # With CgroupCheckPeriod=20s and SustainedCheckCount=2, it takes at least ~40s after convergence.
    # Plus initial delay of CHILD_LAUNCH_INTERVAL (5 min) on first attempt.
    found: bool = retry_if_false(check_for_breach_detection, attempts=15, delay=30)
    if found:
        log.info("Breach detection messages found: %s", breach_messages[:3])
        log.info("Memory exceeded messages found: %s", exceeded_messages[:3])
        log.info("Successfully verified agent detects sustained anon memory breach")
    else:
        fail("Agent did not detect the sustained memory breach. "
             "Breach messages: {0}, Exceeded messages: {1}".format(breach_messages, exceeded_messages))


def verify_agent_exits_on_memory_breach():
    """
    Verify that the agent exits due to memory limit breach by checking for the exit log message.
    """
    log.info("** Verifying agent exits on memory breach")

    def check_for_exit_message() -> bool:
        return check_log_message("is reached memory limit -- exiting")

    found: bool = retry_if_false(check_for_exit_message, attempts=10, delay=30)
    if found:
        log.info("Successfully verified agent exit message on memory breach")
    else:
        fail("Agent did not log the expected exit message for memory breach")


def verify_agent_restarted_after_exit():
    """
    Verify the agent service is still running after the exit (daemon should have restarted it).
    """
    log.info("** Verifying agent service is running after memory-triggered exit")

    def check_agent_running() -> bool:
        output = shellutil.run_command(["systemctl", "is-active", AGENT_SERVICE_NAME])
        return output.strip() == "active"

    found: bool = retry_if_false(check_agent_running, attempts=10, delay=15)
    if found:
        log.info("Successfully verified agent service is running after memory-triggered restart")
    else:
        fail("Agent service is not running after memory-triggered exit. "
             "Status: {0}".format(shellutil.run_command(["systemctl", "status", AGENT_SERVICE_NAME])))


def verify_restart_timestamp_persisted():
    """
    Verify the memory restart timestamp was persisted to disk with the version-based format.
    """
    log.info("** Verifying memory restart timestamp was persisted")
    timestamps_file = os.path.join(conf.get_lib_dir(), "memory_restart_timestamps")

    def check_timestamps_file() -> bool:
        if not os.path.exists(timestamps_file):
            return False
        try:
            with open(timestamps_file, "r") as f:
                data = json.loads(f.read())
            return (isinstance(data, dict) and "version" in data and "timestamps" in data
                    and isinstance(data["timestamps"], list) and len(data["timestamps"]) >= 1)
        except Exception:
            return False

    found: bool = retry_if_false(check_timestamps_file)
    if found:
        with open(timestamps_file, "r") as f:
            data = json.loads(f.read())
        log.info("Memory restart data: version=%s, timestamps=%s (count: %d)",
                 data.get("version"), data.get("timestamps"), len(data.get("timestamps", [])))
        log.info("Successfully verified memory restart timestamp was persisted")
    else:
        fail("Memory restart timestamps file was not created or has unexpected format: {0}".format(timestamps_file))


def cleanup_test_setup():
    """
    Restore agent configuration to defaults.
    """
    log.info("** Cleaning up test setup - restoring default agent configuration")
    cleanup_restart_timestamps()

    check_time = datetime.datetime.now(UTC)
    shellutil.run_command([
        "update-waagent-conf",
        "Debug.EnableAgentMemoryUsageCheck=n"
    ])

    # Wait for agent to restart with the new config
    time.sleep(10)
    found: bool = retry_if_false(
        lambda: check_log_message("Agent cgroups enabled: True", after_timestamp=check_time))
    if not found:
        log.warning("Agent cgroups may not have re-enabled after cleanup")


def main():
    skip_if_not_cgroupv2()
    prepare_agent()
    verify_agent_detects_memory_breach()
    verify_agent_exits_on_memory_breach()
    verify_agent_restarted_after_exit()
    verify_restart_timestamp_persisted()
    cleanup_test_setup()


run_remote_test(main)
