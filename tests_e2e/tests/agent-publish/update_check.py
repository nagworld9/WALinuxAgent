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
import re
import sys

from tests_e2e.tests.lib.agent_log import AgentLog

"""
Post the _LOG_PATTERN_00 changes, the last group sometimes might not have the "Agent " part at the start of the sentence; thus making it optional.

> WALinuxAgent-2.2.18 discovered WALinuxAgent-2.2.47 as an update and will exit
(None, 'WALinuxAgent-2.2.18', '2.2.47')
"""
_UPDATE_PATTERN_00 = re.compile(r'(.*Agent\s)?(\S*)\sdiscovered\sWALinuxAgent-(\S*)\sas an update and will exit')

"""
> Agent WALinuxAgent-2.2.45 discovered update WALinuxAgent-2.2.47 -- exiting
('Agent', 'WALinuxAgent-2.2.45', '2.2.47')
"""
_UPDATE_PATTERN_01 = re.compile(r'(.*Agent)?\s(\S*) discovered update WALinuxAgent-(\S*) -- exiting')

"""
> Normal Agent upgrade discovered, updating to WALinuxAgent-2.9.1.0 -- exiting
('Normal Agent', None, 'WALinuxAgent-2.9.1.0 ')
"""
_UPDATE_PATTERN_02 = re.compile(r'(.*Agent)?\s(\S*) upgrade discovered, updating to WALinuxAgent-(\S*) -- exiting')

"""
> Agent WALinuxAgent-2.2.47 is running as the goal state agent
('2.2.47',)
"""
_RUNNING_PATTERN_00 = re.compile(r'.*Agent\sWALinuxAgent-(\S*)\sis running as the goal state agent')


def main():

    exit_code = 0
    verbose = False
    detected_update = False
    update_successful = False
    update_version = ''
    try:
        log = AgentLog()

        for record in log.read():
            if 'TelemetryData' in record.text:
                continue

            for p in [_UPDATE_PATTERN_00, _UPDATE_PATTERN_01, _UPDATE_PATTERN_02]:
                update_match = re.match(p, record.text)
                if update_match:
                    detected_update = True
                    update_version = update_match.groups()[2]

            if detected_update:
                running_match = re.match(_RUNNING_PATTERN_00, record.text)
                if running_match and update_version == running_match.groups()[0]:
                    update_successful = True

            if 'VERBOSE' in record.text:
                verbose = True

    except Exception as e:
        print(e)
        sys.exit(1)

    if detected_update:
        print('update was detected: {0}'.format(update_version))
        if update_successful:
            print('version {0} was started successfully'.format(update_version))
        else:
            print('error - version {0} was not started'.format(update_version))
            exit_code += 1
    else:
        print('warning - no update detected')
        exit_code += 1

    if not verbose:
        print('verbose logs not found')
        exit_code += 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
