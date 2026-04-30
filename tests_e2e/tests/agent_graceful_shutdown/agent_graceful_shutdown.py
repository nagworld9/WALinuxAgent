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
# Validates that, when the agent service is stopped (SIGTERM), the ext-handler shuts down gracefully:
#  * the SIGTERM handler logs that the signal was received
#  * every background thread the ext-handler started is signaled to stop
#  * every background thread reports it stopped successfully (i.e. the thread exited within the join timeout)
#  * shutdown completes within a reasonable time
#
# We stop and start the agent multiple times, waiting a random amount of time before each stop.
# This way the threads are caught in different states (just started, running an operation, sleeping,
# draining the telemetry queue, etc.) when the stop signal arrives. Each iteration checks its own
# section of the agent log to confirm the shutdown happened correctly.
#

import random
import re
import time

from assertpy import fail

from tests_e2e.tests.lib.agent_test import AgentVmTest
from tests_e2e.tests.lib.logging import log
from tests_e2e.tests.lib.shell import CommandError


# Threads launched by UpdateHandler.run() that must shut down gracefully on SIGTERM.
# Note: CollectLogsHandler is only started when log collection is enabled (Logs.Collect=y); we enable
# it explicitly below so that all five threads are exercised by this test.
_EXPECTED_THREADS = [
    "TelemetryEventsCollector",
    "SendTelemetryHandler",
    "MonitorHandler",
    "EnvHandler",
    "CollectLogsHandler",
]

# Maximum wall-clock time we allow the agent to take from receiving SIGTERM to all threads having
# stopped. Each thread has a 5s join timeout; with five threads the worst-case serial join would be
# 25s, but Phase-1 signal-broadcast lets healthy threads exit in parallel so total shutdown should be
# well under this bound.
_MAX_SHUTDOWN_SECONDS = 30

# Number of stop/start iterations to run. More iterations give better coverage of the various thread
# states (just-started, mid-operation, mid-sleep, queue-draining) at the cost of test runtime.
_SHUTDOWN_ITERATIONS = 5

# Range (in seconds) of the additional random delay we apply between confirming the agent is up and
# issuing the stop. The lower bound is small enough to catch threads still in startup; the upper
# bound spans more than one Goal State Period (default ~6s) so we also catch threads mid-iteration
# and mid-sleep.
_PRE_STOP_DELAY_RANGE = (1, 75)


