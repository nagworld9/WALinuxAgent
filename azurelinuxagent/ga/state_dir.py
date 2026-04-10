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
# Requires Python 2.6+ and Openssl 1.0+
#

import os

import azurelinuxagent.common.conf as conf
from azurelinuxagent.common.utils import fileutil


def get_state_dir():
    return os.path.join(conf.get_lib_dir(), "state")


def initialize_state_dir():
    """Create the state directory if it does not exist. Called from both the daemon and the extension handler."""
    state_dir = get_state_dir()
    if not os.path.isdir(state_dir):
        fileutil.mkdir(state_dir, mode=0o700)
