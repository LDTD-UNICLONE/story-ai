import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from scripts.testing import REDIS_VISIBILITY_TIMEOUT

from app.models.ai_model import AiModel
from app.models.points import UserPointsTransaction
from app.models.project import Project
from app.models.project_canvas import ProjectCanvas, CanvasNode
from app.models.canvas_generation import CanvasGeneration
from app.models.task_dispatch import TaskDispatchOutbox
from app.models.task_record import UserTaskRecord
from app.models.user import User
from test_migrations_integration import migration_db as migration_db


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION_TESTS") != "1", reason="use --with-redis"),
]
ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "tasks.canvas_generation.run_canvas_generation"


def wait_until(predicate, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail(f"condition did not become true within {timeout}s")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def queue_runtime(migration_db, test_database_url, tmp_path, request):
    redis_server = shutil.which("redis-server")
    if not redis_server:
        pytest.fail("Install redis-server to run --with-redis tests")
    migration_db.migrate()
    engine = create_engine(
        make_url(test_database_url).set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        execution_options={"schema_translate_map": {None: migration_db.schema}},
        connect_args={"options": f"-c search_path={migration_db.schema}"},
    )
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            id=uuid4(),
            account=uuid4().hex,
            password_hash="test",
            nickname="Queue",
            points_balance=100,
        )
        model = AiModel(
            id=uuid4(),
            nickname="Queue model",
            model_id="gpt-5.5",
            vendor="apimart",
            model_type="text",
            is_enabled=True,
            configuration={"billing": {"base_points": 1, "multipliers": {"platform": "1"}}},
        )
        db.add_all([user, model])
        db.flush()
        project = Project(id=uuid4(), user_id=user.id, name="Queue", cover="", description="")
        db.add(project)
        db.flush()
        canvas = ProjectCanvas(id=uuid4(), project_id=project.id, name="Queue canvas")
        db.add(canvas)
        db.flush()
        node = CanvasNode(
            id=uuid4(), canvas_id=canvas.id, kind="text", title="Queue text",
            x=0, y=0, width=320, height=240,
            content={"text": "Test source", "generation": {"ai_model_id": str(model.id), "parameters": {}}},
        )
        db.add(node)
        db.commit()
        ids = {
            "TEST_USER_ID": str(user.id),
            "TEST_MODEL_ID": str(model.id),
            "TEST_PROJECT_ID": str(project.id),
            "TEST_CANVAS_ID": str(canvas.id),
            "TEST_NODE_ID": str(node.id),
        }
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    broker = f"redis://127.0.0.1:{port}/1"
    client = Redis.from_url(broker, socket_timeout=1, socket_connect_timeout=1)
    processes = []
    streams = []

    def spawn(command, name, env=None):
        stream = (tmp_path / f"{name}.log").open("wb")
        streams.append(stream)
        process = subprocess.Popen(
            command, env=env, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT
        )
        processes.append(process)
        return process

    def start_redis():
        process = spawn(
            [
                redis_server,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "yes",
                "--dir",
                str(tmp_path),
            ],
            f"redis-{len(processes)}",
        )

        def available():
            assert process.poll() is None, "redis-server exited; see Redis log"
            try:
                return client.ping()
            except RedisConnectionError:
                return False

        wait_until(available, timeout=10)
        return process

    def start(mode, **flags):
        name = f"{mode}-{len(processes)}"
        env = {
            **os.environ,
            **ids,
            **flags,
            "TEST_DATABASE_URL": test_database_url,
            "TEST_DATABASE_SCHEMA": migration_db.schema,
            "TEST_RUNTIME_DIR": str(tmp_path),
            "TEST_PROCESS_NAME": name,
            "TEST_BROKER_URL": broker,
            "TEST_RESULT_BACKEND": f"redis://127.0.0.1:{port}/2",
        }
        process = spawn([sys.executable, str(ROOT / "tests/celery_runtime.py"), mode], name, env)
        if mode == "worker":

            def ready():
                assert process.poll() is None, "worker exited before ready; see Worker log"
                return (tmp_path / f"{name}.ready").exists()

            wait_until(ready, timeout=20)
        elif mode == "submit":
            assert process.wait(timeout=20) == 0, "submission failed; see submit log"
        return process

    def state():
        with Session(engine) as db:
            task = db.scalar(select(UserTaskRecord))
            return SimpleNamespace(
                task_id=str(task.id) if task else None,
                status=task.status if task else None,
                cost=task.points_cost if task else None,
                attempts=(task.extra or {}).get("execution_attempt") if task else None,
                settled=(task.extra or {}).get("points_settled") if task else None,
                balance=db.get(User, user.id).points_balance,
                transactions=db.scalar(select(func.count()).select_from(UserPointsTransaction)),
                outbox=db.scalar(select(func.count()).select_from(TaskDispatchOutbox)),
                content=(db.scalar(select(CanvasGeneration.result)) or {}).get("text"),
            )

    def events(kind):
        path = tmp_path / "events.jsonl"
        return (
            [
                event
                for line in path.read_text().splitlines()
                if (event := json.loads(line))["kind"] == kind
            ]
            if path.exists()
            else []
        )

    try:
        redis_process = start_redis()
        yield SimpleNamespace(
            start=start,
            start_redis=start_redis,
            redis=redis_process,
            redis_client=client,
            state=state,
            events=events,
            directory=tmp_path,
        )
    finally:
        for process in reversed(processes):
            stop(process)
        client.close()
        engine.dispose()
        for stream in streams:
            stream.close()
        artifact_dir = os.getenv("TEST_ARTIFACT_DIR")
        if artifact_dir:
            destination = Path(artifact_dir) / request.node.name
            destination.mkdir(parents=True, exist_ok=True)
            for pattern in ("*.log", "*.jsonl"):
                for path in tmp_path.glob(pattern):
                    shutil.copy2(path, destination / path.name)
        if any(
            getattr(request.node, f"rep_{phase}", None)
            and getattr(request.node, f"rep_{phase}").failed
            for phase in ("setup", "call")
        ):
            for path in tmp_path.glob("*.log"):
                print(f"\n{path.name}:\n{path.read_text(errors='replace')[-6000:]}")


