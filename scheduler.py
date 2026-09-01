"""Unified queue scheduler — pure selection logic (spec:
docs/superpowers/plans/2026-09-01-unified-queue-scheduler-spec.md).

All state comes in as arguments; this module never imports main (hot-reload-safe,
unit-testable). Three rules replace priority/speed routing everywhere:
unpaid-then-fastest ordering, freed-backend type affinity, and an overdue guard.
"""
from typing import Callable, Iterable, Optional

EMA_ALPHA = 0.3


def ema(prev: Optional[float], sample: float, alpha: float = EMA_ALPHA) -> float:
    """Exponential moving average; the first sample is taken verbatim."""
    return sample if prev is None else alpha * sample + (1 - alpha) * prev


def order_ready(cands: list, speed_of: Callable, paid_of: Callable) -> list:
    """Order ready (backend, x) candidates for dispatch: unpaid before paid, then
    fastest first. speed_of(backend, x) is higher-is-better; float('inf') marks an
    unmeasured candidate, which therefore sorts first within its tier (probe-once).
    Stable, so equal candidates keep their incoming order."""
    return sorted(cands, key=lambda bx: (bool(paid_of(bx[0])), -speed_of(bx[0], bx[1])))


def designated_taker(pool: Iterable, can_serve: Callable, type_key: Callable,
                     last_key: Optional[str], now: float, max_wait_s: float):
    """The waiting entry a freed backend should take, or None.

    pool iterates in enqueue order (oldest first); entries expose ["enqueued_at"]
    (monotonic seconds). Rules, first match wins:
      1. overdue entries (waited > max_wait_s) the backend can serve — oldest first;
      2. entries whose type key equals what the backend last ran — oldest first;
      3. the oldest entry the backend can serve.
    """
    servable = [e for e in pool if can_serve(e)]
    if not servable:
        return None
    overdue = [e for e in servable if now - e["enqueued_at"] > max_wait_s]
    if overdue:
        return overdue[0]
    if last_key:
        same = [e for e in servable if type_key(e) == last_key]
        if same:
            return same[0]
    return servable[0]
