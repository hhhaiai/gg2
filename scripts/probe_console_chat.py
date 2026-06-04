"""Probe console.x.ai/v1/responses (chat) with the same SSO token to check
whether 'User is blocked' is account-wide or endpoint-specific.

Same SSO loading as probe_console_images.py so we can compare apples to apples.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import orjson
from curl_cffi.requests import AsyncSession

ROOT = Path(__file__).resolve().parent.parent
SSO_PATH = ROOT / "sso.txt"
CONSOLE_RESPONSES = "https://console.x.ai/v1/responses"
PROMPT = "say hi in one word"
TIMEOUT_S = 30.0


def build_headers(token: str) -> dict[str, str]:
    tok = token[4:] if token.startswith("sso=") else token
    return {
        "Authorization":   "Bearer anonymous",
        "Content-Type":    "application/json",
        "Accept":          "*/*",
        "Cookie":          f"sso={tok}; sso-rw={tok}",
        "Origin":          "https://console.x.ai",
        "Referer":         "https://console.x.ai/",
        "x-cluster":       "https://us-east-1.api.x.ai",
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/136.0.0.0 Safari/537.36",
    }


async def probe(token: str) -> tuple[int, str]:
    payload = {
        "model": "grok-4-fast",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": PROMPT}]}
        ],
        "stream": False,
        "reasoning": {"effort": "low"},
    }
    body = orjson.dumps(payload)
    async with AsyncSession(impersonate="chrome120") as session:
        try:
            resp = await session.post(
                CONSOLE_RESPONSES,
                headers=build_headers(token),
                data=body,
                timeout=TIMEOUT_S,
            )
        except Exception as exc:
            return 0, f"transport error: {type(exc).__name__}: {exc}"
        return resp.status_code, resp.text[:500] if resp.text else ""


async def main() -> int:
    token_idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    tokens = [t.strip() for t in SSO_PATH.read_text().splitlines() if t.strip()]
    token = tokens[token_idx]
    print(f"using token #{token_idx} (len={len(token)}, prefix={token[:8]}...)")
    status, body = await probe(token)
    print(f"status={status}")
    print(f"body[:500]={body!r}")
    if status == 200:
        print("✓ chat works → user has console access → image endpoint is the issue")
    elif status in (401, 403):
        print("✗ user blocked from console entirely")
    elif status == 429:
        print("✗ rate-limited")
    else:
        print(f"? unexpected status {status}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
