"""Priority-based timed event scheduler using a min-heap with monotonic clock tracking.

Provides deterministic scheduling, cooperative batch yielding, cancellation tokens,
and latency drift measurement for agent cron ticks and background task polling.
"""

from __future__ import annotations

import asyncio
import heapq
import inspect
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional, Union

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(order=True)
class _HeapItem:
    scheduled_time: float
    priority: int  # lower integer = higher priority
    sequence: int
    task_id: str = field(compare=False)


@dataclass(frozen=True)
class ScheduledTaskResult:
    task_id: str
    status: TaskStatus
    result: Any = None
    error: Optional[str] = None
    scheduled_time: float = 0.0
    execution_time: float = 0.0
    drift_seconds: float = 0.0
    duration_seconds: float = 0.0


class TimedPriorityScheduler:
    """Deterministic min-heap task scheduler backed by time.monotonic()."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._heap: list[_HeapItem] = []
        self._tasks: dict[str, dict[str, Any]] = {}
        self._seq: int = 0

    @property
    def pending_count(self) -> int:
        return len(self._tasks)

    def schedule(
        self,
        task_id: str,
        delay_seconds: float,
        fn: Callable[..., Any],
        *args: Any,
        priority: int = 10,
        **kwargs: Any,
    ) -> str:
        """Schedule a callable or coroutine function after delay_seconds."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError(f"task_id must be a non-empty string, got {task_id!r}")
        if not callable(fn):
            raise TypeError(f"fn must be callable, got {type(fn).__name__}")
        if not isinstance(delay_seconds, (int, float)) or isinstance(delay_seconds, bool) or delay_seconds < 0:
            raise ValueError(f"delay_seconds must be a non-negative float/int, got {delay_seconds!r}")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError(f"priority must be an integer, got {priority!r}")

        # If duplicate task_id exists and is pending, cancel previous
        if task_id in self._tasks:
            self.cancel(task_id)

        now = self._clock()
        scheduled_time = now + float(delay_seconds)
        self._seq += 1

        item = _HeapItem(
            scheduled_time=scheduled_time,
            priority=priority,
            sequence=self._seq,
            task_id=task_id,
        )
        self._tasks[task_id] = {
            "fn": fn,
            "args": args,
            "kwargs": kwargs,
            "scheduled_time": scheduled_time,
            "priority": priority,
            "cancelled": False,
        }
        heapq.heappush(self._heap, item)
        return task_id

    def cancel(self, task_id: str) -> bool:
        """Mark a pending task as cancelled."""
        task_info = self._tasks.get(task_id)
        if task_info is None or task_info["cancelled"]:
            return False
        task_info["cancelled"] = True
        del self._tasks[task_id]
        return True

    def is_scheduled(self, task_id: str) -> bool:
        return task_id in self._tasks and not self._tasks[task_id]["cancelled"]

    def peek_next_due_time(self) -> Optional[float]:
        """Return the scheduled monotonic timestamp of the next valid pending task, or None."""
        while self._heap:
            top = self._heap[0]
            task_info = self._tasks.get(top.task_id)
            if task_info is None or task_info["cancelled"]:
                heapq.heappop(self._heap)
            else:
                return top.scheduled_time
        return None

    def time_to_next(self) -> Optional[float]:
        """Seconds remaining until the next due task, clamped to >= 0.0, or None if idle."""
        next_due = self.peek_next_due_time()
        if next_due is None:
            return None
        return max(0.0, next_due - self._clock())

    async def execute_due(
        self,
        max_tasks: Optional[int] = None,
        now: Optional[float] = None,
    ) -> list[ScheduledTaskResult]:
        """Execute all due tasks whose scheduled_time <= current monotonic clock."""
        if max_tasks is not None and (isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks <= 0):
            raise ValueError(f"max_tasks must be a positive integer, got {max_tasks!r}")

        current_time = self._clock() if now is None else now
        results: list[ScheduledTaskResult] = []
        executed_count = 0

        while self._heap and (max_tasks is None or executed_count < max_tasks):
            top = self._heap[0]
            if top.scheduled_time > current_time:
                break

            heapq.heappop(self._heap)
            task_info = self._tasks.pop(top.task_id, None)
            if task_info is None or task_info["cancelled"]:
                continue

            executed_count += 1
            fn = task_info["fn"]
            args = task_info["args"]
            kwargs = task_info["kwargs"]
            sched_time = task_info["scheduled_time"]
            exec_start = self._clock()
            drift = max(0.0, exec_start - sched_time)

            status = TaskStatus.SUCCESS
            res_val = None
            err_msg = None

            try:
                if inspect.iscoroutinefunction(fn):
                    res_val = await fn(*args, **kwargs)
                else:
                    call_res = fn(*args, **kwargs)
                    if inspect.isawaitable(call_res):
                        res_val = await call_res
                    else:
                        res_val = call_res
            except Exception as exc:
                status = TaskStatus.FAILED
                err_msg = f"{type(exc).__name__}: {str(exc)}"
                logger.exception("Task %s failed during execution: %s", top.task_id, err_msg)

            exec_end = self._clock()
            duration = max(0.0, exec_end - exec_start)

            results.append(
                ScheduledTaskResult(
                    task_id=top.task_id,
                    status=status,
                    result=res_val,
                    error=err_msg,
                    scheduled_time=sched_time,
                    execution_time=exec_start,
                    drift_seconds=drift,
                    duration_seconds=duration,
                )
            )

        return results

    def clear(self) -> int:
        """Clear all pending tasks and reset heap."""
        count = len(self._tasks)
        self._tasks.clear()
        self._heap.clear()
        return count
