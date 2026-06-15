"""HTTP client wrapper used by the probe worker.

Reuses the production transport (curl_cffi impersonation, proxy lease, headers)
so a successful probe is a true end-to-end signal.  On every call we record
``ttfb_ms`` and ``total_ms`` along with the HTTP status.  The result is
returned to the runner which persists it to the account record.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import orjson

from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import (
    ResettableSession,
    build_session_kwargs,
)
from app.dataplane.reverse.protocol.xai_chat import build_chat_payload
from app.dataplane.reverse.runtime.endpoint_table import CHAT
from app.control.model.enums import ModeId
from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger


@dataclass(slots=True)
class ProbeResult:
    """Result of a single probe request."""

    token:        str
    status:       int           # HTTP status code, 0 = transport error
    ttfb_ms:      int           # time-to-first-byte (ms); 0 if no body read
    total_ms:     int           # wall-clock time (ms)
    ok:           bool          # True when status == 200 and SSE stream opened

    @classmethod
    def from_error(cls, token: str, total_ms: int) -> "ProbeResult":
        return cls(token=token, status=0, ttfb_ms=0, total_ms=total_ms, ok=False)


async def measure_chat(
    token: str,
    *,
    model: str,
    max_tokens: int,
    timeout_s: float,
) -> ProbeResult:
    """Fire one real ``/rest/app-chat/conversations/new`` request.

    Uses ``streaming=True`` so we can read the first SSE byte (TTFB) and
    cancel as soon as we have it — keeping the probe cheap.

    Args:
        token:     The SSO token to test.
        model:     Public model name (e.g. ``grok-4.20-fast``).
        max_tokens: Forwarded as ``maxTokens`` model config override.
        timeout_s: Per-request timeout in seconds.
    """
    proxy = await get_proxy_runtime()
    try:
        lease = await proxy.acquire()
    except Exception as exc:
        logger.debug("probe proxy acquire failed: token={}... err={}", token[:10], exc)
        return ProbeResult.from_error(token, 0)

    model_config_override: dict[str, Any] = {
        "modelMap": {
            "chatModelConfig": {
                "maxTokens": max_tokens,
            },
        },
    }

    payload = build_chat_payload(
        message="hi",
        mode_id=ModeId.FAST,
        model_config_override=model_config_override,
    )
    payload_bytes = orjson.dumps(payload)
    headers = build_http_headers(
        token,
        content_type="application/json",
        origin="https://grok.com",
        referer="https://grok.com/",
        lease=lease,
    )
    session_kwargs = build_session_kwargs(lease=lease)

    t_start = time.perf_counter()
    t_first_byte = 0
    status = 0
    try:
        async with ResettableSession(**session_kwargs) as session:
            try:
                response = await session.post(
                    CHAT,
                    headers=headers,
                    data=payload_bytes,
                    timeout=timeout_s,
                    stream=True,
                )
            except Exception as exc:
                logger.debug("probe transport error: token={}... err={}", token[:10], exc)
                return ProbeResult.from_error(token, _elapsed_ms(t_start))

            status = int(response.status_code)
            if status != 200:
                # Drain a small excerpt for logging, then bail.
                try:
                    body = response.content.decode("utf-8", "replace")[:200]
                except Exception:
                    body = ""
                logger.debug(
                    "probe upstream non-200: token={}... status={} body={}",
                    token[:10], status, body,
                )
                return ProbeResult(
                    token=token,
                    status=status,
                    ttfb_ms=_elapsed_ms(t_start),
                    total_ms=_elapsed_ms(t_start),
                    ok=False,
                )

            # Read the first chunk then close — we don't need the body.
            try:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        t_first_byte = time.perf_counter()
                        break
            except Exception as exc:
                logger.debug("probe read first byte failed: token={}... err={}", token[:10], exc)
                return ProbeResult(
                    token=token,
                    status=status,
                    ttfb_ms=_elapsed_ms(t_start),
                    total_ms=_elapsed_ms(t_start),
                    ok=False,
                )

        return ProbeResult(
            token=token,
            status=status,
            ttfb_ms=_elapsed_ms(t_start) if t_first_byte == 0
                    else int((t_first_byte - t_start) * 1000),
            total_ms=_elapsed_ms(t_start),
            ok=True,
        )
    except asyncio.TimeoutError:
        logger.debug("probe timeout: token={}... timeout_s={}", token[:10], timeout_s)
        return ProbeResult.from_error(token, int(timeout_s * 1000))
    except UpstreamError as exc:
        return ProbeResult(
            token=token,
            status=int(getattr(exc, "status", 0) or 0),
            ttfb_ms=0,
            total_ms=_elapsed_ms(t_start),
            ok=False,
        )
    except Exception as exc:
        logger.debug("probe unexpected error: token={}... err={}", token[:10], exc)
        return ProbeResult.from_error(token, _elapsed_ms(t_start))


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


__all__ = ["ProbeResult", "measure_chat"]
