# Microsoft Azure Linux Agent
#
# Copyright 2020 Microsoft Corporation
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


class ThreadHandlerInterface(object):
    """
    Interface for all thread handlers created and maintained by the GuestAgent.

    Each handler creates a background thread that runs a daemon loop. To support graceful shutdown, each handler
    uses a threading.Event (_stop_event) to make sleep operations interruptible:

    - Without the event, threads use time.sleep() between loop iterations. time.sleep() is uninterruptible -- if a
      thread is sleeping for several minutes, calling stop() would have to wait that entire duration before the
      thread wakes up and checks its stop flag.

    - With the event, threads use _stop_event.wait(timeout) instead of time.sleep(timeout). This behaves identically
      to time.sleep() during normal operation (blocks for the specified duration), but when stop() is called, it sets
      the event via _stop_event.set(), which immediately wakes up any thread waiting on it. The thread then checks
      its stop flag, sees that it should exit, and terminates promptly.

    The stop() method follows this sequence:
      1. Set the should_run/stopped flag so the daemon loop exits on its next check.
      2. Set the _stop_event to wake up the thread if it is sleeping.
      3. Call join(timeout=_THREAD_JOIN_TIMEOUT) to wait for the thread to finish its current operation and exit.

    This approach allows threads to complete their in-progress work (e.g. sending telemetry, processing events)
    before exiting, while ensuring they wake up promptly instead of sleeping through the shutdown window.
    """

    # Default timeout to wait for threads to stop during shutdown
    _THREAD_JOIN_TIMEOUT = 5

    @staticmethod
    def get_thread_name():
        raise NotImplementedError("get_thread_name() not implemented")

    def run(self):
        raise NotImplementedError("run() not implemented")

    def keep_alive(self):
        """
        Returns true if the thread handler should be restarted when the thread dies
        and false when it should remain dead.
        
        Defaults to True and can be overridden by sub-classes.
        """
        return True

    def is_alive(self):
        raise NotImplementedError("is_alive() not implemented")

    def start(self):
        raise NotImplementedError("start() not implemented")

    def stop(self):
        raise NotImplementedError("stop() not implemented")