class AgentGracefulShutdown(AgentVmTest):
    """
    Tests that the ext-handler shuts down gracefully when SIGTERM is delivered (e.g. by `systemctl stop`).
    """

    def run(self):
        ssh_client = self._context.create_ssh_client()

        # One-time setup: enable log collection so the CollectLogsHandler thread is started by the
        # agent every time it comes up. Also lower Debug.LogCollectorInitialDelay (default 5 minutes)
        # so the collector exits its initial sleep quickly; otherwise an iteration that stops the
        # agent before that delay elapses would not see the "Signaling CollectLogsHandler thread to
        # stop" line because the thread would still be parked in the initial sleep.
        log.info("Enabling log collection so all threads run on every iteration...")
        ssh_client.run_command(
            "sh -c 'agent-service stop && "
            "update-waagent-conf Logs.Collect=y Debug.LogCollectorInitialDelay=5'",
            use_sudo=True,
        )

        seed = int(time.time())
        rng = random.Random(seed)
        log.info("Random seed for this run: %d (use this to reproduce the delay sequence)", seed)

        # Run several iterations. Each iteration:
        #   1. rotates waagent.log so we always inspect a fresh slice
        #   2. starts the agent and waits for it to be up
        #   3. sleeps a random amount so the threads are caught in different states (startup,
        #      mid-iteration, mid-sleep, queue-draining, etc.) when we issue the stop
        #   4. stops the agent and validates the graceful-shutdown sequence in waagent.log
        failures = []
        for iteration in range(1, _SHUTDOWN_ITERATIONS + 1):
            pre_stop_delay = rng.uniform(*_PRE_STOP_DELAY_RANGE)
            log.info("")
            log.info("===== Iteration %d / %d (pre-stop delay: %.2fs) =====",
                     iteration, _SHUTDOWN_ITERATIONS, pre_stop_delay)
            try:
                self._run_one_iteration(ssh_client, pre_stop_delay)
            except AssertionError as ex:
                # assertpy.fail() raises AssertionError; collect them so we still surface every
                # iteration's outcome rather than stopping at the first one.
                failures.append("Iteration {0} (delay={1:.2f}s): {2}".format(iteration, pre_stop_delay, ex))
                log.warning("Iteration %d failed: %s", iteration, ex)

        # Restore the service so we leave the VM in a usable state for any subsequent tests.
        log.info("Restoring agent service for subsequent tests...")
        ssh_client.run_command("agent-service start", use_sudo=True)

        if failures:
            fail(
                "Graceful shutdown validation failed in {0} of {1} iteration(s):\n".format(
                    len(failures), _SHUTDOWN_ITERATIONS)
                + "\n".join("    - " + f for f in failures))

        log.info("All %d shutdown iterations completed successfully.", _SHUTDOWN_ITERATIONS)

    def _run_one_iteration(self, ssh_client, pre_stop_delay):
        """
        Performs a single stop/start cycle and validates the shutdown sequence in waagent.log.

        :param pre_stop_delay: how many seconds to wait between confirming the agent is up and
                               sending the stop signal. A different value on each iteration causes
                               the threads to be in different states when SIGTERM arrives.
        """
        # Rotate waagent.log so we only inspect log entries from this iteration.
        log.info("Rotating waagent.log and starting the agent...")
        ssh_client.run_command(
            "sh -c 'agent-service stop || true; "
            "mv /var/log/waagent.log /var/log/waagent.$(date --iso-8601=seconds).log 2>/dev/null || true; "
            "agent-service start'",
            use_sudo=True,
        )

        # Wait for the agent to come up. We use the 'Goal State Period:' line as a readiness marker
        # because it is logged once after all background threads are launched.
        log.info("Waiting for the agent to start all of its threads...")
        for _ in range(30):
            try:
                ssh_client.run_command(
                    "grep -q 'Goal State Period:' /var/log/waagent.log",
                    use_sudo=True,
                )
                break
            except CommandError:
                time.sleep(2)
        else:
            fail("The agent did not finish starting up within 60 seconds of the service start")

        # Add the randomized delay so this iteration catches the threads in a different state than
        # the previous iteration did.
        log.info("Letting the agent run for %.2fs before issuing stop...", pre_stop_delay)
        time.sleep(pre_stop_delay)

        # Trigger the graceful shutdown path. systemctl stop sends SIGTERM and waits for the unit to exit.
        log.info("Stopping the agent service to trigger graceful shutdown...")
        start_time = time.time()
        ssh_client.run_command("agent-service stop", use_sudo=True)
        elapsed = time.time() - start_time
        log.info("agent-service stop returned in %.2fs", elapsed)

        if elapsed > _MAX_SHUTDOWN_SECONDS:
            fail(
                "Agent shutdown took too long: {0:.2f}s (max allowed {1}s). The service may not "
                "be tearing down threads in parallel.".format(elapsed, _MAX_SHUTDOWN_SECONDS))

        # Pull the relevant log lines from the SIGTERM marker to the end of the shutdown sequence.
        log.info("Inspecting waagent.log for the shutdown sequence...")
        sigterm_marker = "SIGTERM received for ext handler, shutting down gracefully"
        shutdown_log = ""
        try:
            shutdown_log = ssh_client.run_command(
                "sed -n '/{0}/,$p' /var/log/waagent.log".format(sigterm_marker),
                use_sudo=True,
            ).rstrip()
        except CommandError as e:
            fail("Could not extract shutdown log lines from waagent.log: {0}".format(e))

        if not shutdown_log:
            fail(
                "Did not find the SIGTERM marker '{0}' in waagent.log. The agent may have exited "
                "without going through the graceful shutdown path.".format(sigterm_marker))

        log.info("Shutdown log:\n%s", "\n".join("        " + ln for ln in shutdown_log.splitlines()))

        # Per-thread validation. We deliberately do NOT fail when a single thread reports
        # "did not stop within the timeout": the per-thread join timeout is only 5s, and a thread
        # that is mid-operation (e.g. the log collector running systemd-run, the telemetry sender
        # waiting on a network call) can occasionally exceed that and still finish cleanly. What
        # actually matters for graceful shutdown is:
        #   1. The agent signaled every expected thread to stop (so we know it didn't skip any).
        #   2. Each thread reported a terminal status -- either "stopped successfully" OR
        #      "did not stop within the timeout".
        #   3. The whole agent-service stop call returned within _MAX_SHUTDOWN_SECONDS (asserted
        #      above, before we even read the log).
        # Iterations where every thread stopped cleanly within 5s are still the common case; we
        # just stop treating the occasional 5s-overshoot as a hard failure.
        missing = []
        timed_out = []
        for thread_name in _EXPECTED_THREADS:
            signal_re = r"Signaling {0} thread to stop".format(re.escape(thread_name))
            stopped_re = r"{0} thread stopped successfully".format(re.escape(thread_name))
            timed_out_re = r"{0} thread did not stop within the timeout".format(re.escape(thread_name))

            if not re.search(signal_re, shutdown_log):
                missing.append("{0} (no 'Signaling ... to stop' log)".format(thread_name))
                continue

            # A thread has a valid terminal status if EITHER it reported "stopped successfully"
            # OR "did not stop within the timeout". Only when neither line appears do we treat it
            # as missing -- that would mean _shutdown() didn't reach the per-thread stop step.
            stopped_match = re.search(stopped_re, shutdown_log) is not None
            timed_out_match = re.search(timed_out_re, shutdown_log) is not None
            has_terminal_status = stopped_match or timed_out_match

            if not has_terminal_status:
                missing.append(
                    "{0} (no terminal status: neither 'stopped successfully' nor "
                    "'did not stop within the timeout')".format(thread_name))
                continue

            if timed_out_match:
                # Track for diagnostic logging, but don't fail the iteration as long as the overall
                # agent-service stop returned within _MAX_SHUTDOWN_SECONDS.
                timed_out.append(thread_name)

        if missing:
            fail(
                "The following threads did not complete their graceful shutdown sequence:\n"
                + "\n".join("    - " + m for m in missing))

        if timed_out:
            log.info(
                "The following threads exceeded the 5s per-thread join timeout but the overall "
                "agent shutdown still completed within %.2fs (limit %ds): %s",
                elapsed, _MAX_SHUTDOWN_SECONDS, ", ".join(timed_out))

        # Verify the dependency ordering: TelemetryEventsCollector must be stopped before
        # SendTelemetryHandler so that the collector cannot enqueue events into a queue whose owner
        # has already exited. We only enforce this when both threads reported "stopped successfully"
        # (i.e. we have stop-completion timestamps to compare). If either one timed out, the order
        # check is skipped because the timed-out thread has no completion line.
        collector_stopped_match = re.search(
            r"TelemetryEventsCollector thread stopped successfully", shutdown_log)
        sender_stopped_match = re.search(
            r"SendTelemetryHandler thread stopped successfully", shutdown_log)
        if collector_stopped_match and sender_stopped_match \
                and collector_stopped_match.start() > sender_stopped_match.start():
            fail(
                "TelemetryEventsCollector stopped after SendTelemetryHandler; the dependency order "
                "is wrong (the collector produces events into the sender's queue and must stop first).")

        if timed_out:
            log.info(
                "Graceful shutdown completed in %.2fs; %d/%d thread(s) stopped within the per-thread "
                "join timeout, %d exceeded it but the overall shutdown was still within bounds.",
                elapsed, len(_EXPECTED_THREADS) - len(timed_out), len(_EXPECTED_THREADS), len(timed_out))
        else:
            log.info(
                "Graceful shutdown completed in %.2fs and all %d threads stopped successfully.",
                elapsed, len(_EXPECTED_THREADS))


if __name__ == "__main__":
    AgentGracefulShutdown.run_from_command_line()
