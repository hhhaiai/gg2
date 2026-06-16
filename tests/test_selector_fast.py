"""Unit tests for the fast selector strategy and probe-runtime table columns."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.dataplane.account import selector
from app.dataplane.account.table import AccountRuntimeTable
from app.dataplane.shared.enums import PoolId, StatusId, ModeId  # noqa


def _build_basic_table() -> AccountRuntimeTable:
    """Construct a runtime table with 5 active basic-pool accounts and varying latencies."""
    table = AccountRuntimeTable()
    latencies_ms = [800, 200, 1500, 300, 1000]
    for i, lat in enumerate(latencies_ms):
        table._append_slot(
            token=f"tok-{i}",
            pool_id=int(PoolId.BASIC),
            status_id=int(StatusId.ACTIVE),
            quota_auto=10, quota_fast=10, quota_expert=10,
            quota_heavy=-1, quota_grok_4_3=-1, quota_console=-1,
            total_auto=10, total_fast=10, total_expert=10,
            total_heavy=0, total_grok_4_3=0, total_console=0,
            window_auto=1, window_fast=1, window_expert=1,
            window_heavy=0, window_grok_4_3=0, window_console=0,
            reset_auto=0, reset_fast=0, reset_expert=0,
            reset_heavy=0, reset_grok_4_3=0, reset_console=0,
            health=1.0,
            last_use_s=0, last_fail_s=0,
            fail_count=0,
            last_latency_ms=lat,
            last_probe_s=int(time.time()),
            tags=[],
        )
    # Rebuild latency-sorted cache after adding all accounts
    table.rebuild_latency_sorted_cache()
    return table


class FastSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = selector._STRATEGY_NAME
        selector.set_strategy("fast")

    def tearDown(self) -> None:
        selector._STRATEGY_NAME = self._prev

    def test_picks_from_top_20_percent(self) -> None:
        """When 5 accounts are probed, the top-20% bucket is just 1 — that
        single lowest-latency account must always be chosen (deterministic)."""
        table = _build_basic_table()
        # Latencies are 800, 200, 1500, 300, 1000 → idx 1 (200ms) is the fastest.
        idx = selector.select(
            table, int(PoolId.BASIC), int(ModeId.FAST),
            exclude_idxs=None, prefer_tag_idxs=None, now_s=0,
        )
        self.assertIsNotNone(idx)
        self.assertEqual(table.token_by_idx[idx], "tok-1")

    def test_falls_back_to_random_when_nothing_probed(self) -> None:
        """All accounts have last_probe_s=0 → fast strategy picks uniformly."""
        table = _build_basic_table()
        for i in range(len(table.token_by_idx)):
            table.last_probe_s_by_idx[i] = 0
            table.last_latency_ms_by_idx[i] = 0

        # Rebuild cache after clearing probe data
        table.rebuild_latency_sorted_cache()

        # Run many picks; check the chosen token set covers at least 3 of 5.
        picks = set()
        for _ in range(100):
            idx = selector.select(
                table, int(PoolId.BASIC), int(ModeId.FAST),
                exclude_idxs=None, prefer_tag_idxs=None, now_s=0,
            )
            if idx is not None:
                picks.add(table.token_by_idx[idx])
        self.assertGreaterEqual(len(picks), 3,
                               "fallback should sample randomly across the pool")

    def test_top_20_of_10_is_two(self) -> None:
        """With 10 probed accounts, the top-20% bucket has 2 entries — fast
        strategy must pick only between those two."""
        table = AccountRuntimeTable()
        # Latencies: 100, 200, 300, ..., 1000 (idx 0 fastest)
        for i, lat in enumerate(range(100, 1100, 100)):
            table._append_slot(
                token=f"tok-{i}", pool_id=int(PoolId.BASIC),
                status_id=int(StatusId.ACTIVE),
                quota_auto=10, quota_fast=10, quota_expert=10,
                quota_heavy=-1, quota_grok_4_3=-1, quota_console=-1,
                total_auto=10, total_fast=10, total_expert=10,
                total_heavy=0, total_grok_4_3=0, total_console=0,
                window_auto=1, window_fast=1, window_expert=1,
                window_heavy=0, window_grok_4_3=0, window_console=0,
                reset_auto=0, reset_fast=0, reset_expert=0,
                reset_heavy=0, reset_grok_4_3=0, reset_console=0,
                health=1.0, last_use_s=0, last_fail_s=0, fail_count=0,
                last_latency_ms=lat, last_probe_s=int(time.time()),
                tags=[],
            )

        # Rebuild cache after adding all accounts
        table.rebuild_latency_sorted_cache()

        chosen = set()
        for _ in range(50):
            idx = selector.select(
                table, int(PoolId.BASIC), int(ModeId.FAST),
                exclude_idxs=None, prefer_tag_idxs=None, now_s=0,
            )
            if idx is not None:
                chosen.add(table.token_by_idx[idx])
        # Only top-2 (tok-0 at 100ms, tok-1 at 200ms) should ever be picked.
        self.assertTrue(chosen.issubset({"tok-0", "tok-1"}),
                        f"unexpected picks outside top-20%: {chosen}")
        # And both should be picked (random within the bucket) over 50 trials.
        self.assertEqual(chosen, {"tok-0", "tok-1"})

    def test_excluded_idxs_still_respected(self) -> None:
        """The fastest account (tok-1) is excluded → top-2 becomes the second-fastest."""
        table = _build_basic_table()
        exclude = frozenset({1})  # exclude the 200ms account
        for _ in range(20):
            idx = selector.select(
                table, int(PoolId.BASIC), int(ModeId.FAST),
                exclude_idxs=exclude, prefer_tag_idxs=None, now_s=0,
            )
            self.assertIsNotNone(idx)
            self.assertNotEqual(idx, 1)

    def test_set_strategy_validates_known_names(self) -> None:
        selector.set_strategy("fast")
        self.assertEqual(selector.current_strategy(), "fast")
        with self.assertRaises(ValueError):
            selector.set_strategy("unknown")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
