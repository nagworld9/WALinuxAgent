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
import datetime
import re
import sys

from assertpy import fail

from azurelinuxagent.common.future import UTC
from azurelinuxagent.common.utils import shellutil
from tests_e2e.tests.lib.agent_log import AgentLog
from tests_e2e.tests.lib.cgroup_helpers import check_log_message, get_agent_memory_quota, using_cgroupv2

from tests_e2e.tests.lib.logging import log
from tests_e2e.tests.lib.remote_test import run_remote_test
from tests_e2e.tests.lib.retry import retry_if_false


def skip_if_distro_not_supports_cgroupv2():
    if not using_cgroupv2():
        log.info("Skipping memory quota test as the distro is not using cgroupv2")
        sys.exit(0)


def prepare_agent():
    check_time = datetime.datetime.now(UTC)
    log.info("Executing script update-waagent-conf to enable agent cgroups config flag")
    result = shellutil.run_command(["update-waagent-conf", "Debug.CgroupCheckPeriod=30", "Debug.CgroupLogMetrics=y",
                                    "Debug.CgroupDisableOnProcessCheckFailure=n",
                                    "Debug.CgroupDisableOnQuotaCheckFailure=n"])
    log.info("Successfully enabled agent cgroups config flag: {0}".format(result))

    found: bool = retry_if_false(
        lambda: check_log_message("Agent cgroups enabled: True", after_timestamp=check_time))
    if not found:
        fail("Agent cgroups not enabled")


def verify_agent_memory_limit_not_set():
    """
    Verifies that the agent's cgroup does NOT have a memory.high limit set.
    The agent no longer sets MemoryHigh on its cgroup since it causes performance issues due to
    cache pressure from the kernel's reclaim behavior. Instead, the agent self-monitors anon memory usage.
    """
    log.info("** Verifying agent cgroup does NOT have memory.high limit set")

    def check_no_memory_quota() -> bool:
        quota = get_agent_memory_quota()
        return quota is None or quota == "infinity"

    found: bool = retry_if_false(check_no_memory_quota)
    if found:
        log.info("Agent Memory Quota (MemoryHigh): %s", get_agent_memory_quota())
        log.info("Successfully verified agent cgroup does NOT have memory.high limit set")
    else:
        fail("The agent's cgroup has an unexpected memory.high limit set. Agent Memory Quota: {0}".format(get_agent_memory_quota()))


def verify_agent_reported_memory_metrics():
    """
    This method verifies that the agent reports Memory Usage metrics (anon and cache)
    """
    log.info("** Verifying agent reported memory metrics")
    log.info("Parsing agent log for memory metrics")
    memory_usage = []

    def check_agent_log_for_metrics() -> bool:
        for record in AgentLog().read():
            # This regex matches "Memory Usage" with optional (B) and extracts the value
            match = re.search(r"Memory/.*Memory Usage(?: \(B\))?\s*\[walinuxagent\.service\]\s*=\s*([0-9.]+)", record.message)
            if match is not None:
                memory_usage.append(match.group(1))
        if len(memory_usage) < 1:
            return False
        return True

    found: bool = retry_if_false(check_agent_log_for_metrics)
    if found:
        log.info("Memory Usage: %s", memory_usage)
        log.info("Successfully verified agent reported memory usage metrics")
    else:
        fail(
            "The agent doesn't seem to be collecting Memory Usage metrics. Agent found Memory Usage: {0}".format(
                memory_usage))


def main():
    skip_if_distro_not_supports_cgroupv2()
    prepare_agent()
    verify_agent_memory_limit_not_set()
    verify_agent_reported_memory_metrics()


run_remote_test(main)
