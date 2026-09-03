from __future__ import annotations

import contextvars
import time
from typing import Any, Callable, Optional

# asyncio.to_thread() propagates contextvars, so the Telegram layer can provide
# a lightweight notifier without coupling the extractor modules to Telegram.
_retry_notifier: contextvars.ContextVar[Optional[Callable[[int, int, BaseException], None]]] = contextvars.ContextVar(
    "mtb_ai_retry_notifier", default=None
)


def set_retry_notifier(callback: Optional[Callable[[int, int, BaseException], None]]):
    return _retry_notifier.set(callback)


def reset_retry_notifier(token) -> None:
    _retry_notifier.reset(token)


def is_high_demand_error(exc: BaseException) -> bool:
    """Return True only for transient model-capacity / service-unavailable errors.

    We deliberately do not retry authentication, invalid request/schema, quota, or
    other permanent failures forever.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if "this model is currently experiencing high demand" in text:
        return True
    if "503" in text and ("unavailable" in text or "high demand" in text or "service unavailable" in text):
        return True
    if "status" in text and "unavailable" in text and "high demand" in text:
        return True
    return False


def _delay_for_attempt(attempt: int) -> int:
    # Fast retry first, then gently back off. Keep the ceiling short enough that
    # the operator does not wait minutes between temporary capacity spikes.
    schedule = (4, 6, 8, 10, 12, 15, 18, 20, 25, 30)
    return schedule[min(max(attempt, 1) - 1, len(schedule) - 1)]


def call_with_high_demand_retry(call: Callable[[], Any]) -> Any:
    """Run a Gemini request, retrying 503/high-demand failures until it succeeds.

    Other errors are raised immediately. The retry notifier, when set by bot.py,
    is invoked from the worker thread with (attempt, delay_seconds, exception).
    """
    attempt = 0
    max_retries = max(0, int(__import__('os').getenv('AI_HIGH_DEMAND_MAX_RETRIES', '2')))
    while True:
        try:
            return call()
        except Exception as exc:
            if not is_high_demand_error(exc):
                raise
            attempt += 1
            if attempt > max_retries:
                raise RuntimeError(
                    "AI service remained busy after bounded retries. Please process the same supplier file again."
                ) from exc
            delay = _delay_for_attempt(attempt)
            notifier = _retry_notifier.get()
            if notifier is not None:
                try:
                    notifier(attempt, delay, exc)
                except Exception:
                    pass
            time.sleep(delay)
