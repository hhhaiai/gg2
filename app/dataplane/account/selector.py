"""Hot-path account selector — pluggable strategies.

Two fully independent strategies:

* ``_quota_select`` — scores candidates by health / quota / inflight / fails.
  Used when ``account.refresh.enabled=true``. Behaviour is the historical one,
  kept unchanged.
* ``_random_select`` — uniform random choice among non-cooling candidates.
  Used when ``account.refresh.enabled=false``. Ignores quota and health entirely.

Strategy selection is process-global, registered once by the lifespan via
:func:`set_strategy`. Callers (``AccountDirectory``) invoke :func:`select` /
:func:`select_any` and are unaware of which strategy is active.
"""

import array  # noqa: F401  — used in forward-referenced type annotations
import heapq
import random
from typing import Literal

from app.platform.config.snapshot import get_config
from ..shared.enums import PoolId
from .table import AccountRuntimeTable

# Scoring weights used by the quota strategy.
_W_HEALTH   = 100.0
_W_QUOTA    = 25.0
_W_RECENT   = 15.0     # penalty for recently used accounts
_W_INFLIGHT = 20.0
_W_FAIL     = 4.0
_RECENT_WINDOW_S = 15  # seconds


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

_StrategyName = Literal["quota", "random", "fast"]
_STRATEGY_NAME: _StrategyName = "random"


def set_strategy(name: _StrategyName) -> None:
    """Register the process-global selection strategy.

    Called once by the lifespan after reading ``account.refresh.enabled``.
    """
    global _STRATEGY_NAME
    if name not in ("quota", "random", "fast"):
        raise ValueError(f"unknown selection strategy: {name!r}")
    _STRATEGY_NAME = name


def current_strategy() -> _StrategyName:
    return _STRATEGY_NAME


# ---------------------------------------------------------------------------
# Public entry points — delegate to the active strategy
# ---------------------------------------------------------------------------


def select(
    table: AccountRuntimeTable,
    pool_id: int,
    mode_id: int,
    *,
    exclude_idxs: frozenset[int] | None = None,
    prefer_tag_idxs: set[int] | None    = None,
    now_s: int,
) -> int | None:
    """Select an account slot for ``(pool_id, mode_id)``.

    Returns the slot index or ``None`` when no candidate is available.
    Does not mutate the table — callers increment inflight separately.
    """
    if _STRATEGY_NAME == "fast":
        return _fast_select(
            table, pool_id, mode_id,
            exclude_idxs=exclude_idxs,
            prefer_tag_idxs=prefer_tag_idxs,
            now_s=now_s,
        )
    if _STRATEGY_NAME == "random":
        return _random_select(
            table, pool_id,
            exclude_idxs=exclude_idxs,
            prefer_tag_idxs=prefer_tag_idxs,
            now_s=now_s,
        )
    return _quota_select(
        table, pool_id, mode_id,
        exclude_idxs=exclude_idxs,
        prefer_tag_idxs=prefer_tag_idxs,
        now_s=now_s,
    )


def select_any(
    table: AccountRuntimeTable,
    pool_id: int,
    *,
    exclude_idxs: frozenset[int] | None = None,
    prefer_tag_idxs: set[int] | None    = None,
    now_s: int,
) -> int | None:
    """Select any active account in ``pool_id`` irrespective of per-mode quota.

    Used by WebSocket-based products that manage their own rate limiting.
    """
    if _STRATEGY_NAME == "fast":
        return _fast_select_any(
            table, pool_id,
            exclude_idxs=exclude_idxs,
            prefer_tag_idxs=prefer_tag_idxs,
            now_s=now_s,
        )
    if _STRATEGY_NAME == "random":
        return _random_select(
            table, pool_id,
            exclude_idxs=exclude_idxs,
            prefer_tag_idxs=prefer_tag_idxs,
            now_s=now_s,
        )
    return _quota_select_any(
        table, pool_id,
        exclude_idxs=exclude_idxs,
        prefer_tag_idxs=prefer_tag_idxs,
        now_s=now_s,
    )


# ---------------------------------------------------------------------------
# Strategy: quota — score-based selection (unchanged behaviour)
# ---------------------------------------------------------------------------


