import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

logger = logging.getLogger(__name__)

T = TypeVar("T")

_CACHE: dict[str, tuple[object, float]] = {}
_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
_REFRESH_TASKS: dict[tuple[str, int], asyncio.Task[None]] = {}


def _validate_ttl(ttl_seconds: float) -> float:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
        raise ValueError(f"ttl_seconds must be a number, got {type(ttl_seconds).__name__}")
    if not math.isfinite(ttl_seconds) or ttl_seconds < 0:
        raise ValueError(f"ttl_seconds must be a non-negative finite number, got {ttl_seconds!r}")
    return float(ttl_seconds)


def _now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


def _lock_for(key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock_key = (key, id(loop))
    lock = _LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[lock_key] = lock
    return lock


async def cached(key: str, ttl_seconds: float, loader: Callable[[], Awaitable[T]]) -> T:
    ttl = _validate_ttl(ttl_seconds)
    now = _now()
    entry = _CACHE.get(key)
    if entry is not None:
        value, expires_at = entry
        if expires_at > now:
            return cast(T, value)

    async with _lock_for(key):
        now = _now()
        entry = _CACHE.get(key)
        if entry is not None:
            value, expires_at = entry
            if expires_at > now:
                return cast(T, value)

        stale = entry[0] if entry is not None else None
        has_stale = entry is not None
        try:
            value = await loader()
        except Exception:
            if has_stale:
                logger.warning(
                    "TTL cache loader failed for %s; serving stale value", key, exc_info=True
                )
                return cast(T, stale)
            raise
        _CACHE[key] = (value, now + ttl)
        return value


async def _refresh_stale_entry(
    key: str, ttl_seconds: float, loader: Callable[[], Awaitable[object]]
) -> None:
    async with _lock_for(key):
        try:
            value = await loader()
        except Exception:
            logger.warning("TTL cache background refresh failed for %s", key, exc_info=True)
            return
        _CACHE[key] = (value, _now() + ttl_seconds)


def _schedule_refresh(
    key: str, ttl_seconds: float, loader: Callable[[], Awaitable[object]]
) -> None:
    loop = asyncio.get_running_loop()
    task_key = (key, id(loop))
    existing = _REFRESH_TASKS.get(task_key)
    if existing is not None and not existing.done():
        return

    task = asyncio.create_task(_refresh_stale_entry(key, ttl_seconds, loader))
    _REFRESH_TASKS[task_key] = task
    task.add_done_callback(lambda _task: _REFRESH_TASKS.pop(task_key, None))


async def cached_stale_while_revalidate(
    key: str,
    ttl_seconds: float,
    loader: Callable[[], Awaitable[T]],
) -> T:
    ttl = _validate_ttl(ttl_seconds)
    now = _now()
    entry = _CACHE.get(key)
    if entry is not None:
        value, expires_at = entry
        if expires_at > now:
            return cast(T, value)
        _schedule_refresh(key, ttl, loader)
        return cast(T, value)
    return await cached(key, ttl, loader)


def set_cached(key: str, value: object, ttl_seconds: float) -> None:
    ttl = _validate_ttl(ttl_seconds)
    _CACHE[key] = (value, _now() + ttl)


def invalidate(key: str) -> None:
    _CACHE.pop(key, None)


def clear() -> None:
    _CACHE.clear()
    _LOCKS.clear()
    for task in _REFRESH_TASKS.values():
        try:
            task.cancel()
        except RuntimeError:
            pass
    _REFRESH_TASKS.clear()
