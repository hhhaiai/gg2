"""Account refresh service — mode-aware usage synchronisation."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.platform.errors import UpstreamError
from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms
from app.platform.runtime.batch import run_batch
from app.control.model.enums import ALL_MODES_FULL
from .enums import AccountStatus, QuotaSource
from .models import AccountRecord, QuotaWindow
from .quota_defaults import (
    default_quota_window,
    infer_pool,
    normalize_quota_window,
    supported_mode_ids,
    supports_mode,
)
from .state_machine import is_manageable

if TYPE_CHECKING:
    from .repository import AccountRepository


@dataclass
class RefreshResult:
    checked: int = 0
    refreshed: int = 0
    recovered: int = 0
    expired: int = 0
    disabled: int = 0
    rate_limited: int = 0
    failed: int = 0

    def merge(self, other: "RefreshResult") -> None:
        self.checked += other.checked
        self.refreshed += other.refreshed
        self.recovered += other.recovered
        self.expired += other.expired
        self.disabled += other.disabled
        self.rate_limited += other.rate_limited
        self.failed += other.failed


@dataclass
class OnDemandResult:
    """Result of an on-demand 429-triggered refresh.

    Attributes:
        refresh_result: Aggregated refresh stats from sampled accounts.
        server_blocked: True when ALL sampled accounts failed, suggesting
                        an IP-level block rather than individual account bans.
        sampled: Number of accounts that were sampled for validation.
        server_blocked_reason: Human-readable reason for UI notification.
    """
    refresh_result: RefreshResult = field(default_factory=RefreshResult)
    server_blocked: bool = False
    sampled: int = 0
    server_blocked_reason: str = ""

    @property
    def success(self) -> bool:
        return self.refresh_result.refreshed > 0


_MODE_KEYS = {
    0: "quota_auto",
    1: "quota_fast",
    2: "quota_expert",
    3: "quota_heavy",
    4: "quota_grok_4_3",
    5: "quota_console",  # console.x.ai 独立配额
}


class AccountRefreshService:
    """Fetches real quota data from the upstream usage API and persists it.

    Triggers:
      1. Import   — fetch all modes supported by the account's pool.
      2. Call     — fetch the called mode only (async, non-blocking).
      3. Schedule — refresh one pool per loop using that pool's supported modes.
    """

    def __init__(self, repository: "AccountRepository") -> None:
        self._repo = repository
        self._lock = asyncio.Lock()
        self._od_lock = asyncio.Lock()
        self._od_last = 0.0
        # Per-token throttle for refresh_token_only. Key: token, value: monotonic ts.
        # Prevents one token from triggering N upstream probes within the throttle window.
        self._od_last_token: dict[str, float] = {}
        # Failure coalescer: groups rapid record_failure_async calls into a single
        # batched DB write. Without this, 100 RPS * 5% error = 5 UPDATE/s; with it,
        # bursty errors collapse into one UPDATE per coalesce window.
        self._fail_coalescer = _FailureCoalescer(repository)

    # ------------------------------------------------------------------
    # Usage API fetch (delegates to dataplane reverse protocol)
    # ------------------------------------------------------------------

    async def _fetch_all_quotas(
        self, token: str, pool: str
    ) -> dict[int, QuotaWindow] | None:
        """Fetch quota windows for every mode supported by *pool*.

        Examples:
          - basic -> fast
          - super -> auto / fast / expert / grok_4_3
          - heavy -> auto / fast / expert / heavy / grok_4_3
        """
        try:
            from app.dataplane.reverse.protocol.xai_usage import fetch_all_quotas

            usage_modes = tuple(
                mode_id for mode_id in supported_mode_ids(pool) if mode_id != 5
            )
            if not usage_modes:
                return None
            return await fetch_all_quotas(token, usage_modes)
        except UpstreamError:
            raise
        except Exception as exc:
            logger.debug(
                "account quota fetch failed: token={}... pool={} error={}",
                token[:10],
                pool,
                exc,
            )
            return None

    async def _fetch_mode_quota(
        self, token: str, pool: str, mode_id: int
    ) -> QuotaWindow | None:
        """Fetch a single mode quota window."""
        if not supports_mode(pool, mode_id):
            logger.debug(
                "account mode quota fetch skipped: token={}... pool={} mode_id={} reason=unsupported_mode",
                token[:10],
                pool,
                mode_id,
            )
            return None
        try:
            from app.dataplane.reverse.protocol.xai_usage import fetch_mode_quota

            return await fetch_mode_quota(token, mode_id)
        except UpstreamError:
            raise
        except Exception as exc:
            logger.debug(
                "account mode quota fetch failed: token={}... pool={} mode_id={} error={}",
                token[:10],
                pool,
                mode_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Core refresh logic
    # ------------------------------------------------------------------

    async def refresh_on_import(self, tokens: list[str]) -> RefreshResult:
        """Called after bulk import — sync real quotas for all accounts.

        Processes accounts in chunks to avoid memory/network spikes:
        - Each chunk is validated independently with bounded concurrency.
        - A short pause between chunks lets the event loop breathe.

        In random mode (refresh.enabled=false), this method should not be called
        at all — the caller (_refresh_imported) checks the config flag. But if
        called anyway, we return early to avoid unnecessary network traffic.
        """
        # Early exit in random mode
        if not get_config("account.refresh.enabled", True):
            logger.debug(
                "refresh_on_import called in random mode, skipping: token_count={}",
                len(tokens)
            )
            return RefreshResult(checked=len(tokens))

        records = await self._repo.get_accounts(tokens)
        active = [r for r in records if is_manageable(r)]
        if not active:
            return RefreshResult(checked=len(records))

        concurrency = get_config("account.refresh.usage_concurrency", 5)
        chunk_size = get_config("account.refresh.import_chunk_size", 50)
        agg = RefreshResult(checked=len(records))

        for i in range(0, len(active), chunk_size):
            chunk = active[i : i + chunk_size]
            results = await run_batch(
                chunk,
                lambda r: self._refresh_one(r, apply_fallback=True),
                concurrency=concurrency,
            )
            for r in results:
                agg.merge(r)
            # Brief pause between chunks to avoid saturating the uplink.
            if i + chunk_size < len(active):
                await asyncio.sleep(2.0)

        logger.info(
            "account import refresh completed: total={} chunks={} "
            "refreshed={} failed={} expired={}",
            len(active),
            (len(active) + chunk_size - 1) // chunk_size,
            agg.refreshed,
            agg.failed,
            agg.expired,
        )
        return agg

    async def refresh_call_async(self, token: str, mode_id: int) -> None:
        """Fire-and-forget single-mode quota sync after a successful call."""
        record = (await self._repo.get_accounts([token]) or [None])[0]
        if record is None or record.is_deleted():
            return

        # mode_id=5 (CONSOLE) 是本地管理的配额，不需要请求 xai usage API
        # 直接做本地扣减并更新 usage_use_count
        if mode_id == 5:
            await self._apply_single_mode(
                record, mode_id, window=None, is_use=True, use_at_ms=now_ms()
            )
            return

        try:
            window = await self._fetch_mode_quota(token, record.pool, mode_id)
        except UpstreamError as exc:
            if await self._expire_invalid_credentials(record, exc):
                return
            raise
        await self._apply_single_mode(
            record, mode_id, window, is_use=True, use_at_ms=now_ms()
        )

    async def refresh_scheduled(self, pool: str | None = None) -> RefreshResult:
        """Periodic refresh — fetch real quotas for all (or one pool's) accounts.

        Optimisations:
        - Skip accounts used in the last ``recent_use_skip_sec`` seconds
          (they already have fresh quota data from the call path).
        - Skip accounts synced in the last ``recent_sync_skip_sec`` seconds
          (avoid redundant upstream probes).
        - Process in chunks with pauses to spread network load evenly
          across the refresh interval instead of spiking.
        - Default concurrency is now 5 (not 50) to avoid saturating
          residential/cellular uplinks.

        Args:
            pool: When set, only refreshes accounts belonging to that pool.
                  When ``None``, refreshes all pools.
        """
        import time as _time

        snapshot = await self._repo.runtime_snapshot()
        records = [r for r in snapshot.items if is_manageable(r)]
        if pool is not None:
            records = [r for r in records if r.pool == pool]

        if not records:
            return RefreshResult()

        now = now_ms()
        recent_use_sec = int(get_config("account.refresh.recent_use_skip_sec", 300))
        recent_sync_sec = int(get_config("account.refresh.recent_sync_skip_sec", 300))
        use_cutoff = now - recent_use_sec * 1000
        sync_cutoff = now - recent_sync_sec * 1000

        # Filter out recently used / recently synced accounts.
        to_refresh: list[AccountRecord] = []
        skipped_recent = 0
        for r in records:
            if r.last_use_at and r.last_use_at > use_cutoff:
                skipped_recent += 1
                continue
            if r.last_sync_at and r.last_sync_at > sync_cutoff:
                skipped_recent += 1
                continue
            to_refresh.append(r)

        if not to_refresh:
            logger.debug(
                "account scheduled refresh skipped: pool={} total={} all_recently_used",
                pool or "all",
                len(records),
            )
            return RefreshResult(checked=len(records))

        logger.info(
            "account scheduled refresh starting: pool={} total={} "
            "to_refresh={} skipped_recent={}",
            pool or "all",
            len(records),
            len(to_refresh),
            skipped_recent,
        )

        concurrency = get_config("account.refresh.usage_concurrency", 5)
        chunk_size = get_config("account.refresh.import_chunk_size", 50)
        agg = RefreshResult(checked=len(records))

        # Spread chunks across time to flatten the network spike.
        # For a 2h interval with 1000 accounts and chunk_size=50,
        # each chunk gets ~144s pause → ~5 concurrent requests average.
        interval_sec = 7200  # default; scheduler passes pool-specific interval
        if pool is not None:
            from .scheduler import _interval
            interval_sec = _interval(pool)
        n_chunks = max(1, (len(to_refresh) + chunk_size - 1) // chunk_size)
        # Pause between chunks: spread evenly, but cap at 60s and floor at 1s.
        spread_pause = max(1.0, min(60.0, interval_sec / n_chunks * 0.5))

        for i in range(0, len(to_refresh), chunk_size):
            chunk = to_refresh[i : i + chunk_size]
            results = await run_batch(
                chunk,
                lambda r: self._refresh_one(r, apply_fallback=True),
                concurrency=concurrency,
            )
            for r in results:
                agg.merge(r)
            # Spread pause between chunks.
            if i + chunk_size < len(to_refresh):
                await asyncio.sleep(spread_pause)

        logger.info(
            "account scheduled refresh completed: pool={} checked={} "
            "refreshed={} recovered={} failed={} expired={} skipped_recent={}",
            pool or "all",
            agg.checked,
            agg.refreshed,
            agg.recovered,
            agg.failed,
            agg.expired,
            skipped_recent,
        )
        return agg

    async def refresh_on_demand(
        self,
        *,
        triggered_by_token: str | None = None,
    ) -> "OnDemandResult":
        """Throttled on-demand refresh triggered by 429 or request path.

        Instead of refreshing ALL accounts (expensive), this now:
        1. Samples up to ``on_demand_429_sample_size`` available accounts.
        2. Validates each sampled account against the upstream API.
        3. Detects server-level blocks vs individual account bans:
           - If ALL sampled accounts fail → likely server block →
             return ``server_blocked=True`` so the UI can notify.
           - If SOME accounts succeed → individual account ban →
             the banned account is excluded, others continue working.
        """
        import random as _random
        import time

        min_interval = float(
            get_config("account.refresh.on_demand_min_interval_sec", 300)
        )

        now = time.monotonic()
        if now - self._od_last < min_interval:
            return OnDemandResult()
        if self._od_lock.locked():
            return OnDemandResult()
        async with self._od_lock:
            now = time.monotonic()
            if now - self._od_last < min_interval:
                return OnDemandResult()

            # Sample a subset of available accounts instead of refreshing all.
            sample_size = int(
                get_config("account.refresh.on_demand_429_sample_size", 100)
            )
            snapshot = await self._repo.runtime_snapshot()
            available = [
                r for r in snapshot.items
                if is_manageable(r) and r.status == AccountStatus.ACTIVE
            ]
            if not available:
                self._od_last = time.monotonic()
                return OnDemandResult()

            # Exclude the triggering token (already known to be 429'd).
            if triggered_by_token:
                available = [r for r in available if r.token != triggered_by_token]
            if not available:
                self._od_last = time.monotonic()
                return OnDemandResult()

            # Random sample to avoid always hitting the same accounts first.
            if len(available) > sample_size:
                available = _random.sample(available, sample_size)

            concurrency = get_config("account.refresh.usage_concurrency", 5)
            results = await run_batch(
                available,
                lambda r: self._refresh_one(r, apply_fallback=False),
                concurrency=concurrency,
            )
            agg = RefreshResult()
            for r in results:
                agg.merge(r)
            self._od_last = time.monotonic()

            # Server block detection: if ALL sampled accounts failed
            # (and we had accounts to test), it's likely a server-level block.
            server_blocked = (
                len(available) > 0
                and agg.checked > 0
                and agg.refreshed == 0
                and agg.failed == agg.checked
            )

            if server_blocked:
                logger.warning(
                    "account on-demand refresh detected possible server block: "
                    "sampled={} all_failed=true — upstream may be blocking this IP",
                    len(available),
                )
            else:
                logger.info(
                    "account on-demand refresh completed: sampled={} "
                    "refreshed={} failed={} server_blocked=false",
                    len(available),
                    agg.refreshed,
                    agg.failed,
                )

            return OnDemandResult(
                refresh_result=agg,
                server_blocked=server_blocked,
                sampled=len(available),
            )

    async def refresh_token_only(self, token: str, pool: str | None = None) -> RefreshResult:
        """Refresh a single token — used by the 429 hot-path so a single failure
        never cascades into a full account-pool refresh.

        Per-token throttle defaults to ``on_demand_min_interval_sec`` (300s) to
        match the legacy bulk throttle; override via the second positional arg
        in seconds when needed for testing.
        """
        import time

        min_interval = float(
            get_config("account.refresh.on_demand_min_interval_sec", 300)
        )
        now = time.monotonic()
        last = self._od_last_token.get(token, 0.0)
        if now - last < min_interval:
            return RefreshResult()

        records = await self._repo.get_accounts([token])
        if not records:
            return RefreshResult()
        record = records[0]
        if record.is_deleted() or not is_manageable(record):
            return RefreshResult()

        async with self._od_lock:
            # Re-check throttle inside the lock to avoid stampedes.
            now = time.monotonic()
            if now - self._od_last_token.get(token, 0.0) < min_interval:
                return RefreshResult()
            result = await self._refresh_one(record, apply_fallback=False)
            self._od_last_token[token] = time.monotonic()
            return result

    # ------------------------------------------------------------------
    # Failure coalescer lifecycle
    # ------------------------------------------------------------------

    async def start_failure_coalescer(self) -> None:
        """Start the background flush task for coalesced failure writes.

        Idempotent — safe to call from lifespan startup.
        """
        await self._fail_coalescer.start()

    async def stop_failure_coalescer(self) -> None:
        """Stop the flush task and drain pending failures.

        Idempotent — safe to call from lifespan shutdown.
        """
        await self._fail_coalescer.stop()

    async def refresh_tokens(self, tokens: list[str]) -> RefreshResult:
        """Explicit refresh for a list of tokens (admin / manual trigger)."""
        records = [r for r in await self._repo.get_accounts(tokens) if is_manageable(r)]
        concurrency = get_config("account.refresh.usage_concurrency", 50)
        results = await run_batch(records, self._refresh_one, concurrency=concurrency)
        agg = RefreshResult()
        for r in results:
            agg.merge(r)
        return agg

    async def revive_expired_tokens(
        self,
        tokens: list[str],
        *,
        concurrency: int = 2,
    ) -> RefreshResult:
        """Conservatively re-check expired tokens and reactivate only real successes.

        This is intentionally stricter than ``refresh_tokens``:
        - only expired records are considered
        - fallback quota estimates are disabled
        - a token is cleared back to ACTIVE only after live quota data is fetched
        """
        from .commands import AccountPatch

        records = [
            r
            for r in await self._repo.get_accounts(tokens)
            if not r.is_deleted() and r.status == AccountStatus.EXPIRED
        ]
        if not records:
            return RefreshResult()

        async def _revive_one(record: AccountRecord) -> RefreshResult:
            result = await self._refresh_one(record, apply_fallback=False)
            if result.refreshed:
                await self._repo.patch_accounts(
                    [AccountPatch(token=record.token, clear_failures=True)]
                )
                result.recovered += 1
            return result

        results = await run_batch(
            records,
            _revive_one,
            concurrency=max(1, min(int(concurrency), 10)),
        )
        agg = RefreshResult()
        for r in results:
            agg.merge(r)
        return agg

    # ------------------------------------------------------------------
    # Per-account refresh
    # ------------------------------------------------------------------

    async def _refresh_one(
        self,
        record: AccountRecord,
        *,
        apply_fallback: bool = False,
    ) -> RefreshResult:
        """Fetch all pool-supported modes from the usage API and persist them.

        apply_fallback=True  — used by scheduled/import paths: when API fails,
                               decrement REAL quotas or reset expired DEFAULT windows.
        apply_fallback=False — used by manual/on-demand paths: if API fails, return
                               failed=1 immediately without touching stored data.
        """
        if record.is_deleted():
            return RefreshResult()

        try:
            windows = await self._fetch_all_quotas(record.token, record.pool)
        except UpstreamError as exc:
            if await self._expire_invalid_credentials(record, exc):
                return RefreshResult(checked=1, expired=1, failed=0)
            raise

        # API call completely failed — no real data available.
        if windows is None:
            if not apply_fallback:
                return RefreshResult(checked=1, failed=1)
            # Scheduled/import path: apply conservative fallback.
            return await self._apply_fallback(record)

        # We got at least a response — apply real data per mode.
        qs = record.quota_set()
        now = now_ms()
        patches: dict[str, dict] = {}
        refreshed = False

        for mode in ALL_MODES_FULL:
            mode_id = int(mode)
            if mode_id in windows:
                window = normalize_quota_window(record.pool, mode_id, windows[mode_id])
                if window is None:
                    continue
                patches[_MODE_KEYS[mode_id]] = window.to_dict()
                refreshed = True
            elif apply_fallback:
                existing = qs.get(mode_id)
                if existing is None:
                    continue
                if existing.source == QuotaSource.REAL:
                    patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                        remaining=max(0, existing.remaining - 1),
                        total=existing.total,
                        window_seconds=existing.window_seconds,
                        reset_at=existing.reset_at,
                        synced_at=existing.synced_at,
                        source=QuotaSource.ESTIMATED,
                    ).to_dict()
                elif existing.is_window_expired(now):
                    default = default_quota_window(record.pool, mode_id)
                    if default is None:
                        continue
                    patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                        remaining=default.total,
                        total=default.total,
                        window_seconds=default.window_seconds,
                        reset_at=now + default.window_seconds * 1000,
                        synced_at=now,
                        source=QuotaSource.DEFAULT,
                    ).to_dict()

        if not patches:
            return RefreshResult(checked=1, failed=0 if refreshed else 1)

        # Infer pool type from live quota data and patch if it changed.
        inferred = infer_pool(windows)  # type: ignore[arg-type]
        pool_patch = inferred if inferred != record.pool else None
        if pool_patch:
            logger.info(
                "account pool updated from live quota: token={}... previous_pool={} current_pool={}",
                record.token[:10],
                record.pool,
                inferred,
            )

        from .commands import AccountPatch

        await self._repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    pool=pool_patch,
                    last_sync_at=now_ms() if refreshed else None,
                    usage_sync_delta=1 if refreshed else None,
                    **patches,  # type: ignore[arg-type]
                )
            ]
        )
        was_cooling = record.status == AccountStatus.COOLING
        return RefreshResult(
            checked=1,
            refreshed=1 if refreshed else 0,
            failed=0 if refreshed else 1,
            recovered=1 if (was_cooling and refreshed) else 0,
        )

    async def _apply_fallback(self, record: AccountRecord) -> RefreshResult:
        """Conservative fallback when API is unreachable (scheduled/import path only)."""
        qs = record.quota_set()
        now = now_ms()
        patches: dict[str, dict] = {}

        for mode in ALL_MODES_FULL:
            mode_id = int(mode)
            existing = qs.get(mode_id)
            if existing is None:
                continue
            if existing.source == QuotaSource.REAL:
                patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                    remaining=max(0, existing.remaining - 1),
                    total=existing.total,
                    window_seconds=existing.window_seconds,
                    reset_at=existing.reset_at,
                    synced_at=existing.synced_at,
                    source=QuotaSource.ESTIMATED,
                ).to_dict()
            elif existing.is_window_expired(now):
                default = default_quota_window(record.pool, mode_id)
                if default is None:
                    continue
                patches[_MODE_KEYS[mode_id]] = QuotaWindow(
                    remaining=default.total,
                    total=default.total,
                    window_seconds=default.window_seconds,
                    reset_at=now + default.window_seconds * 1000,
                    synced_at=now,
                    source=QuotaSource.DEFAULT,
                ).to_dict()

        if patches:
            from .commands import AccountPatch

            await self._repo.patch_accounts(
                [AccountPatch(token=record.token, **patches)]
            )  # type: ignore[arg-type]

        return RefreshResult(checked=1, failed=1)

    async def record_failure_async(
        self, token: str, mode_id: int, exc: BaseException | None = None
    ) -> None:
        """Fire-and-forget: persist failure counter and timestamp after a failed call.

        Implementation: enqueues a (token, mode_id, exc) event into the
        _FailureCoalescer, which batches events that occur within a short
        window into a single ``patch_accounts`` call. The hot path no longer
        performs a SELECT + UPDATE per failure event.
        """
        await self._fail_coalescer.enqueue(token, mode_id, exc)

    async def _apply_single_mode(
        self,
        record: AccountRecord,
        mode_id: int,
        window: QuotaWindow | None,
        *,
        is_use: bool = False,
        use_at_ms: int | None = None,
    ) -> None:
        qs = record.quota_set()
        mode_key = _MODE_KEYS.get(mode_id)
        if mode_key is None:
            logger.warning(
                "account single-mode sync skipped: token={}... pool={} mode_id={} reason=unknown_mode",
                record.token[:10],
                record.pool,
                mode_id,
            )
            return

        quota_patch: dict[str, dict] = {}
        if window is not None:
            normalized = normalize_quota_window(record.pool, mode_id, window)
            if normalized is None:
                logger.debug(
                    "account single-mode quota patch skipped: token={}... pool={} mode_id={} reason=unsupported_mode",
                    record.token[:10],
                    record.pool,
                    mode_id,
                )
                return
            quota_patch[mode_key] = normalized.to_dict()
        else:
            existing = qs.get(mode_id)
            if existing is not None:
                now = now_ms()
                # 如果窗口已过期，重置为默认值（适用于本地管理的配额，如 console）
                if existing.is_window_expired(now):
                    default = default_quota_window(record.pool, mode_id)
                    if default is not None:
                        quota_patch[mode_key] = QuotaWindow(
                            remaining=max(0, default.total - 1),  # 本次调用消耗1次
                            total=default.total,
                            window_seconds=default.window_seconds,
                            reset_at=now + default.window_seconds * 1000,
                            synced_at=now,
                            source=QuotaSource.DEFAULT,
                        ).to_dict()
                else:
                    # reset_at 为 None 时（首次扣减），设置窗口起始时间
                    reset_at = existing.reset_at
                    if reset_at is None and existing.window_seconds > 0:
                        reset_at = now + existing.window_seconds * 1000
                    quota_patch[mode_key] = QuotaWindow(
                        remaining=max(0, existing.remaining - 1),
                        total=existing.total,
                        window_seconds=existing.window_seconds,
                        reset_at=reset_at,
                        synced_at=existing.synced_at,
                        source=QuotaSource.ESTIMATED,
                    ).to_dict()
            else:
                logger.debug(
                    "account single-mode quota patch skipped: token={}... pool={} mode_id={} reason=unsupported_mode",
                    record.token[:10],
                    record.pool,
                    mode_id,
                )

        from .commands import AccountPatch

        await self._repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    last_sync_at=now_ms() if window is not None else None,
                    usage_sync_delta=1 if window is not None else None,
                    usage_use_delta=1 if is_use else None,
                    last_use_at=use_at_ms if is_use else None,
                    **quota_patch,  # type: ignore[arg-type]
                )
            ]
        )

    async def _expire_invalid_credentials(
        self, record: AccountRecord, exc: UpstreamError
    ) -> bool:
        from .invalid_credentials import mark_account_invalid_credentials

        return await mark_account_invalid_credentials(
            self._repo,
            record.token,
            exc,
            source="usage refresh",
        )


# ---------------------------------------------------------------------------
# Failure coalescer
# ---------------------------------------------------------------------------


class _FailureCoalescer:
    """Coalesce rapid ``record_failure_async`` events into batched DB writes.

    Without this, every failed request issues its own SELECT (to read the
    current quota window for 429 quota-reset) + UPDATE. At 100 RPS * 5% error
    that's 10 DB ops/s. Under a 100% upstream outage it scales to hundreds
    per second and overwhelms WAL / Redis Cluster.

    Coalescing strategy:
      - Per (token, mode_id) keep one pending entry.
      - On every enqueue, increment counters and remember the latest event ts.
      - A background task flushes the buffer every ``flush_interval_sec``;
        flushed entries are merged into a single ``patch_accounts`` call
        (one entry per token+mode after merge).
      - The 429 quota-reset path is also coalesced — the quota patch is
        computed on the first event for a (token, mode) and reused, since
        the resulting state (remaining=0, reset_at=now+window) is identical
        across rapid duplicate failures.
    """

    def __init__(
        self,
        repository: "AccountRepository",
        *,
        flush_interval_sec: float = 1.0,
    ) -> None:
        self._repo = repository
        self._flush_interval = flush_interval_sec
        # Key: (token, mode_id). Value: dict with merged fields.
        self._pending: dict[tuple[str, int], dict] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="failure-coalescer")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._flush()

    async def enqueue(
        self, token: str, mode_id: int, exc: BaseException | None
    ) -> None:
        try:
            status = getattr(exc, "status", None)
            async with self._lock:
                key = (token, mode_id)
                entry = self._pending.get(key)
                if entry is None:
                    entry = {
                        "fail_delta": 0,
                        "last_fail_at": 0,
                        "last_fail_reason": None,
                        "is_429": False,
                        "quota_patch": None,
                    }
                    self._pending[key] = entry
                entry["fail_delta"] += 1
                now = now_ms()
                entry["last_fail_at"] = max(entry["last_fail_at"], now)
                if status == 429:
                    entry["is_429"] = True
                    entry["last_fail_reason"] = "rate_limited"
                elif entry["last_fail_reason"] is None and status is not None:
                    entry["last_fail_reason"] = f"http_{int(status)}"
        except Exception as e:
            logger.debug("failure coalescer enqueue error: token={}... err={}", token[:10], e)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._flush_interval)
            except asyncio.CancelledError:
                return
            try:
                await self._flush()
            except Exception as e:
                logger.debug("failure coalescer flush error: {}", e)

    async def _flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            pending = self._pending
            self._pending = {}

        # For 429 entries we still need the current quota window to compute the
        # reset_at. Batch-fetch only the tokens we need, then build patches.
        tokens_429 = {t for (t, _m), e in pending.items() if e["is_429"]}
        quota_by_token: dict[str, "AccountRecord"] = {}
        if tokens_429:
            try:
                records = await self._repo.get_accounts(list(tokens_429))
                quota_by_token = {r.token: r for r in records}
            except Exception as e:
                logger.debug("failure coalescer quota fetch error: {}", e)

        from .commands import AccountPatch

        patches: list[AccountPatch] = []
        for (token, mode_id), entry in pending.items():
            quota_patch: dict[str, dict] = {}
            if entry["is_429"] and mode_id in _MODE_KEYS:
                record = quota_by_token.get(token)
                if record is not None:
                    now = now_ms()
                    window = record.quota_set().get(mode_id)
                    if window is not None:
                        reset_at = (
                            window.reset_at
                            if window.reset_at is not None and window.reset_at > now
                            else now + max(window.window_seconds, 1) * 1000
                        )
                        quota_patch[_MODE_KEYS[mode_id]] = QuotaWindow(
                            remaining=0,
                            total=window.total,
                            window_seconds=window.window_seconds,
                            reset_at=reset_at,
                            synced_at=window.synced_at,
                            source=QuotaSource.ESTIMATED,
                        ).to_dict()
            patches.append(
                AccountPatch(
                    token=token,
                    usage_fail_delta=entry["fail_delta"],
                    last_fail_at=entry["last_fail_at"],
                    last_fail_reason=entry["last_fail_reason"],
                    **quota_patch,
                )
            )

        if patches:
            try:
                await self._repo.patch_accounts(patches)
            except Exception as e:
                logger.debug("failure coalescer patch write error: {}", e)


__all__ = ["AccountRefreshService", "RefreshResult", "OnDemandResult"]
