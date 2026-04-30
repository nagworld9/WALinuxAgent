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

import threading


class ThreadHandlerInterface(object):
    """
    Interface and shared base for all thread handlers.

    Each handler creates a background thread that runs a daemon loop. To support graceful shutdown, this base class
    provides a shared threading.Event (self._stop_event) that makes sleep operations in the daemon loop interruptible:

    - Without the event, threads use time.sleep() between loop iterations. time.sleep() is uninterruptible -- if a
      thread is sleeping for several minutes, calling stop() would have to wait that entire duration before the
      thread wakes up and checks its stop flag.

    - With the event, threads call self._interruptible_sleep(timeout) (which delegates to self._stop_event.wait).
      This behaves identically to time.sleep() during normal operation (blocks for the specified duration), but when
      stop() is called, self._signal_stop() sets the event, which immediately wakes up any thread waiting on it. The
      thread then checks its stop flag, sees that it should exit, and terminates promptly.

    The stop() method in subclasses should follow this sequence:
      1. Set the should_run/stopped flag so the daemon loop exits on its next check.
      2. Call self._signal_stop() to wake up the thread if it is sleeping.
      3. Call join(timeout=_THREAD_JOIN_TIMEOUT) to wait for the thread to finish its current operation and exit.

    Subclasses that restart the thread should call self._reset_stop_event() in start() so the new thread does not
    inherit a "stopped" state from a previous run.

    This approach allows threads to complete their in-progress work (e.g. sending telemetry, processing events)
    before exiting, while ensuring they wake up promptly instead of sleeping through the shutdown window.
    """

    # Default timeout in seconds to wait for threads to stop during shutdown
    _THREAD_JOIN_TIMEOUT = 5

    def __init__(self):
        # Event used to interrupt the daemon loop's sleep when stop() is called. Centralized here so individual
        # handlers do not have to re-implement the same synchronization boilerplate.
        self._stop_event = threading.Event()

    def _interruptible_sleep(self, timeout):
        """
        Sleeps for the given timeout (in seconds), but returns early if stop() has been signaled.
        """
        self._stop_event.wait(timeout)

    def _signal_stop(self):
        """
        Wakes up the daemon loop if it is currently sleeping in _interruptible_sleep(). Should be called from stop().
        """
        self._stop_event.set()

    def _reset_stop_event(self):
        """
        Clears the stop signal so the thread can be (re)started cleanly. Should be called from start() before
        launching a new thread.
        """
        self._stop_event.clear()

    def _is_stop_signaled(self):
        """
        Returns True once stop() has signaled the thread to exit. Daemon loops can use this to bail out of long
        operation sequences instead of waiting for the next outer-loop check.
        """
        return self._stop_event.is_set()

    def _run_periodic_operations(self, periodic_operations):
        """
        Runs each operation in the given list, but stops early if a shutdown has been signaled. This avoids
        starting a new periodic operation once stop() has been called.
        """
        for op in periodic_operations:
            if self._is_stop_signaled():
                break
            op.run()

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

    def signal_stop(self):
        """
        Signals the thread to stop without waiting for it to exit. This allows callers to broadcast a stop
        signal to several handlers in parallel and only join afterwards, rather than serializing the join
        timeouts inside each handler's stop().
        """
        raise NotImplementedError("signal_stop() not implemented")
