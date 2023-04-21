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
from tests_e2e.tests.lib.agent_test import AgentTest
from tests_e2e.tests.lib.logging import log


class AgentPublishTest(AgentTest):

    def run(self):
        self._get_agent_info()
        self._prepare_agent()
        self._check_update()
        self._get_agent_info()
        self._check_cse()

    def _get_agent_info(self) -> None:

    def _prepare_agent(self) -> None:
        output = self._ssh_client.run_command("agent-publish-config", use_sudo=True)
        log.info('Updating agent update required config \n%s', output)

    def _check_update(self) -> None:
        output = self._ssh_client.run_command("update_check")
        log.info('Check for agent update \n%s', output)

    def _check_cse(self) -> None:

if __name__ == "__main__":
    AgentPublishTest.run_from_command_line()
