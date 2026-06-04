"""Shared console.x.ai completion helpers.

Both console_chat.py and console_responses.py used to carry near-identical
copies of three helpers (~30 lines each).  Extracted here so the rules
for background-task error logging, fire-and-forget quota persistence,
and failure-count persistence stay in lockstep.

The 5-deep stack of (reserve → call upstream → yield text → release →
feedback) is still open-coded in each file because the SSE shape differs
(Chat Completions vs Responses API); that refactor is tracked as a
follow-up and is not done here to keep the diff small and behaviour-
preserving.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.platform.logging.logger import logger
from app.dataplane.account.selector import current_strategy
from app.control.account.runtime import get_refresh_service

if TYPE_CHECKING:
    pass


def log_task_exception(task: "asyncio.Task", *, label: str = "console") -> None:
    """Swallow-but-log background task exception (called via add_done_callback)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("{} background task failed: task={} error={}",
                       label, task.get_name(), exc)


async def quota_sync(token: str, mode_id: int, *, label: str = "console") -> None:
    """Fire-and-forget: persist the just-completed call's quota decrement.

    Skipped in random mode (no upstream probe) — only meaningful when
    the refresh service is running and tracking real quota windows.
    """
    try:
        if current_strategy() != "quota":
            return
        svc = get_refresh_service()
        if svc is not None:
            await svc.refresh_call_async(token, mode_id)
    except Exception as exc:
        logger.warning(
            "{} quota sync failed: token={}... mode_id={} error={}",
            label, token[:10], mode_id, exc,
        )


async def fail_sync(
    token: str,
    mode_id: int,
    exc: BaseException | None = None,
    *,
    label: str = "console",
) -> None:
    """Fire-and-forget: persist failure counter after a failed call.

    The account refresh service internally debounces these writes so a
    burst of failures for the same (token, mode) collapses into a
    single patch_accounts call.
    """
    try:
        svc = get_refresh_service()
        if svc is not None:
            await svc.record_failure_async(token, mode_id, exc)
    except Exception as e:
        logger.warning(
            "{} fail sync error: token={}... mode_id={} error={}",
            label, token[:10], mode_id, e,
        )


__all__ = ["log_task_exception", "quota_sync", "fail_sync"]
