import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from app.core.exceptions import AppException
from app.services.models.configuration import (
    build_model_configuration,
    build_model_runtime_snapshot,
    ensure_model_available,
    normalize_model_configuration,
)


def _model(**overrides):
    values = {
        "vendor": "comfly",
        "model_type": "video",
        "model_id": "video-model",
        "configuration": {
            "version": 1,
            "request": {"capabilities": {}},
            "billing": {"base_points": 0, "multipliers": {}, "policy": {}},
            "operations": {"status": "active"},
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_empty_model_configuration_uses_vendor_and_type_defaults() -> None:
    configuration = build_model_configuration(_model(configuration={}))

    assert configuration == {
        "version": 1,
        "request": {
            "capabilities": {},
        },
        "billing": {
            "base_points": 0,
            "multipliers": {
                "model": "1",
                "cache": "1",
                "completion": "1",
                "platform": "1",
            },
            "policy": {},
        },
        "operations": {
            "status": "active",
            "maintenance_message": None,
        },
    }


def test_unified_configuration_is_normalized_without_legacy_fields() -> None:
    configuration = normalize_model_configuration(
        vendor="apimart",
        model_type="image",
        configuration={
            "version": 1,
            "request": {"capabilities": {"resolutions": ["1k", "2k"]}},
            "billing": {
                "base_points": 8,
                "multipliers": {"platform": 1},
                "policy": {},
            },
            "operations": {"status": "active"},
        },
    )

    assert configuration["billing"]["multipliers"] == {
        "model": "1",
        "cache": "1",
        "completion": "1",
        "platform": "1",
    }
    model = _model(vendor="apimart", model_type="image", configuration=configuration)
    snapshot = build_model_runtime_snapshot(model)

    assert snapshot.configuration == configuration
    assert not hasattr(snapshot, "points_cost")
    assert not hasattr(snapshot, "capabilities")


def test_unified_configuration_validates_vendor_specific_billing_policy() -> None:
    configuration = {
        "version": 1,
        "request": {"capabilities": {}},
        "billing": {
            "base_points": 1,
            "policy": {
                "type": "text",
                "pricing_unit": "apimart_credits",
                "tiers": [
                    {
                        "input_credits_per_million": "40",
                        "cached_input_credits_per_million": "4",
                        "cache_write_credits_per_million": "50",
                        "output_credits_per_million": "240",
                    }
                ],
            },
        },
        "operations": {"status": "active"},
    }

    with pytest.raises(AppException) as exc_info:
        normalize_model_configuration(
            vendor="comfly",
            model_type="text",
            configuration=configuration,
        )

    assert exc_info.value.code == 40062


def test_configuration_validation_does_not_load_points_services() -> None:
    # A fresh interpreter prevents other tests' imports from hiding this dependency.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            dedent(
                """
                import sys
                from app.services.models.configuration import normalize_model_configuration

                configuration = normalize_model_configuration(
                    vendor="comfly",
                    model_type="image",
                    configuration={"billing": {"policy": {"type": " IMAGE "}}},
                )

                assert configuration["billing"]["policy"] == {"type": "image"}
                assert "app.services.billing.model_points" not in sys.modules
                assert "app.services.billing.points" not in sys.modules
                """
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_maintenance_configuration_blocks_new_generation_with_public_message() -> None:
    model = _model(
        configuration={
            "version": 1,
            "request": {"capabilities": {}},
            "billing": {"base_points": 0, "multipliers": {}, "policy": {}},
            "operations": {
                "status": "maintenance",
                "maintenance_message": "模型升级中，请稍后重试",
            },
        }
    )

    with pytest.raises(AppException) as exc_info:
        ensure_model_available(model)

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == 50301
    assert exc_info.value.message == "模型升级中，请稍后重试"


@pytest.mark.parametrize(
    "configuration",
    [
        {"version": 2},
        {"version": 1, "unknown": {}},
        {"version": 1, "operations": {"status": "paused"}},
        {
            "version": 1,
            "billing": {"base_points": -1},
        },
    ],
)
def test_invalid_unified_configuration_is_rejected(configuration: dict) -> None:
    with pytest.raises(AppException) as exc_info:
        normalize_model_configuration(
            vendor="comfly",
            model_type="text",
            configuration=configuration,
        )

    assert exc_info.value.code == 40064
