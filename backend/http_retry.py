"""Provider HTTP errors: what is sent again, how long to back off, and the error body kept for the receipt.

A 5xx is the provider having a moment: the call is sent again after a short backoff. A 429 is a rate limit:
the DM's model goes quiet for as long as the provider asks (Retry-After, or Google's retryDelay), else for an
exponential backoff, instead of being hammered by the game's re-asks. A planner, which nobody re-asks,
waits a short one out and tries again.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import re
import time
from typing import Awaitable, Callable

log = logging.getLogger("llmdm")

RETRY_STATUSES = frozenset({500, 502, 503, 504})
BODY_CHARS = 2000               # how much of an error response the receipt keeps
BACKOFF_MAX_S = 60.0            # our own backoff never waits longer than this
PROVIDER_WAIT_MAX_S = 300.0     # nor do we take a provider's Retry-After past this
# Google puts the wait in the body (google.rpc.RetryInfo), not in a Retry-After header.
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def backoff(attempt: int, base: float = 1.0) -> float:
    """Exponential backoff with jitter for the `attempt`th retry (0 = the first): between half and all of
    base * 2^attempt, capped, so calls that failed together don't all come back together."""
    d = min(BACKOFF_MAX_S, base * 2 ** max(0, attempt))
    return d / 2 + random.uniform(0, d / 2)


def body_text(resp) -> str | None:
    """The response body, truncated for the receipt (None if there is none or it can't be read)."""
    try:
        text = resp.text
    except Exception:                         # noqa: BLE001 - a stream not read, a fake response
        return None
    return text[:BODY_CHARS] if isinstance(text, str) and text else None


def retry_after(resp) -> float | None:
    """How long the provider asked us to wait, in seconds: a Retry-After header (seconds or an HTTP date),
    else Google's retryDelay in the body. None when it didn't say."""
    headers = getattr(resp, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    m = _RETRY_DELAY.search(body_text(resp) or "")
    return float(m.group(1)) if m else None


def raise_for_status(resp) -> None:
    """resp.raise_for_status(), keeping the response body (truncated) on the error as `body`: the status
    line alone doesn't say why a 400 was refused."""
    try:
        resp.raise_for_status()
    except Exception as e:
        e.body = body_text(resp)
        raise


def error_fields(e: BaseException) -> dict:
    """A failure as the receipt keeps it: the error line, and the provider's response body when there is one."""
    body = getattr(e, "body", None)
    return {"error": f"{type(e).__name__}: {e}", **({"body": body} if body else {})}


def error_line(e: BaseException, chars: int = 300) -> str:
    """The error for the log: its message, plus the start of the provider's response body."""
    body = getattr(e, "body", None)
    return f"{e} | {' '.join(body.split())[:chars]}" if body else str(e)


class CoolingDown(RuntimeError):
    """Not sent: the model is rate-limited. `retry_in` = seconds until it may be called again."""

    def __init__(self, retry_in: float) -> None:
        super().__init__(f"rate-limited, cooling down for {retry_in:.1f}s")
        self.retry_in = retry_in


class Cooldown:
    """One model's rate-limit window. A 429 that lands outside the current window is a strike, and each
    strike doubles our own backoff (a wait the provider names is taken as it is); a call that goes
    through clears them. 429s from calls already in flight extend the window but aren't strikes."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.until = 0.0
        self.strikes = 0

    def remaining(self) -> float:
        return max(0.0, self.until - time.monotonic())

    def check(self) -> None:
        left = self.remaining()
        if left > 0:
            raise CoolingDown(left)

    def hit(self, resp) -> float:
        """A 429 came back: start (or extend) the window. Returns how long it now runs."""
        fresh = self.remaining() <= 0
        if fresh:
            self.strikes += 1
        asked = retry_after(resp)
        wait = min(asked, PROVIDER_WAIT_MAX_S) if asked is not None else backoff(self.strikes - 1, base=2.0)
        self.until = max(self.until, time.monotonic() + wait)
        if fresh:
            log.warning("%s rate-limited (429) -> cooling down %.1fs (%s, strike %d)", self.name or "model",
                        self.remaining(), "as the provider asked" if asked is not None else "backoff", self.strikes)
        return self.remaining()

    def ok(self) -> None:
        self.strikes = 0


async def post_retrying(post: Callable[[], Awaitable], *, what: str, retries: int, base: float = 1.0,
                        wait_429_up_to: float = 0.0):
    """`post()` sent again after a backoff while the provider answers 5xx, up to `retries` more times; on a
    429 too when the wait it asks for (or our backoff) is at most `wait_429_up_to` seconds. Returns the last
    response: the caller raises on its status."""
    resp = None
    for attempt in range(retries + 1):
        resp = await post()
        status = getattr(resp, "status_code", 200)
        if attempt == retries:
            break
        if status in RETRY_STATUSES:
            delay = backoff(attempt, base)
        elif status == 429 and wait_429_up_to > 0:
            asked = retry_after(resp)
            delay = asked if asked is not None else backoff(attempt, base)
            if delay > wait_429_up_to:
                break
        else:
            break
        log.info("%-11s %d from the provider -> retry %d/%d in %.1fs", what, status, attempt + 1, retries, delay)
        await asyncio.sleep(delay)
    return resp