def assert_completed(runtime, *, provider_calls=1):
    wait_until(lambda: runtime.state().status == "success")
    state = runtime.state()
    assert state.content == "Generated by test provider"
    assert state.cost > 0
    assert state.balance == 100 - state.cost
    assert state.transactions == 1
    assert state.settled is True
    assert state.outbox == 0
    assert len(runtime.events("provider")) == provider_calls
    return state


def test_beat_recovers_submission_process_exit_after_commit(queue_runtime):
    runtime = queue_runtime
    runtime.start("submit", TEST_EXIT_AFTER_COMMIT="1")
    assert runtime.state().status == "pending"
    assert runtime.state().outbox == 1
    runtime.start("worker")
    runtime.start("beat")
    assert_completed(runtime)


def test_broker_outage_recovers_after_redis_restart(queue_runtime):
    runtime = queue_runtime
    stop(runtime.redis)
    runtime.start("submit")
    assert runtime.state().status == "pending"
    assert runtime.state().outbox == 1
    assert runtime.state().balance == 100
    runtime.start_redis()
    runtime.start("worker")
    runtime.start("beat")
    assert_completed(runtime)


def test_worker_crash_before_ack_redelivers_without_double_generation(queue_runtime):
    runtime = queue_runtime
    first = runtime.start("worker", TEST_CRASH_BEFORE_ACK="1")
    runtime.start("submit")
    wait_until(lambda: first.poll() is not None)
    assert first.returncode == 17
    initial = assert_completed(runtime)
    unacked = runtime.redis_client.zrange("unacked_index", 0, -1, withscores=True)
    assert len(unacked) == 1
    # Start the replacement after the real message becomes visible. Starting it
    # earlier races the initial recovery scan and Kombu's longer sweep interval.
    wait_until(lambda: time.time() > unacked[0][1] + REDIS_VISIBILITY_TIMEOUT)
    runtime.start("worker")
    wait_until(
        lambda: len([e for e in runtime.events("received") if e["task_name"] == TASK_NAME]) >= 2
    )
    wait_until(
        lambda: (
            len(
                [
                    e
                    for e in runtime.events("finished")
                    if e["task_name"] == TASK_NAME and e["state"] == "SUCCESS"
                ]
            )
            >= 2
        )
    )
    final = assert_completed(runtime)
    assert final.task_id == initial.task_id
    assert final.attempts == initial.attempts == 1
    assert final.balance == initial.balance


def test_real_celery_retry_settles_once_after_provider_recovers(queue_runtime):
    runtime = queue_runtime
    runtime.start("worker", TEST_PROVIDER_RETRY="1")
    runtime.start("submit")
    final = assert_completed(runtime, provider_calls=2)
    assert final.attempts == 2