def _quota_select(
    table: AccountRuntimeTable,
    pool_id: int,
    mode_id: int,
    *,
    exclude_idxs: frozenset[int] | None,
    prefer_tag_idxs: set[int] | None,
    now_s: int,
) -> int | None:
    candidates: set[int] | None = table.mode_available.get((pool_id, mode_id))
    if not candidates:
        return None

    reset_col  = table._reset_col(mode_id)
    quota_col  = table._quota_col(mode_id)
    total_col  = table._total_col(mode_id)
    window_col = table._window_col(mode_id)
    _maybe_reset_windows(
        table, candidates, mode_id,
        reset_col, quota_col, total_col, window_col,
        pool_id, now_s,
    )

    working: set[int] = candidates.copy()
    if exclude_idxs:
        working -= exclude_idxs
    working = {idx for idx in working if int(quota_col[idx]) > 0}
    if not working:
        return None

    if prefer_tag_idxs:
        preferred = working & prefer_tag_idxs
        working = preferred if preferred else working

    return _best(table, working, quota_col, now_s)


def _quota_select_any(
    table: AccountRuntimeTable,
    pool_id: int,
    *,
    exclude_idxs: frozenset[int] | None,
    prefer_tag_idxs: set[int] | None,
    now_s: int,
) -> int | None:
    candidates: set[int] = _pool_union(table, pool_id)
    if not candidates:
        return None

    working = candidates.copy()
    if exclude_idxs:
        working -= exclude_idxs
    if not working:
        return None

    if prefer_tag_idxs:
        preferred = working & prefer_tag_idxs
        working = preferred if preferred else working

    return _best_no_quota(table, working, now_s)


def _maybe_reset_windows(
    table: AccountRuntimeTable,
    candidates: set[int],
    mode_id: int,
    reset_col: "array.array",
    quota_col: "array.array",
    total_col: "array.array",
    window_col: "array.array",
    pool_id: int,
    now_s: int,
) -> None:
    """Reset expired windows for basic-pool accounts inline (no API call needed)."""
    if pool_id != int(PoolId.BASIC):
        return

    for idx in list(candidates):
        r = reset_col[idx]
        if r == 0 or now_s < r:
            continue
        if int(table.pool_by_idx[idx]) != pool_id:
            continue
        new_total = int(total_col[idx])
        window_s  = int(window_col[idx])
        if new_total <= 0 or window_s <= 0:
            continue
        quota_col[idx] = new_total
        reset_col[idx] = now_s + window_s


def _best(
    table: AccountRuntimeTable,
    working: set[int],
    quota_col: "array.array",
    now_s: int,
) -> int | None:
    if not working:
        return None

    health_col   = table.health_by_idx
    inflight_col = table.inflight_by_idx
    fail_col     = table.fail_count_by_idx
    last_use_col = table.last_use_at_by_idx

    def _score(idx: int) -> float:
        # Hot-path inlining: pre-fetch all columns once.
        quota    = int(quota_col[idx])
        if quota <= 0:
            return float("-inf")
        health   = float(health_col[idx])
        inflight = int(inflight_col[idx])
        fails    = min(int(fail_col[idx]), 10)
        last_use = int(last_use_col[idx])
        score = (
            health   * _W_HEALTH
            + quota  * _W_QUOTA
            - inflight * _W_INFLIGHT
            - fails  * _W_FAIL
        )
        if last_use > 0:
            age_s = now_s - last_use
            if age_s < _RECENT_WINDOW_S:
                score -= (1.0 - age_s / _RECENT_WINDOW_S) * _W_RECENT
        return score

    # heapq.nlargest with n=1 walks the input once and tracks only the
    # current maximum — C-implemented and ~3-5x faster than a Python
    # for-loop over the same set when n is large.
    return heapq.nlargest(1, working, key=_score)[0] if working else None


def _best_no_quota(
    table: AccountRuntimeTable,
    working: set[int],
    now_s: int,
) -> int | None:
    if not working:
        return None

    health_col   = table.health_by_idx
    inflight_col = table.inflight_by_idx
    fail_col     = table.fail_count_by_idx
    last_use_col = table.last_use_at_by_idx

    def _score(idx: int) -> float:
        health   = float(health_col[idx])
        inflight = int(inflight_col[idx])
        fails    = min(int(fail_col[idx]), 10)
        last_use = int(last_use_col[idx])
        score = health * _W_HEALTH - inflight * _W_INFLIGHT - fails * _W_FAIL
        if last_use > 0:
            age_s = now_s - last_use
            if age_s < _RECENT_WINDOW_S:
                score -= (1.0 - age_s / _RECENT_WINDOW_S) * _W_RECENT
        return score

    return heapq.nlargest(1, working, key=_score)[0]


