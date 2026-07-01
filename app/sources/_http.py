"""Small HTTP helpers shared by the source adapters.

``get_json`` / ``get_text`` / ``post_json`` swallow network and decode errors and
return None so that an optional source can degrade gracefully. The mandatory
price source does NOT use the swallowing variants for its primary fetch — it lets
failures surface.

Every optional layer is hit once per 6h run, so a single transient blip (a
timeout, a dropped connection, a 5xx, or a 429 rate-limit) used to darken a whole
scoring category for the entire run (renormalization then silently reweights).
These helpers now retry a couple of times with exponential backoff + jitter for
*transient* failures, honoring a 429 ``Retry-After`` (capped so a run can't hang).
The fail-soft contract is unchanged: after exhausting retries they still return
None rather than raising.

Two extra shared facilities live here because every source adapter needs them:
an optional per-process network **deadline** (``set_deadline``) so a collector
can bound its aggregate wall-clock across many sequential source calls, and a
**freshness** check (``is_stale``) so daily-cadence sources treat a frozen
upstream's last value as missing instead of scoring it as current forever.
"""
from __future__ import annotations

import logging
import random
import time

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20

# Retry policy for transient failures. Kept small so a single source can never
# dominate a run's wall-clock: worst case ~ (0.5 + 1.0) s of backoff + the
# per-attempt timeouts.
_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 0.5          # seconds; doubled each retry
_MAX_BACKOFF = 4.0           # cap on a single backoff sleep
_MAX_RETRY_AFTER = 10.0      # cap on honoring a server's Retry-After (s)
# HTTP statuses worth retrying: rate-limit + server-side transient errors.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# --- Per-process deadline budget ------------------------------------------------
# Each _request is individually bounded (attempts x timeout + backoff), but a
# collector makes ~7-9 sequential source calls, so the AGGREGATE had no ceiling —
# a network brown-out could push the 10-minute collector past its own cadence.
# An entrypoint may arm a wall-clock deadline; once it expires, further requests
# are skipped (fail-soft None) and per-attempt timeouts are clamped to the time
# remaining. No deadline armed (the default) = behavior unchanged.
_deadline_at: float | None = None


def set_deadline(seconds: float | None) -> None:
    """Arm a per-process network deadline ``seconds`` from now (None = clear)."""
    global _deadline_at
    _deadline_at = None if seconds is None else time.monotonic() + float(seconds)


def remaining_budget() -> float | None:
    """Seconds left before the armed deadline; None when no deadline is armed."""
    if _deadline_at is None:
        return None
    return _deadline_at - time.monotonic()


# --- Freshness guard for daily-cadence sources -----------------------------------
DEFAULT_FRESHNESS_DAYS = 3.0


def freshness_budget_days() -> float:
    """Staleness budget (days) for daily-cadence sources, from config.

    A frozen upstream that keeps serving its last value is worse than a dead one:
    a None renormalizes away and flips active_cats, while a stale value silently
    pins a category at an outdated level with zero health signal. Sources compare
    their payload's own date field against this budget and return None past it.
    """
    try:
        from ..config import load_config
        return float(load_config().freshness_budget_days)
    except Exception:  # noqa: BLE001 - config must never break a source read
        return DEFAULT_FRESHNESS_DAYS


def is_stale(ts_epoch_seconds: float, budget_days: float | None = None) -> bool:
    """True when a reading's timestamp (epoch SECONDS, UTC) exceeds the budget."""
    budget = freshness_budget_days() if budget_days is None else budget_days
    return (time.time() - float(ts_epoch_seconds)) > budget * 86_400.0


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with full jitter for the given (0-based) attempt."""
    base = min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** attempt))
    time.sleep(random.uniform(0, base))


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse a Retry-After header (delta-seconds form), capped. None if absent/odd.

    Only the integer-seconds form is honored; the HTTP-date form is ignored (and
    falls back to normal backoff) to keep this dependency-free and bounded.
    """
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
    except (TypeError, ValueError):
        return None
    if secs < 0:
        return None
    return min(secs, _MAX_RETRY_AFTER)


def _request(method: str, url: str, *, params=None, headers=None,
             json_body=None, timeout: int = DEFAULT_TIMEOUT):
    """Shared request loop. Returns a successful ``requests.Response`` or None.

    Retries transient failures (timeouts, connection errors, retryable HTTP
    statuses) up to ``_MAX_ATTEMPTS`` with backoff; never raises. Honors the
    per-process deadline (``set_deadline``): once exhausted, attempts (including
    retries) are skipped and the per-attempt timeout is clamped to what's left.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        budget = remaining_budget()
        if budget is not None and budget <= 0:
            log.warning("%s %s skipped: per-process network deadline exhausted",
                        method, url)
            return None
        attempt_timeout = timeout if budget is None else min(timeout, budget)
        try:
            r = requests.request(method, url, params=params, headers=headers,
                                 json=json_body, timeout=attempt_timeout)
            if r.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS - 1:
                wait = _retry_after_seconds(r) if r.status_code == 429 else None
                log.info("%s %s -> HTTP %s; retrying (attempt %d/%d)",
                         method, url, r.status_code, attempt + 1, _MAX_ATTEMPTS)
                if wait is not None:
                    time.sleep(wait)
                else:
                    _sleep_backoff(attempt)
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Transient network failure: back off and retry.
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                log.info("%s %s transient error (%s); retrying (attempt %d/%d)",
                         method, url, exc, attempt + 1, _MAX_ATTEMPTS)
                _sleep_backoff(attempt)
                continue
            break
        except Exception as exc:  # noqa: BLE001 - graceful degradation is the contract
            # Non-transient (4xx other than 429, decode/SSL/etc.): don't retry.
            last_exc = exc
            break
    if last_exc is not None:
        log.warning("%s %s failed: %s", method, url, last_exc)
    return None


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = DEFAULT_TIMEOUT):
    """GET and parse JSON; return None on any network/HTTP/decode error.

    Retries transient failures (timeouts, connection errors, 5xx, 429) a couple
    of times with backoff before giving up.
    """
    r = _request("GET", url, params=params, headers=headers, timeout=timeout)
    if r is None:
        return None
    try:
        return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("GET %s JSON decode failed: %s", url, exc)
        return None


def get_text(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """GET and return body text; return None on any network/HTTP error.

    Retries transient failures the same way ``get_json`` does.
    """
    r = _request("GET", url, params=params, headers=headers, timeout=timeout)
    if r is None:
        return None
    try:
        return r.text
    except Exception as exc:  # noqa: BLE001
        log.warning("GET %s text read failed: %s", url, exc)
        return None


def post_json(url: str, json_body: dict | None = None, params: dict | None = None,
              headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT):
    """POST a JSON body and parse the JSON response; None on any failure.

    Mirrors ``get_json``'s swallow-and-return-None contract (and its retry/backoff
    behavior). Used by sources whose endpoints are POST-only (e.g. SoSoValue).
    """
    r = _request("POST", url, params=params, headers=headers,
                 json_body=json_body, timeout=timeout)
    if r is None:
        return None
    try:
        return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("POST %s JSON decode failed: %s", url, exc)
        return None
