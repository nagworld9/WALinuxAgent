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
# BVT for the agent update scenario
#
import json
import time

import requests
from assertpy import assert_that
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute.models import VirtualMachine

from tests_e2e.scenarios.lib.agent_test import AgentTest
from tests_e2e.scenarios.lib.agent_test_context import AgentTestContext
from tests_e2e.scenarios.lib.logging import log
from tests_e2e.scenarios.lib.ssh_client import SshClient
from tests_e2e.scenarios.lib.virtual_machine import VmMachine


class AgentUpdateBvt(AgentTest):

    def __init__(self, context: AgentTestContext):
        super().__init__(context)
        self._ssh_client = None

    def run(self):
        self._ssh_client = SshClient(
            ip_address=self._context.vm_ip_address,
            username=self._context.username,
            private_key_file=self._context.private_key_file)

        self._prepare_agent()

        # Verify downgrade scenario
        self._prepare_rsm_update()
        self._verify_guest_agent_update()
        #
        # # Verify upgrade scenario
        # self._prepare_rsm_update()
        # self._verify_guest_agent_update()

    def _prepare_agent(self):
        self._ssh_client.run_command("sudo sed -i 's/GAUpdates.Enabled=n/GAUpdates.Enabled=y/g' /etc/waagent.conf")
        self._ssh_client.run_command("sudo sed -i '$a AutoUpdate.GAFamily=Test' /etc/waagent.conf")
        local_path = self._context.test_source_directory/"scenarios"/"scripts"/"service-name"
        remote_path = self._context.remote_working_directory/"service-name"
        self._ssh_client.copy(local_path, remote_path)
        service_name = self._ssh_client.run_command(f"sudo {remote_path}").rstrip()
        self._ssh_client.restart_service(service_name)

    @staticmethod
    def _verify_agent_update_flag_enabled(vm: VmMachine) -> bool:
        result: VirtualMachine = vm.get()
        flag: bool = result.os_profile.linux_configuration.enable_vm_agent_platform_updates
        if flag is None:
            return False
        return flag

    def _enable_agent_update_flag(self, vm: VmMachine):
        osprofile = {
            "location": self._context.vm.location,  # location is required field
            "properties": {
                "osProfile": {
                    "linuxConfiguration": {
                        "enableVMAgentPlatformUpdates": True
                    }
                }
            }
        }
        vm.create_or_update(osprofile)

    def _prepare_rsm_update(self, requested_version="1.2.0.0"):
        vm = VmMachine(self._context.vm)
        if not self._verify_agent_update_flag_enabled(vm):
            # enable the flag
            self._enable_agent_update_flag(vm)

        credential = DefaultAzureCredential()
        token = credential.get_token("https://management.azure.com/.default")
        headers = {'Authorization': 'Bearer ' + token.token, 'Content-Type': 'application/json'}
        url = "https://management.azure.com/subscriptions/{0}/resourceGroups/{1}/providers/Microsoft.Compute/virtualMachines/{2}/" \
              "UpgradeVMAgent?api-version=2022-08-01".format(self._context.vm.subscription, self._context.vm.resource_group, self._context.vm.name)
        data = {
            "target": "Microsoft.OSTCLinuxAgent.Test",
            "targetVersion": requested_version
        }

        response = requests.post(url, data=json.dumps(data), headers=headers)
        if response.status_code == 202:
            log.info("RSM upgrade request accepted")
        else:
            raise Exception("Error occurred while RSM upgrade request. Status code : {0} and msg: {1}".format(response.status_code, response.content))

    def _verify_guest_agent_update(self, requested_version="1.1.0.0"):
        # Allow agent to update to requested version
        time.sleep(120)
        stdout = self._ssh_client.run_command("waagent -version")
        assert_that(stdout).described_as("Guest agent didn't update to requested version {0} but found {1}".format(requested_version, stdout))\
            .contains(requested_version)


if __name__ == "__main__":
    AgentUpdateBvt.run_from_command_line()
