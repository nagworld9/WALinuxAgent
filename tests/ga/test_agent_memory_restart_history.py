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

import json
import os
from datetime import datetime, timedelta

from azurelinuxagent.common.future import UTC
from azurelinuxagent.common.utils import timeutil
from azurelinuxagent.ga.agent_memory_restart_history import (
    RestartHistory,
    HISTORY_FILE_NAME,
    SCHEMA_VERSION,
    MAX_VERSIONS_RETAINED,
)
from tests.lib.tools import AgentTestCase


VERSION = "2.13.1.1"
OTHER_VERSION = "2.13.1.2"


class RestartHistoryTestCase(AgentTestCase):
    """
    Unit tests for azurelinuxagent.ga.agent_memory_restart_history.RestartHistory.
    """

    def setUp(self):
        AgentTestCase.setUp(self)
        self.history_path = os.path.join(self.tmp_dir, HISTORY_FILE_NAME)

    def _new(self):
        return RestartHistory(path=self.history_path)

    @staticmethod
    def _ts(dt):
        return timeutil.create_utc_timestamp(dt)

    def _seed(self, entries_by_version, schema_version=SCHEMA_VERSION):
        """
        Write a history file with the given {version: [(datetime, anon_bytes), ...]}
        contents so we can exercise the load path.
        """
        versions = {}
        for ver, entries in entries_by_version.items():
            versions[ver] = [
                {"timestamp": self._ts(dt), "anon_bytes": anon}
                for dt, anon in entries
            ]
        with open(self.history_path, "w") as f:
            json.dump({"schema_version": schema_version, "versions": versions}, f)

    def test_missing_file_starts_empty(self):
        hist = self._new()
        self.assertEqual([], hist.restarts_for(VERSION))
        self.assertIsNone(hist.last_restart_time(VERSION))
        self.assertFalse(os.path.exists(self.history_path),
                         "Just constructing the object must not create the file")

    def test_load_reads_existing_entries(self):
        now = datetime.now(UTC)
        self._seed({VERSION: [(now - timedelta(days=1), 400 * 1024 * 1024)]})
        hist = self._new()
        entries = hist.restarts_for(VERSION)
        self.assertEqual(1, len(entries))
        self.assertEqual(400 * 1024 * 1024, entries[0]["anon_bytes"])

    def test_corrupt_file_is_backed_up_and_state_is_empty(self):
        with open(self.history_path, "w") as f:
            f.write("{not valid json")
        hist = self._new()
        self.assertEqual([], hist.restarts_for(VERSION),
                         "State must fall back to empty when the file is corrupt")
        self.assertTrue(os.path.exists(self.history_path + ".corrupt"),
                        "The corrupt file must be renamed with a .corrupt suffix")

    def test_load_ignores_file_with_unexpected_shape(self):
        # A JSON file that parses but does not match the schema is treated as empty.
        with open(self.history_path, "w") as f:
            json.dump(["not", "a", "dict"], f)
        hist = self._new()
        self.assertEqual([], hist.restarts_for(VERSION))

    def test_load_ignores_versions_key_of_wrong_type(self):
        with open(self.history_path, "w") as f:
            json.dump({"schema_version": SCHEMA_VERSION, "versions": ["oops"]}, f)
        hist = self._new()
        self.assertEqual([], hist.restarts_for(VERSION))

    def test_record_restart_creates_file_with_expected_schema(self):
        hist = self._new()
        hist.record_restart(VERSION, 314572800)

        self.assertTrue(os.path.exists(self.history_path))
        with open(self.history_path) as f:
            data = json.load(f)

        self.assertEqual(SCHEMA_VERSION, data["schema_version"])
        self.assertIn(VERSION, data["versions"])
        entry = data["versions"][VERSION][0]
        self.assertEqual(314572800, entry["anon_bytes"])
        # Must match timeutil.create_utc_timestamp format exactly: ends with 'Z'
        self.assertTrue(entry["timestamp"].endswith("Z"))
        # And be parseable back.
        parsed = datetime.strptime(entry["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertIsNotNone(parsed)

    def test_record_restart_appends_within_same_version(self):
        hist = self._new()
        hist.record_restart(VERSION, 1)
        hist.record_restart(VERSION, 2)
        entries = self._new().restarts_for(VERSION)
        self.assertEqual([1, 2], [e["anon_bytes"] for e in entries])

    def test_record_restart_isolates_versions(self):
        hist = self._new()
        hist.record_restart(VERSION, 1)
        hist.record_restart(OTHER_VERSION, 2)
        reloaded = self._new()
        self.assertEqual(1, len(reloaded.restarts_for(VERSION)))
        self.assertEqual(1, len(reloaded.restarts_for(OTHER_VERSION)))

    def test_record_restart_accepts_non_string_version(self):
        # RestartHistory keys by str(version) so callers can pass FlexibleVersion.
        hist = self._new()
        hist.record_restart(2, 1)
        self.assertEqual(1, len(self._new().restarts_for("2")))

    def test_record_restart_coerces_anon_bytes_to_int(self):
        hist = self._new()
        hist.record_restart(VERSION, 3.7)
        self.assertEqual(3, self._new().restarts_for(VERSION)[0]["anon_bytes"])

    def test_history_survives_across_instances(self):
        first = self._new()
        first.record_restart(VERSION, 500)
        second = self._new()
        self.assertEqual([500], [e["anon_bytes"] for e in second.restarts_for(VERSION)])

    def test_last_restart_time_none_when_no_entries(self):
        self.assertIsNone(self._new().last_restart_time(VERSION))

    def test_last_restart_time_returns_latest(self):
        old = datetime.now(UTC) - timedelta(days=10)
        mid = datetime.now(UTC) - timedelta(days=5)
        new = datetime.now(UTC) - timedelta(days=1)
        # Intentionally seed out of chronological order to verify max() logic.
        self._seed({VERSION: [(mid, 1), (old, 2), (new, 3)]})
        last = self._new().last_restart_time(VERSION)
        self.assertIsNotNone(last)
        # Compare at second precision to avoid microsecond flakes from round-trip.
        self.assertEqual(new.replace(microsecond=0), last.replace(microsecond=0))

    def test_last_restart_time_returns_none_when_timestamps_unparseable(self):
        # Directly write a file with a bogus timestamp string.
        with open(self.history_path, "w") as f:
            json.dump({"schema_version": SCHEMA_VERSION,
                       "versions": {VERSION: [{"timestamp": "not-a-time", "anon_bytes": 1}]}}, f)
        self.assertIsNone(self._new().last_restart_time(VERSION))

    def test_can_restart_allowed_when_history_empty(self):
        allowed, reason = self._new().can_restart(VERSION, max_per_version=5,
                                                  min_interval_seconds=3 * 86400)
        self.assertTrue(allowed)
        self.assertEqual("allowed", reason)

    def test_can_restart_blocked_when_max_per_version_reached(self):
        long_ago = datetime.now(UTC) - timedelta(days=30)
        self._seed({VERSION: [(long_ago, 1)] * 5})
        allowed, reason = self._new().can_restart(VERSION, max_per_version=5,
                                                  min_interval_seconds=3 * 86400)
        self.assertFalse(allowed)
        self.assertIn("max restarts", reason)

    def test_can_restart_blocked_within_min_interval(self):
        recent = datetime.now(UTC) - timedelta(hours=1)
        self._seed({VERSION: [(recent, 1)]})
        allowed, reason = self._new().can_restart(VERSION, max_per_version=5,
                                                  min_interval_seconds=3 * 86400)
        self.assertFalse(allowed)
        self.assertIn("within min interval", reason)

    def test_can_restart_allowed_after_min_interval(self):
        far_past = datetime.now(UTC) - timedelta(days=5)
        self._seed({VERSION: [(far_past, 1)]})
        allowed, _ = self._new().can_restart(VERSION, max_per_version=5,
                                             min_interval_seconds=3 * 86400)
        self.assertTrue(allowed)

    def test_can_restart_treats_negative_delta_as_zero(self):
        # A "future" timestamp (clock rolled back) must NOT bypass the min-interval.
        future = datetime.now(UTC) + timedelta(days=1)
        self._seed({VERSION: [(future, 1)]})
        allowed, reason = self._new().can_restart(VERSION, max_per_version=5,
                                                  min_interval_seconds=60)
        self.assertFalse(allowed,
                         "A future last-restart timestamp must not be treated as 'long ago'")
        self.assertIn("within min interval", reason)

    def test_can_restart_guardrails_are_per_version(self):
        # Fill up VERSION but leave OTHER_VERSION empty. OTHER_VERSION must still be allowed.
        long_ago = datetime.now(UTC) - timedelta(days=30)
        self._seed({VERSION: [(long_ago, 1)] * 5})
        allowed, _ = self._new().can_restart(OTHER_VERSION, max_per_version=5,
                                             min_interval_seconds=3 * 86400)
        self.assertTrue(allowed)

    def test_record_restart_prunes_to_max_versions_retained(self):
        # Pre-seed with more than MAX_VERSIONS_RETAINED versions.
        old_ts = datetime.now(UTC) - timedelta(days=100)
        seeded = {
            "2.10.0.0": [(old_ts, 1)],
            "2.11.0.0": [(old_ts, 1)],
            "2.12.0.0": [(old_ts, 1)],
            "2.13.0.0": [(old_ts, 1)],
        }
        self._seed(seeded)
        hist = self._new()
        # Recording a newer version should trigger pruning to the newest N.
        hist.record_restart("2.14.0.0", 42)

        kept = set(self._new()._data["versions"].keys())
        self.assertEqual(MAX_VERSIONS_RETAINED, len(kept),
                         "History must be pruned to exactly MAX_VERSIONS_RETAINED entries")
        # The N highest versions are the ones retained.
        self.assertEqual(set(["2.14.0.0", "2.13.0.0", "2.12.0.0"]), kept)

    def test_record_restart_does_not_prune_when_under_limit(self):
        old_ts = datetime.now(UTC) - timedelta(days=100)
        self._seed({"2.10.0.0": [(old_ts, 1)]})
        hist = self._new()
        hist.record_restart("2.11.0.0", 1)
        hist.record_restart("2.12.0.0", 1)
        kept = set(self._new()._data["versions"].keys())
        self.assertEqual(set(["2.10.0.0", "2.11.0.0", "2.12.0.0"]), kept,
                         "Nothing should be pruned when count <= MAX_VERSIONS_RETAINED")

    def test_prune_keeps_higher_versions_regardless_of_timestamp(self):
        # Older versions have very recent timestamps; newer versions have very
        # old timestamps. Pruning must still be driven by version, not by time.
        very_recent = datetime.now(UTC) - timedelta(seconds=1)
        very_old = datetime.now(UTC) - timedelta(days=365)
        self._seed({
            "2.10.0.0": [(very_recent, 1)],
            "2.11.0.0": [(very_recent, 1)],
            "2.14.0.0": [(very_old, 1)],
            "2.15.0.0": [(very_old, 1)],
        })
        hist = self._new()
        hist.record_restart("2.16.0.0", 1)
        kept = set(self._new()._data["versions"].keys())
        self.assertEqual(set(["2.16.0.0", "2.15.0.0", "2.14.0.0"]), kept)

    def test_prune_drops_unparseable_versions_first(self):
        old_ts = datetime.now(UTC) - timedelta(days=1)
        self._seed({
            "not-a-version": [(old_ts, 1)],
            "another-bad-key": [(old_ts, 1)],
            "2.13.0.0": [(old_ts, 1)],
            "2.14.0.0": [(old_ts, 1)],
        })
        hist = self._new()
        hist.record_restart("2.15.0.0", 1)
        kept = set(self._new()._data["versions"].keys())
        self.assertEqual(set(["2.15.0.0", "2.14.0.0", "2.13.0.0"]), kept,
                         "Unparseable version keys must be pruned before parseable ones")

    def test_prune_preserves_entries_of_retained_versions(self):
        old_ts = datetime.now(UTC) - timedelta(days=10)
        self._seed({
            "2.10.0.0": [(old_ts, 1)],
            "2.11.0.0": [(old_ts, 1)],
            "2.13.0.0": [(old_ts, 11), (old_ts, 22)],
            "2.14.0.0": [(old_ts, 33)],
        })
        hist = self._new()
        hist.record_restart("2.15.0.0", 44)
        reloaded = self._new()
        # Historical entries of retained versions must be preserved intact.
        self.assertEqual([11, 22], [e["anon_bytes"] for e in reloaded.restarts_for("2.13.0.0")])
        self.assertEqual([33], [e["anon_bytes"] for e in reloaded.restarts_for("2.14.0.0")])
        self.assertEqual([44], [e["anon_bytes"] for e in reloaded.restarts_for("2.15.0.0")])
