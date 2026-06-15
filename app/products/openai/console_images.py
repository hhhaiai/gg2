"""Console image generation service — routes to console.x.ai/v1/images/generations.

X 免费账号走 console.x.ai 的 OpenAI Images API 兼容端点,
与 console_chat 共用 reserve/release/feedback 流程,差别只在 SSE 适配。

非流式优先:OpenAI Images API 本身没有 streaming,只能等上游整张返回。
"""

import asyncio

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError
from app.platform.runtime.clock import now_s
from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.model.registry import resolve as resolve_model
from app.products._account_selection import reserve_account, selection_max_retries
from app.products.openai.chat import _configured_retry_codes, _should_retry_upstream
from app.dataplane.reverse.protocol.xai_console_image import (
    generate_via_console,
)


def _log_task_exception(task: "asyncio.Task") -> None:
    from app.products._console_helpers import log_task_exception
    log_task_exception(task, label="console image")


async def _quota_sync(token: str, mode_id: int) -> None:
    from app.products._console_helpers import quota_sync as _quota_sync_impl
    await _quota_sync_impl(token, mode_id, label="console image")


async def _fail_sync(token: str, mode_id: int, exc: BaseException | None = None) -> None:
    from app.products._console_helpers import fail_sync as _fail_sync_impl
    await _fail_sync_impl(token, mode_id, exc, label="console image")


def _extract_prompt(messages: list[dict]) -> str:
    """Last user message text (matches chat.py::_extract_message semantics)."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return ""


async def generate(
    *,
    model: str,
    prompt: str | None = None,
    messages: list[dict] | None = None,
    n: int = 1,
    size: str = "1024x1024",
    response_format: str = "url",
) -> dict:
    """Generate images via console.x.ai/v1/images/generations.

    Mirrors the non-streaming branch of console_chat.completions so the
    reserve/release/feedback flow is identical:

      reserve → POST → release → feedback → fire-and-forget quota sync

    Both ``prompt=`` and ``messages=`` are accepted (chat router feeds us
    messages, image router feeds us a bare prompt).
    """
    cfg = get_config()
    spec = resolve_model(model)
    timeout_s = cfg.get_float("chat.timeout", 120.0)
    max_retries = selection_max_retries()
    retry_codes = _configured_retry_codes(cfg)

    if not prompt:
        if not messages:
            raise UpstreamError("Console image: empty prompt", status=400)
        prompt = _extract_prompt(messages)
    if not prompt:
        raise UpstreamError("Console image: empty prompt after extraction", status=400)

    logger.info(
        "console image request: model={} n={} size={} prompt_len={}",
        model, n, size, len(prompt),
    )

    from app.dataplane.account import _directory as _acct_dir
    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    excluded: list[str] = []
    for attempt in range(max_retries + 1):
        acct, selected_mode_id, _server_blocked = await reserve_account(
            directory,
            spec,
            now_s_override=now_s(),
            exclude_tokens=excluded or None,
        )
        if acct is None:
            raise RateLimitError("No available accounts for this model tier")

        token = acct.token
        success = False
        fail_exc: BaseException | None = None

        try:
            try:
                result = await generate_via_console(
                    token,
                    model=model,
                    prompt=prompt,
                    n=n,
                    size=size,
                    response_format=response_format,
                    timeout_s=timeout_s,
                )
                success = True
                logger.info(
                    "console image completed: model={} n={} data_items={}",
                    model, n, len(result.get("data", [])),
                )
                return result

            except UpstreamError as exc:
                fail_exc = exc
                if _should_retry_upstream(exc, retry_codes) and attempt < max_retries:
                    logger.warning(
                        "console image retry: attempt={}/{} status={} token={}...",
                        attempt + 1, max_retries, exc.status, token[:8],
                    )
                    excluded.append(token)
                    continue
                raise

        finally:
            await directory.release(acct)
            kind = (
                FeedbackKind.SUCCESS if success
                else feedback_kind_for_error(fail_exc) if fail_exc
                else FeedbackKind.SERVER_ERROR
            )
            await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
            if success:
                asyncio.create_task(
                    _quota_sync(token, selected_mode_id)
                ).add_done_callback(_log_task_exception)
            else:
                asyncio.create_task(
                    _fail_sync(token, selected_mode_id, fail_exc)
                ).add_done_callback(_log_task_exception)

    raise RateLimitError("No available accounts after retries")


__all__ = ["generate"]
