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

from tests_e2e.tests.lib.agent_test import AgentVmTest
from tests_e2e.tests.lib.agent_test_context import AgentVmTestContext
from tests_e2e.tests.lib.logging import log
from tests_e2e.tests.lib.ssh_client import SshClient


class AgentPersistFirewallDeprovisionTest(AgentVmTest):
    """

    """
    def __init__(self, context: AgentVmTestContext):
        super().__init__(context)
        self._ssh_client: SshClient = self._context.create_ssh_client()

    def run(self):
        # Test case: Ensure custom image created by old agent not messing the new agent persist firewall setup
        self._ssh_client.run_command("waagent-deprovision", use_sudo=True)
        self._ssh_client.run_command("agent-service restart", use_sudo=True)
        self._verify_persist_firewall_service_running()

    def _verify_persist_firewall_service_running(self):
        log.info("Verifying persist firewall service is running")
        self._run_remote_test(self._ssh_client, "agent_persist_firewall-verify_persist_firewall_service_running.py",
                              use_sudo=True)
        log.info("Successfully verified persist firewall service is running")