# ---------------------------------------------------------------------------
# Strategy: random — uniform choice with cooling + inflight filter
# ---------------------------------------------------------------------------


def _random_select(
    table: AccountRuntimeTable,
    pool_id: int,
    *,
    exclude_idxs: frozenset[int] | None,
    prefer_tag_idxs: set[int] | None,
    now_s: int,
) -> int | None:
    candidates: set[int] = _pool_union(table, pool_id)
    if not candidates:
        return None

    max_inflight = int(get_config("account.selection.max_inflight", 8))
    cooling_col  = table.cooling_until_s_by_idx
    inflight_col = table.inflight_by_idx

    working = candidates.copy()
    if exclude_idxs:
        working -= exclude_idxs
    working = {
        idx for idx in working
        if int(cooling_col[idx]) <= now_s
        and int(inflight_col[idx]) < max_inflight
    }
    if not working:
        return None

    if prefer_tag_idxs:
        preferred = working & prefer_tag_idxs
        working = preferred if preferred else working

    return random.choice(tuple(working))


# ---------------------------------------------------------------------------
# Strategy: fast — prefer low-latency accounts (probe worker driven)
# ---------------------------------------------------------------------------


def _get_fast_top_pct() -> float:
    """Fraction of fastest accounts to sample from.  Configurable."""
    raw = get_config("account.selection.fast_top_pct", 0.2)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.2
    # Clamp to a sane range to avoid degenerate behaviour.
    if v <= 0 or v > 1:
        return 0.2
    return v


def _fast_select(
    table: AccountRuntimeTable,
    pool_id: int,
    mode_id: int,
    *,
    exclude_idxs: frozenset[int] | None,
    prefer_tag_idxs: set[int] | None,
    now_s: int,
) -> int | None:
    candidates: set[int] | None = table.mode_available.get((pool_id, mode_id))
    if not candidates:
        return None

    max_inflight = int(get_config("account.selection.max_inflight", 8))
    cooling_col  = table.cooling_until_s_by_idx
    inflight_col = table.inflight_by_idx

    working: set[int] = candidates.copy()
    if exclude_idxs:
        working -= exclude_idxs
    working = {
        idx for idx in working
        if int(cooling_col[idx]) <= now_s
        and int(inflight_col[idx]) < max_inflight
    }
    if not working:
        return None

    if prefer_tag_idxs:
        preferred = working & prefer_tag_idxs
        working = preferred if preferred else working

    return _fast_pick(table, working)


def _fast_select_any(
    table: AccountRuntimeTable,
    pool_id: int,
    *,
    exclude_idxs: frozenset[int] | None,
    prefer_tag_idxs: set[int] | None,
    now_s: int,
) -> int | None:
    candidates: set[int] = _pool_union(table, pool_id)
    if not candidates:
        return None

    max_inflight = int(get_config("account.selection.max_inflight", 8))
    cooling_col  = table.cooling_until_s_by_idx
    inflight_col = table.inflight_by_idx

    working = candidates.copy()
    if exclude_idxs:
        working -= exclude_idxs
    working = {
        idx for idx in working
        if int(cooling_col[idx]) <= now_s
        and int(inflight_col[idx]) < max_inflight
    }
    if not working:
        return None

    if prefer_tag_idxs:
        preferred = working & prefer_tag_idxs
        working = preferred if preferred else working

    return _fast_pick(table, working)


def _fast_pick(
    table: AccountRuntimeTable,
    working: set[int],
) -> int | None:
    """Pick from the top-N% fastest probed accounts; fall back to random
    when no account in *working* has been probed yet.
    """
    if not working:
        return None

    latency_col = table.last_latency_col()
    probe_col   = table.last_probe_col()

    probed = [idx for idx in working
              if int(probe_col[idx]) > 0 and int(latency_col[idx]) > 0]
    if not probed:
        # Nothing has been probed yet — uniform random so we still serve traffic.
        return random.choice(tuple(working))

    probed.sort(key=lambda i: int(latency_col[i]))
    top_pct = _get_fast_top_pct()
    top_n = max(1, int(len(probed) * top_pct))
    return random.choice(probed[:top_n])


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _pool_union(table: AccountRuntimeTable, pool_id: int) -> set[int]:
    """Union of all ``mode_available`` buckets for ``pool_id``."""
    out: set[int] = set()
    for (pid, _mid), accounts in table.mode_available.items():
        if pid == pool_id:
            out |= accounts
    return out


__all__ = ["select", "select_any", "set_strategy", "current_strategy"]
