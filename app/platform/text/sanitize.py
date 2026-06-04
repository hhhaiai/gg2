"""Shared text sanitisation helpers.

These were previously duplicated across the account model validator
and the admin token endpoints, with the same Unicode-dash / smart-quote /
zero-width-character replacement tables and the same ``sso=`` prefix
stripping logic drifting independently in two places. The two callers
have been unified onto :func:`sanitize_token` so the rules stay in lockstep.

For broader config-value sanitisation (smart quotes, latin-1 fallback) the
admin module still has its own ``_sanitize_text`` — different semantics.
"""

from __future__ import annotations

from typing import Any

# Translation table: Unicode dash variants, narrow/figure spaces, and zero-
# width characters. Used by both the Pydantic AccountRecord.token validator
# and the admin token endpoints.
_TOKEN_TRANS = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        " ": " ",
        " ": " ",
        "​": "",
        "‌": "",
        "‍": "",
        "﻿": "",
    }
)


def sanitize_token(value: Any) -> str:
    """Normalise an account token to its canonical form.

    Steps applied (in order):
      1. Coerce to ``str`` (``None`` becomes ``""`` so callers can choose
         to reject it).
      2. Replace Unicode dash / space / zero-width variants.
      3. Collapse and strip all whitespace.
      4. Drop a leading ``sso=`` prefix (cookie format).
      5. ASCII-encode (non-ASCII bytes are silently dropped — they're
         never part of a valid token).

    Returns the normalised string. Callers decide whether an empty result
    is an error: the Pydantic validator raises ``ValueError`` if so; the
    admin endpoints filter empties out at the request boundary.
    """
    if value is None:
        return ""
    token = str(value).translate(_TOKEN_TRANS)
    token = "".join(token.split())
    if token.startswith("sso="):
        token = token[4:]
    return token.encode("ascii", errors="ignore").decode("ascii")


__all__ = ["sanitize_token", "_TOKEN_TRANS"]
