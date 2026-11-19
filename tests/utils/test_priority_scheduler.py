import asyncio
import pytest

from agent.utils.priority_scheduler import (
    ScheduledTaskResult,
    TaskStatus,
    TimedPriorityScheduler,
)


class MockClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.current_time = start

    def __call__(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


def test_schedule_and_execute_order_by_time_and_priority():
    clock = MockClock(100.0)
    scheduler = TimedPriorityScheduler(clock=clock)

    events = []

    scheduler.schedule("t3", 10.0, lambda: events.append("t3"), priority=1)
    scheduler.schedule("t1", 5.0, lambda: events.append("t1_low"), priority=20)
    scheduler.schedule("t2", 5.0, lambda: events.append("t1_high"), priority=5)

    assert scheduler.pending_count == 3
    assert scheduler.peek_next_due_time() == 105.0

    # Advance time to 102.0: nothing due yet
    clock.advance(2.0)
    results = asyncio.run(scheduler.execute_due())
    assert len(results) == 0
    assert len(events) == 0

    # Advance to 105.0: t2 (priority 5) executes before t1 (priority 20)
    clock.advance(3.0)
    results = asyncio.run(scheduler.execute_due())
    assert len(results) == 2
    assert [r.task_id for r in results] == ["t2", "t1"]
    assert events == ["t1_high", "t1_low"]

    # Advance to 110.0: t3 executes
    clock.advance(5.0)
    results = asyncio.run(scheduler.execute_due())
    assert len(results) == 1
    assert results[0].task_id == "t3"
    assert events == ["t1_high", "t1_low", "t3"]
    assert scheduler.pending_count == 0


def test_task_cancellation():
    clock = MockClock(50.0)
    scheduler = TimedPriorityScheduler(clock=clock)

    scheduler.schedule("job1", 2.0, lambda: "done1")
    scheduler.schedule("job2", 4.0, lambda: "done2")

    assert scheduler.is_scheduled("job1")
    assert scheduler.is_scheduled("job2")

    cancelled = scheduler.cancel("job1")
    assert cancelled is True
    assert not scheduler.is_scheduled("job1")
    assert scheduler.cancel("job1") is False

    clock.advance(5.0)
    results = asyncio.run(scheduler.execute_due())
    assert len(results) == 1
    assert results[0].task_id == "job2"
    assert results[0].result == "done2"


def test_async_task_execution_and_drift():
    clock = MockClock(200.0)
    scheduler = TimedPriorityScheduler(clock=clock)

    async def async_handler(val: int):
        return val * 42

    scheduler.schedule("async_job", 1.0, async_handler, 2)
    clock.advance(3.5)  # 2.5s drift past scheduled 201.0

    results = asyncio.run(scheduler.execute_due())
    assert len(results) == 1
    res = results[0]
    assert res.task_id == "async_job"
    assert res.status == TaskStatus.SUCCESS
    assert res.result == 84
    assert res.scheduled_time == 201.0
    assert res.drift_seconds == 2.5


def test_exception_isolation():
    clock = MockClock(10.0)
    scheduler = TimedPriorityScheduler(clock=clock)

    def failing_fn():
        raise RuntimeError("Something exploded")

    scheduler.schedule("faulty", 0.0, failing_fn, priority=1)
    scheduler.schedule("healthy", 0.0, lambda: "recovered", priority=2)

    results = asyncio.run(scheduler.execute_due())
    assert len(results) == 2

    assert results[0].task_id == "faulty"
    assert results[0].status == TaskStatus.FAILED
    assert "RuntimeError: Something exploded" in results[0].error

    assert results[1].task_id == "healthy"
    assert results[1].status == TaskStatus.SUCCESS
    assert results[1].result == "recovered"


def test_cooperative_batch_limit():
    clock = MockClock(0.0)
    scheduler = TimedPriorityScheduler(clock=clock)

    for i in range(5):
        scheduler.schedule(f"task_{i}", 0.0, lambda idx=i: idx)

    results_batch1 = asyncio.run(scheduler.execute_due(max_tasks=2))
    assert len(results_batch1) == 2
    assert scheduler.pending_count == 3

    results_batch2 = asyncio.run(scheduler.execute_due(max_tasks=2))
    assert len(results_batch2) == 2
    assert scheduler.pending_count == 1

    results_batch3 = asyncio.run(scheduler.execute_due())
    assert len(results_batch3) == 1
    assert scheduler.pending_count == 0


def test_validation_and_clear():
    scheduler = TimedPriorityScheduler()

    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        scheduler.schedule("", 1.0, lambda: None)

    with pytest.raises(TypeError, match="fn must be callable"):
        scheduler.schedule("id1", 1.0, "not_a_function")

    with pytest.raises(ValueError, match="delay_seconds must be a non-negative"):
        scheduler.schedule("id2", -0.5, lambda: None)

    with pytest.raises(TypeError, match="priority must be an integer"):
        scheduler.schedule("id3", 1.0, lambda: None, priority="high")

    with pytest.raises(ValueError, match="max_tasks must be a positive integer"):
        asyncio.run(scheduler.execute_due(max_tasks=0))

    scheduler.schedule("task_a", 10.0, lambda: None)
    scheduler.schedule("task_b", 20.0, lambda: None)
    assert scheduler.pending_count == 2
    cleared = scheduler.clear()
    assert cleared == 2
    assert scheduler.pending_count == 0
    assert scheduler.peek_next_due_time() is None
    assert scheduler.time_to_next() is None
