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
import json
import os
from datetime import datetime, timedelta

from azurelinuxagent.ga import state_dir

import azurelinuxagent.common.logger as logger
from azurelinuxagent.common.future import UTC, ustr
from azurelinuxagent.common.utils import timeutil
from azurelinuxagent.common.utils.flexible_version import FlexibleVersion


HISTORY_FILE_NAME = "agent_memory_restart_history.json"
SCHEMA_VERSION = 1
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # matches timeutil.create_utc_timestamp
# Retain restart history only for the N most-recently-active agent versions;
# older versions are pruned on every save so the file cannot grow without bound.
MAX_VERSIONS_RETAINED = 3


class RestartHistory(object):
    """
    Load/append/save restart records. Not thread-safe; intended to be used from
    the ext-handler main thread only.

    File schema::

        {
          "schema_version": 1,
          "versions": {
            "<version>": [
              {"timestamp": "<iso8601 UTC>", "anon_bytes": <int>}
            ]
          }
        }
    """

    def __init__(self, path=None):
        self._path = path if path is not None else os.path.join(state_dir.get_state_dir(), HISTORY_FILE_NAME)
        self._data = {"schema_version": SCHEMA_VERSION, "versions": {}}
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("versions"), dict):
                self._data = {
                    "schema_version": data.get("schema_version", SCHEMA_VERSION),
                    "versions": data["versions"],
                }
        except Exception as e:
            logger.warn("Corrupt agent memory restart history at {0}: {1}. Starting fresh.", self._path, ustr(e))
            try:
                os.rename(self._path, self._path + ".corrupt")
            except Exception:
                pass

    def _save(self):
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f)
            if hasattr(os, "replace"):
                os.replace(tmp, self._path)
            else:
                os.rename(tmp, self._path)
        except Exception as e:
            logger.warn("Failed to persist agent memory restart history: {0}", ustr(e))

    def restarts_for(self, version):
        return list(self._data.get("versions", {}).get(str(version), []))

    def last_restart_time(self, version):
        entries = self.restarts_for(version)
        if not entries:
            return None
        # The stored format is fixed-width zero-padded UTC ending in "Z",
        # so lexicographic order == chronological order.
        latest = max(e.get("timestamp", "") for e in entries)
        try:
            return datetime.strptime(latest, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
        except Exception:
            return None

    def can_restart(self, version, max_per_version, min_interval_seconds):
        """
        Returns (allowed: bool, reason: str).
        """
        entries = self.restarts_for(version)
        if len(entries) >= max_per_version:
            return False, "max restarts ({0}) reached for version {1}".format(max_per_version, version)
        last = self.last_restart_time(version)
        if last is not None:
            delta = datetime.now(UTC) - last
            if delta < timedelta(0):
                # Clock skew: treat as zero elapsed time.
                delta = timedelta(0)
            min_interval = timedelta(seconds=min_interval_seconds)
            if delta < min_interval:
                return False, "last restart {0} ago is within min interval {1}".format(delta, min_interval)
        return True, "allowed"

    def record_restart(self, version, anon_bytes):
        entry = {
            "timestamp": timeutil.create_utc_timestamp(datetime.now(UTC)),
            "anon_bytes": int(anon_bytes),
        }
        versions = self._data.setdefault("versions", {})
        versions.setdefault(str(version), []).append(entry)
        self._prune_versions()
        self._save()

    def _prune_versions(self):
        """
        Keep only the MAX_VERSIONS_RETAINED newest agent versions; drop the
        rest.
        """
        versions = self._data.get("versions", {})
        if len(versions) <= MAX_VERSIONS_RETAINED:
            return

        def _version_key(item):
            key, _ = item
            try:
                # (1, ver) sorts ahead of (0, ...) under reverse=True, so
                # parseable versions are always preferred over unparseable ones.
                return (1, FlexibleVersion(key))
            except Exception:
                return (0, key)

        # Sort descending by parsed version and keep the top N.
        ranked = sorted(versions.items(), key=_version_key, reverse=True)
        self._data["versions"] = dict(ranked[:MAX_VERSIONS_RETAINED])

