import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from src.config.constants import RegistrationWaitStrategy
from src.web.routes import registration as registration_routes


@contextmanager
def _fake_get_db():
    yield None


def _patch_batch_dependencies(monkeypatch):
    monkeypatch.setattr(registration_routes, "get_db", _fake_get_db)
    monkeypatch.setattr(
        registration_routes.crud,
        "get_registration_task",
        lambda db, uuid: SimpleNamespace(status="completed", error_message=None),
    )
    monkeypatch.setattr(registration_routes.task_manager, "init_batch", lambda batch_id, total: None)
    monkeypatch.setattr(registration_routes.task_manager, "add_batch_log", lambda batch_id, message: None)
    monkeypatch.setattr(registration_routes.task_manager, "update_batch_status", lambda batch_id, **kwargs: None)
    monkeypatch.setattr(registration_routes.task_manager, "is_batch_cancelled", lambda batch_id: False)


def _run_pipeline(monkeypatch, wait_strategy: str, wait_seconds: int):
    _patch_batch_dependencies(monkeypatch)
    registration_routes.batch_tasks.clear()

    events = []
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def fake_run_registration_task(uuid, *args, **kwargs):
        events.append(("run", uuid))

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(registration_routes, "run_registration_task", fake_run_registration_task)
    monkeypatch.setattr(registration_routes.random, "randint", lambda low, high: wait_seconds)
    monkeypatch.setattr(registration_routes.asyncio, "sleep", fake_sleep)

    batch_id = f"batch-{wait_strategy}"
    asyncio.run(
        registration_routes.run_batch_pipeline(
            batch_id=batch_id,
            task_uuids=["task-1", "task-2"],
            email_service_type="tempmail",
            proxy=None,
            email_service_config=None,
            email_service_id=None,
            interval_min=wait_seconds,
            interval_max=wait_seconds,
            concurrency=1,
            wait_strategy=wait_strategy,
        )
    )
    logs = list(registration_routes.batch_tasks[batch_id]["logs"])
    registration_routes.batch_tasks.clear()
    return events, sleep_calls, logs


def test_run_batch_pipeline_start_wait_strategy(monkeypatch):
    events, sleep_calls, logs = _run_pipeline(
        monkeypatch,
        RegistrationWaitStrategy.START.value,
        7,
    )

    assert events == [("run", "task-1"), ("run", "task-2")]
    assert sleep_calls == [7]
    assert any("等待策略: 启动间隔" in log for log in logs)
    assert any("[系统] 等待 7 秒后启动下一个任务" in log for log in logs)
    assert not any("完成后等待 7 秒" in log for log in logs)


def test_run_batch_pipeline_completion_wait_strategy(monkeypatch):
    events, sleep_calls, logs = _run_pipeline(
        monkeypatch,
        RegistrationWaitStrategy.COMPLETION.value,
        9,
    )

    assert events == [("run", "task-1"), ("run", "task-2")]
    assert sleep_calls == [9]
    assert any("等待策略: 完成间隔" in log for log in logs)
    assert any("[任务1] 完成后等待 9 秒，再启动后续任务" in log for log in logs)
    assert not any("[系统] 等待 9 秒后启动下一个任务" in log for log in logs)
