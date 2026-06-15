"""Probe worker main loop.

Cycles through every active account and records ``last_latency_ms`` /
``last_probe_at`` to the shared repository.  Designed for **balanced sustained**
resource use on a small VM:

* ``concurrency`` is bounded so we never burst more than N in-flight HTTP calls
  at a time (default 2).
* Between each ``batch_size`` chunk we sleep ``inter_batch_sleep_sec`` to spread
  the load and let other network users breathe.
* When a full cycle completes we sleep ``idle_sleep_sec`` before re-probing.

Throttled, not aggressive — that's the whole point of running this in its own
process instead of inside the quota refresh scheduler.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Sequence

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.control.account.repository import AccountRepository
from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.platform.runtime.batch import run_batch
from app.platform.runtime.clock import now_ms
from .client import ProbeResult, measure_chat


@dataclass(slots=True)
class _ProbeConfig:
    """Cached config snapshot — read once at run() time."""

    enabled:            bool
    interval_sec:       int
    concurrency:        int
    batch_size:         int
    inter_batch_sleep:  float
    idle_sleep_sec:     float
    request_timeout:    float
    max_tokens:         int
    model:              str

    @classmethod
    def from_config(cls) -> "_ProbeConfig":
        cfg = get_config()
        return cls(
            enabled            = cfg.get_bool("probe.enabled", True),
            interval_sec       = cfg.get_int("probe.interval_sec", 14_400),
            concurrency        = cfg.get_int("probe.concurrency", 2),
            batch_size         = cfg.get_int("probe.batch_size", 10),
            inter_batch_sleep  = float(cfg.get("probe.inter_batch_sleep_sec", 1.5)),
            idle_sleep_sec     = float(cfg.get("probe.idle_sleep_sec", 60.0)),
            request_timeout    = float(cfg.get("probe.request_timeout_sec", 30.0)),
            max_tokens         = cfg.get_int("probe.max_tokens", 1),
            model              = cfg.get_str("probe.model", "grok-4.20-fast"),
        )


class ProbeRunner:
    """Continuous, throttled latency-probing loop."""

    def __init__(self, repo: AccountRepository) -> None:
        self._repo    = repo
        self._cfg     = _ProbeConfig.from_config()
        self._stop    = asyncio.Event()
        self._patches: list[tuple[str, int, int]] = []
        self._patch_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        if not self._cfg.enabled:
            logger.info("probe worker disabled by config — exiting")
            return

        logger.info(
            "probe worker starting: model={} max_tokens={} concurrency={} "
            "batch_size={} inter_batch_sleep_s={} interval_sec={} idle_sleep_s={}",
            self._cfg.model,
            self._cfg.max_tokens,
            self._cfg.concurrency,
            self._cfg.batch_size,
            self._cfg.inter_batch_sleep,
            self._cfg.interval_sec,
            self._cfg.idle_sleep_sec,
        )

        while not self._stop.is_set():
            try:
                cycle_count = await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("probe cycle error: err={}", exc)
                cycle_count = 0

            if cycle_count == 0:
                # Nothing to probe right now — short sleep to avoid tight loop.
                await self._sleep_or_stop(5.0)
            else:
                logger.info(
                    "probe cycle complete: probed={} — idle_sleep_s={}",
                    cycle_count, self._cfg.idle_sleep_sec,
                )
                await self._sleep_or_stop(self._cfg.idle_sleep_sec)

    async def shutdown(self) -> None:
        self._stop.set()
        # Flush any pending latency patches so we don't lose the last cycle.
        await self._flush_patches()

    # ------------------------------------------------------------------
    # One full sweep over the active account set
    # ------------------------------------------------------------------

    async def _run_cycle(self) -> int:
        accounts = await self._list_probeable()
        if not accounts:
            return 0

        tokens = [a.token for a in accounts]
        logger.info(
            "probe cycle starting: account_count={} concurrency={} batch_size={}",
            len(tokens), self._cfg.concurrency, self._cfg.batch_size,
        )

        probed = 0
        for start in range(0, len(tokens), self._cfg.batch_size):
            if self._stop.is_set():
                break
            chunk = tokens[start : start + self._cfg.batch_size]
            results = await run_batch(
                chunk,
                self._probe_one,
                concurrency=self._cfg.concurrency,
                pause_sec=0.0,
                batch_size=0,
            )
            for r in results:
                if isinstance(r, ProbeResult):
                    await self._record_result(r)
                    probed += 1
            # Flush per-chunk so we don't accumulate too much in memory.
            await self._flush_patches()
            if self._stop.is_set():
                break
            if start + self._cfg.batch_size < len(tokens):
                await self._sleep_or_stop(self._cfg.inter_batch_sleep)
        return probed

    async def _list_probeable(self) -> list[AccountRecord]:
        snapshot = await self._repo.runtime_snapshot()
        return [
            r for r in snapshot.items
            if r.status == AccountStatus.ACTIVE and not r.is_deleted()
        ]

    # ------------------------------------------------------------------
    # Per-token probe
    # ------------------------------------------------------------------

    async def _probe_one(self, token: str) -> ProbeResult:
        return await measure_chat(
            token,
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            timeout_s=self._cfg.request_timeout,
        )

    async def _record_result(self, result: ProbeResult) -> None:
        async with self._patch_lock:
            ts_ms = now_ms()
            # We always record the latency even on failure so the selector can
            # avoid obviously bad accounts.  A probe failure rate above the
            # fail threshold puts the account in cool-down by the API server
            # via the existing failure-feedback path (not duplicated here).
            self._patches.append((result.token, int(result.total_ms), int(ts_ms)))

    async def _flush_patches(self) -> None:
        async with self._patch_lock:
            if not self._patches:
                return
            batch = self._patches
            self._patches = []

        from app.control.account.commands import AccountPatch

        try:
            await self._repo.patch_accounts([
                AccountPatch(
                    token=token,
                    last_latency_ms=latency_ms,
                    last_probe_at=ts_ms,
                )
                for token, latency_ms, ts_ms in batch
            ])
        except Exception as exc:
            logger.warning(
                "probe failed to persist latency: batch_size={} err={}",
                len(batch), exc,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep *seconds* but wake immediately on stop."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


__all__ = ["ProbeRunner"]
