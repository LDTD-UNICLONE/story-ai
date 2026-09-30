import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TASKS = {
    "canvas_generation": ["run_canvas_generation", "settle_pending_canvas_points"],
    "example": ["ping"],
    "model_generation": ["run_conversation_generation", "settle_pending_conversation_points"],
    "project_chapter": ["run_project_chapter_processing"],
    "project_asset_analysis": ["run_project_asset_analysis"],
    "project_asset_generation": ["run_project_asset_image_generation"],
    "project_storyboard": ["run_project_storyboard_analysis", "run_project_storyboard_stage"],
    "project_storyboard_image": ["run_project_storyboard_image_generation"],
    "project_storyboard_video": ["run_project_storyboard_video_generation"],
    "agent_source_analysis": ["run_source_analysis", "run_agent_text_task"],
    "agent_production_controller": [
        "advance_agent_batch_production",
        "enqueue_agent_batch_productions",
    ],
    "agent_delivery": ["build_agent_delivery"],
    "provider_reconcile": ["reconcile_provider_task", "enqueue_pending_provider_reconciliations", "transfer_provider_media"],
    "task_dispatch": ["dispatch_pending_tasks"],
    "seedance_images": ["review_image", "enqueue_due_reviews"],
}


def cold_start(code, *args):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom scripts.testing import configure_test_settings\nconfigure_test_settings()\n"
            + code,
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("entry", ["app.worker", *[f"app.tasks.{name}" for name in TASKS]])
def test_task_registration_does_not_depend_on_import_order(entry):
    cold_start(
        """
import importlib, json, sys
from celery.app.utils import find_app
importlib.import_module(sys.argv[1])
from app.worker import celery_app
assert find_app('app.worker.celery_app') is celery_app
expected = {f'tasks.{module}.{name}' for module, names in json.loads(sys.argv[2]).items() for name in names}
registered = {name for name in celery_app.tasks if name.startswith('tasks.')}
assert registered == expected, (registered ^ expected)
celery_app.loader.import_default_modules()
assert {name for name in celery_app.tasks if name.startswith('tasks.')} == expected
assert all(celery_app.tasks[name].app is celery_app for name in expected)
assert all(job['task'] in expected for job in celery_app.conf.beat_schedule.values())
""",
        entry,
        json.dumps(TASKS),
    )


def test_application_static_imports_are_acyclic():
    files = {
        ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__"): path
        for path in (ROOT / "app").rglob("*.py")
    }
    graph = {name: set() for name in files}
    for name, path in files.items():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
                    parents = package.split(".")
                    module = ".".join(
                        parents[: len(parents) - node.level + 1] + ([module] if module else [])
                    )
                targets = [module, *[f"{module}.{alias.name}" for alias in node.names]]
            else:
                continue
            graph[name].update(target for target in targets if target in files and target != name)
    visited = set()
    active = []

    def visit(name):
        assert name not in active, " -> ".join([*active, name])
        if name in visited:
            return
        active.append(name)
        for target in sorted(graph[name]):
            visit(target)
        active.pop()
        visited.add(name)

    for name in sorted(graph):
        visit(name)


def test_celery_configuration_and_publisher_do_not_import_tasks():
    cold_start("""
import sys
from app.core.celery_app import celery_app
from app.integrations.task_queue import publisher
assert publisher is not celery_app
assert 'app.worker' not in sys.modules
assert not any(name.startswith('app.tasks') for name in sys.modules)
assert celery_app.conf.task_acks_late is True
assert celery_app.conf.task_reject_on_worker_lost is True
assert celery_app.conf.worker_prefetch_multiplier == 1
""")


def test_worker_queues_routes_and_beat_contract():
    from app.worker import celery_app

    queues = [
        "celery",
        "story_ai_default",
        "story_ai_text",
        "story_ai_image",
        "story_ai_video",
        "story_ai_delivery",
        "story_ai_query",
        "story_ai_media",
    ]
    assert [
        (q.name, q.exchange.name, q.exchange.type, q.routing_key)
        for q in celery_app.conf.task_queues
    ] == [(name, name, "direct", name) for name in queues]
    groups = {
        "project_chapter": "text",
        "project_asset_analysis": "text",
        "project_storyboard": "text",
        "agent_source_analysis": "text",
        "project_asset_generation": "image",
        "project_storyboard_image": "image",
        "project_storyboard_video": "video",
        "agent_production_controller": "default",
        "agent_delivery": "delivery",
        "provider_reconcile": "default",
        "task_dispatch": "default",
        "seedance_images": "default",
    }
    expected_routes = {
        f"tasks.{module}.{task}": {"queue": f"story_ai_{kind}", "routing_key": f"story_ai_{kind}"}
        for module, kind in groups.items()
        for task in TASKS[module]
    }
    expected_routes["tasks.model_generation.settle_pending_conversation_points"] = {
        "queue": "story_ai_default",
        "routing_key": "story_ai_default",
    }
    expected_routes["tasks.provider_reconcile.reconcile_provider_task"] = {"queue": "story_ai_query", "routing_key": "story_ai_query"}
    expected_routes["tasks.provider_reconcile.transfer_provider_media"] = {"queue": "story_ai_media", "routing_key": "story_ai_media"}
    assert celery_app.conf.task_routes == expected_routes
    interval = 180
    assert celery_app.conf.beat_schedule == {
        "settle-pending-canvas-points": {
            "task": "tasks.canvas_generation.settle_pending_canvas_points",
            "schedule": interval,
            "args": (100,),
        },
        "enqueue-seedance-image-reviews": {
            "task": "tasks.seedance_images.enqueue_due_reviews",
            "schedule": 5,
            "args": (100,),
        },
        "dispatch-pending-tasks": {
            "task": "tasks.task_dispatch.dispatch_pending_tasks",
            "schedule": 10,
            "args": (100,),
        },
        "enqueue-agent-batch-productions": {
            "task": "tasks.agent_production_controller.enqueue_agent_batch_productions",
            "schedule": 30,
            "args": (100,),
        },
        "enqueue-pending-provider-reconciliations": {
            "task": "tasks.provider_reconcile.enqueue_pending_provider_reconciliations",
            "schedule": interval,
            "args": (100,),
        },
        "settle-pending-conversation-points": {
            "task": "tasks.model_generation.settle_pending_conversation_points",
            "schedule": interval,
            "args": (100,),
        },
    }
