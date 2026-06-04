"""Probe console.x.ai/v1/images/generations with a real SSO token.

Standalone smoke test — does NOT bootstrap the app runtime. Uses
curl-cffi directly to bypass proxy lease / account table plumbing
so we can isolate the question: "does this endpoint actually exist?"

Run:
    .venv/bin/python scripts/probe_console_images.py [token_index]

Default token index = 0 (first line of sso.txt).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import orjson
from curl_cffi.requests import AsyncSession

ROOT = Path(__file__).resolve().parent.parent
SSO_PATH = ROOT / "sso.txt"
CONSOLE_IMAGES = "https://console.x.ai/v1/images/generations"

# Models to probe (in order — lite first because it's the most likely
# to be the smallest/cheapest and thus the most likely to exist on free tier).
PROBE_MODELS = [
    "grok-imagine-image-lite-console",  # → console field: grok-imagine-image-lite
    "grok-imagine-image-console",        # → console field: grok-imagine-image
    "grok-imagine-image-pro-console",    # → console field: grok-imagine-image-pro
]

CONSOLE_MODEL_MAP = {
    "grok-imagine-image-lite-console": "grok-imagine-image-lite",
    "grok-imagine-image-console":      "grok-imagine-image",
    "grok-imagine-image-pro-console":  "grok-imagine-image-pro",
}

PROMPT = "a red apple on a white table, studio lighting, product photo"
TIMEOUT_S = 30.0


def build_headers(token: str) -> dict[str, str]:
    """Mirror build_console_headers in app/dataplane/proxy/adapters/headers.py
    but skip the proxy lease part (no proxy in this probe)."""
    return {
        "Authorization":   "Bearer anonymous",
        "Content-Type":    "application/json",
        "Accept":          "application/json",
        "Cookie":          f"sso={token}; sso-rw={token}",
        "Origin":          "https://console.x.ai",
        "Referer":         "https://console.x.ai/",
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
    }


async def probe_one(token: str, grok2api_name: str) -> tuple[int, str]:
    console_model = CONSOLE_MODEL_MAP[grok2api_name]
    payload = {
        "model":           console_model,
        "prompt":          PROMPT,
        "n":               1,
        "size":            "1024x1024",
        "response_format": "url",
    }
    body = orjson.dumps(payload)
    async with AsyncSession(impersonate="chrome120") as session:
        try:
            resp = await session.post(
                CONSOLE_IMAGES,
                headers=build_headers(token),
                data=body,
                timeout=TIMEOUT_S,
            )
        except Exception as exc:
            return 0, f"transport error: {type(exc).__name__}: {exc}"

        text = resp.text[:500] if resp.text else ""
        return resp.status_code, text


async def main() -> int:
    token_idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if not SSO_PATH.exists():
        print(f"ERROR: {SSO_PATH} not found", file=sys.stderr)
        return 2
    tokens = [t.strip() for t in SSO_PATH.read_text().splitlines() if t.strip()]
    if token_idx >= len(tokens):
        print(f"ERROR: token index {token_idx} out of range (have {len(tokens)})", file=sys.stderr)
        return 2
    token = tokens[token_idx]
    print(f"using token #{token_idx} (len={len(token)}, prefix={token[:8]}...)")

    for grok2api_name in PROBE_MODELS:
        print(f"\n--- probe: {grok2api_name} → console field: {CONSOLE_MODEL_MAP[grok2api_name]} ---")
        status, body = await probe_one(token, grok2api_name)
        print(f"status={status}")
        print(f"body[:500]={body!r}")
        if status == 200:
            print("✓ endpoint works for this model")
            try:
                j = orjson.loads(body)
                if "data" in j and j["data"]:
                    first = j["data"][0]
                    print(f"  first image: {list(first.keys())}")
                    if "url" in first:
                        print(f"  url={first['url']}")
            except Exception as e:
                print(f"  (could not parse JSON: {e})")
            return 0
        elif status in (401, 403):
            print("✗ auth/permission — try a different token")
        elif status == 404:
            print("✗ endpoint not found at this path (or model not exposed)")
        elif status == 429:
            print("✗ rate-limited — try a different token or wait")
        elif status in (400, 422):
            print("✗ model field rejected — endpoint exists but model name may be wrong")
        else:
            print(f"✗ unexpected status {status}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
