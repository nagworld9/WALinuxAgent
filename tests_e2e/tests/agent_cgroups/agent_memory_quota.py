from typing import List, Dict, Any

from tests_e2e.tests.lib.agent_test import AgentVmTest
from tests_e2e.tests.lib.agent_test_context import AgentVmTestContext
from tests_e2e.tests.lib.logging import log


class AgentMemoryQuota(AgentVmTest):
    """
    The test verifies that the agent does NOT set a memory.high cgroup limit (since it causes performance issues due to
    cache pressure). Instead, the agent self-monitors anon memory usage. This test also verifies that memory metrics
    are still being reported.

    Additionally, it validates the memory limit breach detection by configuring the agent with a low anon memory limit
    and verifying that the agent detects the sustained breach, exits, and is restarted by the daemon.
    """
    def __init__(self, context: AgentVmTestContext):
        super().__init__(context)
        self._ssh_client = self._context.create_ssh_client()

    def run(self):
        log.info("=====Validating agent memory self-monitoring (no cgroup memory limit)")
        self._run_remote_test(self._ssh_client, "agent_memory_quota-check_agent_memory_quota.py", use_sudo=True)
        log.info("Successfully verified that agent is NOT using cgroup memory limit and memory metrics are reported")

        log.info("=====Validating agent memory limit breach detection and restart behavior")
        self._run_remote_test(self._ssh_client, "agent_memory_quota-check_memory_limit_breach.py", use_sudo=True)
        log.info("Successfully verified agent memory limit breach detection and restart behavior")

    def get_ignore_error_rules(self) -> List[Dict[str, Any]]:
        ignore_rules = [
            # The memory breach test intentionally triggers a memory limit exit, so these messages are expected
            #     Agent anon memory usage <N> bytes exceeds limit <N> bytes. Sustained breach count: ...
            {'message': r"Agent anon memory usage \d+ bytes exceeds limit \d+ bytes"},
            #     The agent anon memory limit <N> bytes exceeded...
            {'message': r"The agent anon memory limit \d+ bytes exceeded"},
            #     Check on agent memory usage:
            {'message': r"Check on agent memory usage"},
            #     Agent WALinuxAgent-x.x.x.x is reached memory limit -- exiting
            {'message': r"is reached memory limit -- exiting"},
        ]
        return ignore_rules


if __name__ == "__main__":
    AgentMemoryQuota.run_from_command_line()
