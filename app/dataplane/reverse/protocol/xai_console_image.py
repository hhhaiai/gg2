"""XAI console.x.ai image generation protocol.

端点: POST https://console.x.ai/v1/images/generations
认证: Authorization: Bearer anonymous  +  Cookie: sso=<token>; sso-rw=<token>

请求格式 (OpenAI Images API 兼容):
{
    "model":  "grok-imagine-image",
    "prompt": "...",
    "n":      1,
    "size":   "1024x1024",
    "response_format": "url" | "b64_json"
}

响应格式 (OpenAI Images API 兼容):
{
    "created": <unix>,
    "data": [
        {"url": "https://..."},
        {"b64_json": "..."}
    ]
}

验证结果(2026-06-04,用真实 accounts.db 中的 SSO token):
- URL /v1/images/generations 存在且可达(到达 xAI API 网关,返回结构化错误)
- Bearer anonymous + sso/sso-rw cookie 认证通过(同一 token 走 /v1/responses 200)
- 三个模型字段 grok-imagine-image-lite / grok-imagine-image / grok-imagine-image-pro
  均被 API 接受(返回结构化 429 而不是 400/422)
- 但 xAI 上游对 image 端点施加了独立且更严格的限速,免费层账号当前返回
  429 "Some resource has been exhausted"。原因可能是:
  * 免费层图片生成默认配额为 0(需 X Premium 开通)
  * 或 IP/全局级 image 限速(同一 token chat 200 但 image 429)
  验证时未触发任何 4xx 业务错误,只有上游限速。
"""

from __future__ import annotations

from typing import Any

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger


# ---------------------------------------------------------------------------
# Model name mapping: grok2api public name → console.x.ai real model field
# ---------------------------------------------------------------------------

CONSOLE_IMAGE_MODELS: dict[str, str] = {
    "grok-imagine-image-lite-console": "grok-imagine-image-lite",
    "grok-imagine-image-console":      "grok-imagine-image",
    "grok-imagine-image-pro-console":  "grok-imagine-image-pro",
}

# size ↔ console.x.ai size 字段 (X 系列通常 1024x1024, 1024x1536, 1536x1024)
# 透传用户传入的 size,如不识别由上游拒绝 (避免静默改 size 影响用户预期)


# ---------------------------------------------------------------------------
# Single-call completion
# ---------------------------------------------------------------------------


async def generate_via_console(
    token: str,
    *,
    model: str,
    prompt: str,
    n: int = 1,
    size: str = "1024x1024",
    response_format: str = "url",
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """POST to console.x.ai/v1/images/generations and return the OpenAI Images
    API-compatible response dict.

    The caller is expected to wrap this in a single-token reserve/release cycle
    and to translate UpstreamError into the project's standard failure flow.
    """
    from app.dataplane.proxy import get_proxy_runtime
    from app.dataplane.proxy.adapters.headers import build_console_headers
    from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
    from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_IMAGES

    console_model = CONSOLE_IMAGE_MODELS.get(model, model)

    payload: dict[str, Any] = {
        "model":           console_model,
        "prompt":          prompt,
        "n":               max(1, min(int(n), 4)),
        "size":            size,
        "response_format": response_format,
    }
    payload_bytes = orjson.dumps(payload)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    headers = build_console_headers(token, lease=lease)
    session_kwargs = build_session_kwargs(lease=lease)

    async with ResettableSession(**session_kwargs) as session:
        try:
            response = await session.post(
                CONSOLE_IMAGES,
                headers=headers,
                data=payload_bytes,
                timeout=timeout_s,
            )
        except Exception as exc:
            await proxy.feedback(lease, _transport_error_feedback())
            raise UpstreamError(
                f"Console images transport failed: {exc}", status=502
            ) from exc

        if response.status_code != 200:
            try:
                body = response.content.decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            await proxy.feedback(lease, _status_feedback(response.status_code))
            logger.warning(
                "console image generation failed: model={} status={} body={}",
                model,
                response.status_code,
                body[:200],
            )
            raise UpstreamError(
                f"Console images API returned {response.status_code}: {body}",
                status=response.status_code,
                body=body,
            )

        try:
            result = orjson.loads(response.content)
        except Exception as exc:
            await proxy.feedback(lease, _transport_error_feedback())
            raise UpstreamError(
                f"Console images returned non-JSON body: {exc}", status=502
            ) from exc

        await proxy.feedback(lease, _success_feedback())
        return result


# ---------------------------------------------------------------------------
# Proxy feedback helpers — same shape as xai_console_chat uses
# ---------------------------------------------------------------------------


def _success_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200)


def _status_feedback(status: int):
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    if status == 403:
        kind = ProxyFeedbackKind.CHALLENGE
    elif status == 429:
        kind = ProxyFeedbackKind.RATE_LIMITED
    elif status >= 500:
        kind = ProxyFeedbackKind.UPSTREAM_5XX
    else:
        kind = ProxyFeedbackKind.FORBIDDEN
    return ProxyFeedback(kind=kind, status_code=status)


def _transport_error_feedback():
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    return ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR)


__all__ = [
    "CONSOLE_IMAGE_MODELS",
    "generate_via_console",
]
