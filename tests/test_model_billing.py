from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_data
from app.services.ai_models import get_ai_model_billing_recommendation
from app.services.model_configuration import default_model_platform_multiplier
from app.services.model_points import (
    _task_billing_model,
    build_model_billing_snapshot,
    calculate_image_model_points_cost,
    calculate_apimart_actual_points_cost,
    calculate_text_model_points_cost,
    calculate_text_submission_points_cost,
    calculate_video_model_points_cost,
    model_minimum_balance_points,
    settle_image_task_points,
    settle_text_task_points,
    summarize_base_points_recommendation,
    validate_model_billing_policy,
)
from app.services.points import change_user_points


def _model(model_type: str, policy: dict, **overrides):
    values = {
        "model_id": "custom-model",
        "vendor": "apimart",
        "model_type": model_type,
        "capabilities": {},
        "base_points": 7,
        "model_rate": 1,
        "cache_rate": 1,
        "completion_rate": 1,
        "platform_rate": 1,
    }
    values.update(overrides)
    return SimpleNamespace(
        id=values.get("id"),
        model_id=values["model_id"],
        vendor=values["vendor"],
        model_type=values["model_type"],
        configuration={
            "version": 1,
            "request": {"capabilities": values["capabilities"]},
            "billing": {
                "base_points": values["base_points"],
                "multipliers": {
                    "model": values["model_rate"],
                    "cache": values["cache_rate"],
                    "completion": values["completion_rate"],
                    "platform": values["platform_rate"],
                },
                "policy": policy,
            },
            "operations": {"status": "active"},
        },
    )


def _apimart_text_credits_policy() -> dict:
    return {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "max_input_tokens": 272000,
                "input_credits_per_million": "40",
                "cached_input_credits_per_million": "4",
                "cache_write_credits_per_million": "50",
                "output_credits_per_million": "240",
            },
            {
                "input_credits_per_million": "80",
                "cached_input_credits_per_million": "8",
                "cache_write_credits_per_million": "100",
                "output_credits_per_million": "360",
            },
        ],
    }


def test_text_policy_uses_model_specific_token_rates_and_multipliers() -> None:
    model = _model(
        "text",
        {
            "type": "text",
            "input_points_per_million": "3",
            "cached_input_points_per_million": "1",
            "output_points_per_million": "8",
            "minimum_points": 2,
        },
        model_rate="1.5",
        cache_rate="0.5",
        completion_rate="2",
        platform_rate="1.2",
    )

    points, detail = calculate_text_model_points_cost(
        model,
        {
            "provider_response": {
                "usage": {
                    "input_tokens": 1_000_000,
                    "cached_tokens": 200_000,
                    "output_tokens": 100_000,
                }
            }
        },
    )

    assert points == 7
    assert detail["billing_policy_type"] == "text"
    assert detail["input_points_per_million"] == "3"
    assert detail["cached_input_points_per_million"] == "1"
    assert detail["output_points_per_million"] == "8"


def test_apimart_text_credits_policy_uses_usage_and_rounds_cost_before_platform_rate() -> None:
    model = _model(
        "text",
        _apimart_text_credits_policy(),
        platform_rate="1.2",
    )

    points, detail = calculate_text_model_points_cost(
        model,
        {
            "provider_response": {
                "usage": {
                    "prompt_tokens": 786,
                    "completion_tokens": 1692,
                    "prompt_tokens_details": {
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                    },
                    "completion_tokens_details": {"reasoning_tokens": 32},
                }
            }
        },
    )

    assert points == 5
    assert detail["billing_type"] == "apimart_usage_credits"
    assert detail["billing_source"] == "provider_usage"
    assert detail["provider_credits_cost"] == "0.43752"
    assert detail["provider_cost_cny"] == "0.306264"
    assert detail["provider_cost_points"] == 4
    assert detail["user_points_cost"] == 5
    assert detail["tier_index"] == 1
    assert detail["reasoning_tokens"] == 32


def test_apimart_text_credits_policy_selects_tier_and_prices_cache_write() -> None:
    model = _model(
        "text",
        _apimart_text_credits_policy(),
        platform_rate="1.2",
    )

    points, detail = calculate_text_model_points_cost(
        model,
        {
            "usage": {
                "prompt_tokens": 300000,
                "completion_tokens": 100000,
                "prompt_tokens_details": {
                    "cached_tokens": 50000,
                    "cache_write_tokens": 25000,
                },
            }
        },
    )

    assert points == 479
    assert detail["tier_index"] == 2
    assert detail["normal_input_tokens"] == 225000
    assert detail["cached_tokens"] == 50000
    assert detail["cache_write_tokens"] == 25000
    assert detail["provider_credits_cost"] == "56.9"
    assert detail["provider_cost_points"] == 399


def test_apimart_text_credits_policy_validation_is_vendor_aware() -> None:
    policy = {
        **_apimart_text_credits_policy(),
        "tiers": [_apimart_text_credits_policy()["tiers"][-1]],
    }

    assert validate_model_billing_policy("text", policy, vendor="apimart") == policy
    with pytest.raises(AppException) as exc_info:
        validate_model_billing_policy("text", policy, vendor="comfly")

    assert exc_info.value.code == 40062


def test_apimart_text_credits_policy_requires_unbounded_final_tier() -> None:
    with pytest.raises(AppException) as exc_info:
        validate_model_billing_policy(
            "text",
            {
                "type": "text",
                "pricing_unit": "apimart_credits",
                "tiers": [
                    {
                        "max_input_tokens": 272000,
                        "input_credits_per_million": "40",
                        "cached_input_credits_per_million": "4",
                        "cache_write_credits_per_million": "50",
                        "output_credits_per_million": "240",
                    }
                ],
            },
            vendor="apimart",
        )

    assert exc_info.value.code == 40062


def test_image_policy_charges_quantity_and_resolution() -> None:
    model = _model(
        "image",
        {
            "type": "image",
            "default_resolution": "1k",
            "resolution_multipliers": {"1k": "1", "2k": "2"},
        },
        base_points=4,
        model_rate="1.25",
        platform_rate="1.2",
        vendor="comfly",
    )

    points = calculate_image_model_points_cost(
        model,
        {"n": 3, "resolution": "2K"},
    )

    assert points == 36


def test_apimart_seedream_image_policy_charges_resolution_pixels_and_references() -> None:
    model = _model(
        "image",
        {
            "type": "image",
            "default_resolution": "1.5k",
            "resolution_multipliers": {
                "1k": "1",
                "1.5k": "1",
                "2k": "2",
            },
            "pixel_tiers": {
                "1.5k": 2_610_000,
                "2k": 4_624_220,
            },
            "free_reference_images": 1,
            "points_per_additional_reference_image": "0.2",
        },
        model_id="seedream-5-0-pro",
        base_points=3,
    )

    assert (
        calculate_image_model_points_cost(
            model,
            {
                "resolution": "1.5K",
                "image_urls": ["https://example.com/1.png", "https://example.com/2.png"],
            },
        )
        == 4
    )
    assert (
        calculate_image_model_points_cost(
            model,
            {
                "resolution": "1K",
                "size": "2K",
                "image_urls": [f"https://example.com/{index}.png" for index in range(4)],
            },
        )
        == 7
    )
    assert calculate_image_model_points_cost(model, {"size": "1600x1600"}) == 3
    assert calculate_image_model_points_cost(model, {"size": "2048X1536"}) == 6


@pytest.mark.parametrize(
    ("model_id", "base_points", "multipliers", "expected"),
    [
        (
            "gemini-3.1-flash-image-preview",
            4,
            {"0.5k": "1", "1k": "1", "2k": "1.3333333333", "4k": "1.6666666667"},
            {"0.5k": 4, "1k": 4, "2k": 6, "4k": 7},
        ),
        (
            "gemini-3-pro-image-preview",
            4,
            {"1k": "1", "2k": "1", "4k": "1.3333333333"},
            {"1k": 4, "2k": 4, "4k": 6},
        ),
        (
            "gpt-image-2",
            4,
            {"1k": "1", "2k": "1.6470588235", "4k": "2.4705882353"},
            {"1k": 4, "2k": 7, "4k": 10},
        ),
        (
            "qwen-image-3.0",
            2,
            {"1k": "1", "2k": "1"},
            {"1k": 2, "2k": 2},
        ),
        (
            "grok-imagine-2.0-ext",
            4,
            {"quality": "1"},
            {"quality": 4},
        ),
    ],
)
def test_apimart_image_price_tiers_preserve_admin_base_price(
    model_id, base_points, multipliers, expected
) -> None:
    model = _model(
        "image",
        {
            "type": "image",
            "default_resolution": next(iter(multipliers)),
            "resolution_multipliers": multipliers,
        },
        model_id=model_id,
        base_points=base_points,
    )

    for resolution, points in expected.items():
        assert (
            calculate_image_model_points_cost(
                model,
                {"resolution": resolution},
            )
            == points
        )

    assert (
        calculate_image_model_points_cost(
            model,
            {"resolution": next(iter(expected)), "n": 3},
        )
        == expected[next(iter(expected))] * 3
    )


def test_apimart_qwen_image_pixel_threshold_uses_standard_flat_tiers() -> None:
    model = _model(
        "image",
        {
            "type": "image",
            "default_resolution": "1k",
            "resolution_multipliers": {"1k": "1", "2k": "1"},
            "pixel_tiers": {"1k": 2_250_000, "2k": 4_194_304},
        },
        model_id="qwen-image-3.0",
        base_points=2,
    )

    assert calculate_image_model_points_cost(model, {"size": "1500x1500"}) == 2
    assert calculate_image_model_points_cost(model, {"size": "1600x1600"}) == 2


def test_image_policy_validates_pixel_tiers_and_reference_pricing() -> None:
    with pytest.raises(AppException) as exc_info:
        validate_model_billing_policy(
            "image",
            {
                "type": "image",
                "pixel_tiers": {"1.5k": 0},
                "free_reference_images": -1,
                "points_per_additional_reference_image": "0.2",
            },
            vendor="apimart",
        )

    assert exc_info.value.code == 40062


def test_video_policy_charges_duration_resolution_and_reference_type() -> None:
    model = _model(
        "video",
        {
            "type": "video",
            "default_duration_seconds": 5,
            "rates": {
                "720p": {"none": "12", "image": "12", "video": "13"},
                "1080p": {"none": "30", "image": "30", "video": "35"},
            },
        },
    )

    points = calculate_video_model_points_cost(
        model,
        {
            "duration": 8,
            "resolution": "1080P",
            "video_urls": ["https://example.com/reference.mp4"],
        },
    )

    assert points == 280


def test_video_policy_does_not_treat_disabled_audio_as_audio_reference() -> None:
    model = _model(
        "video",
        {
            "type": "video",
            "default_duration_seconds": 5,
            "default_resolution": "540p",
            "rates": {
                "540p": {
                    "none": "1.68",
                    "audio": "2.24",
                }
            },
        },
        model_id="pixverse-v6",
        platform_rate="1",
    )

    assert (
        calculate_video_model_points_cost(
            model,
            {"duration": 5, "resolution": "540p", "audio": False},
        )
        == 9
    )
    assert (
        calculate_video_model_points_cost(
            model,
            {"duration": 5, "resolution": "540p", "audio": True},
        )
        == 12
    )


def test_empty_policy_keeps_legacy_image_and_video_prices() -> None:
    image_model = _model("image", {}, base_points=9)
    video_model = _model("video", {}, model_id="seedance-2.5")

    assert calculate_image_model_points_cost(image_model, {"n": 4}) == 9
    assert calculate_video_model_points_cost(video_model, {"duration": 8}) == 80


def test_policy_type_must_match_model_type() -> None:
    with pytest.raises(AppException) as exc_info:
        validate_model_billing_policy(
            "image",
            {"type": "video", "rates": {"default": {"none": 10}}},
        )

    assert exc_info.value.code == 40062


def test_policy_rejects_unknown_nested_fields() -> None:
    with pytest.raises(AppException) as exc_info:
        validate_model_billing_policy(
            "video",
            {
                "type": "video",
                "rates": {"default": {"none": 10}},
                "unexpected": True,
            },
        )

    assert exc_info.value.code == 40062


def test_apimart_text_tier_rejects_unknown_fields() -> None:
    policy = _apimart_text_credits_policy()
    policy["tiers"][0]["currency"] = "USD"

    with pytest.raises(AppException) as exc_info:
        validate_model_billing_policy("text", policy, vendor="apimart")

    assert exc_info.value.code == 40062


def test_legacy_image_unit_price_is_removed_and_base_points_is_authoritative() -> None:
    policy = validate_model_billing_policy(
        "image",
        {
            "type": " IMAGE ",
            "points_per_image": "2",
            "default_resolution": "1k",
            "resolution_multipliers": {"1k": "1"},
        },
    )
    model = _model("image", policy, base_points=9)

    assert "points_per_image" not in policy
    assert policy["type"] == "image"
    assert calculate_image_model_points_cost(model, {"n": 2}) == 18


def test_task_billing_snapshot_keeps_in_flight_price_stable() -> None:
    original = _model(
        "image",
        {"type": "image"},
        base_points=4,
        model_rate="1.5",
        vendor="comfly",
    )
    snapshot = build_model_billing_snapshot(original)
    changed = _model(
        "image",
        {"type": "image"},
        base_points=20,
        model_rate="2",
    )

    billing_model = _task_billing_model(
        SimpleNamespace(extra={"model_billing_snapshot": snapshot}),
        changed,
    )

    assert calculate_image_model_points_cost(billing_model, {"n": 2}) == 12


def test_apimart_video_actual_cost_uses_real_points_and_platform_rate() -> None:
    model = _model("video", {}, platform_rate="1.2")

    points, detail = calculate_apimart_actual_points_cost(
        model,
        {
            "provider_response": {
                "cost": 0.3104,
                "credits_cost": 3.104,
            }
        },
    )

    assert points == 27
    assert detail["provider_cost_usd"] == "0.3104"
    assert detail["provider_credits_cost"] == "3.104"
    assert detail["provider_cost_cny"] == "2.1728"
    assert detail["provider_cost_points"] == 22
    assert detail["pricing_mode"] == "actual_cost_platform_rate"
    assert detail["platform_rate"] == "1.2"
    assert detail["user_points_cost"] == 27
    assert detail["billing_source"] == "provider_credits_cost"


def test_apimart_text_actual_cost_falls_back_to_cost() -> None:
    model = _model("text", {}, platform_rate="1.2")

    points, detail = calculate_apimart_actual_points_cost(
        model,
        {"model_result_extra": {"provider_response": {"cost": "0.2"}}},
    )

    assert points == 17
    assert detail["provider_credits_cost"] == "2.0"
    assert detail["provider_cost_cny"] == "1.40"
    assert detail["provider_cost_points"] == 14
    assert detail["platform_rate"] == "1.2"
    assert detail["billing_source"] == "provider_cost"


def test_apimart_image_uses_admin_fixed_points_and_tracks_provider_cost() -> None:
    model = _model("image", {}, base_points=9, platform_rate="5")

    points, detail = calculate_apimart_actual_points_cost(
        model,
        {"provider_response": {"credits_cost": "3.104"}},
    )

    assert points == 9
    assert detail["pricing_mode"] == "fixed_admin_points"
    assert detail["provider_cost_points"] == 22
    assert detail["user_points_cost"] == 9


def test_non_apimart_does_not_use_provider_actual_cost() -> None:
    model = _model("image", {}, vendor="comfly")

    assert calculate_apimart_actual_points_cost(
        model,
        {"provider_response": {"credits_cost": 3}},
    ) == (None, {})


@pytest.mark.asyncio
async def test_apimart_image_success_keeps_admin_fixed_charge(monkeypatch) -> None:
    transactions = []

    async def change_points(_db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id="refund-transaction")

    monkeypatch.setattr("app.services.model_points.change_user_points", change_points)
    task_record = SimpleNamespace(
        user_id="user-id",
        title="生成图片",
        points_cost=12,
        extra={},
    )

    await settle_image_task_points(
        SimpleNamespace(),
        task_record,
        _model("image", {}, base_points=12),
        {"provider_response": {"credits_cost": "3.104"}},
        remark_prefix="图像生成",
    )

    assert task_record.points_cost == 12
    assert task_record.extra["points_settled"] is True
    assert task_record.extra["points_billing"]["user_points_cost"] == 12
    assert task_record.extra["provider_cost_billing"]["provider_cost_cny"] == "2.1728"
    assert transactions == []


@pytest.mark.asyncio
async def test_apimart_image_success_keeps_admin_policy_charge(monkeypatch) -> None:
    transactions = []

    async def change_points(_db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id="points-transaction")

    monkeypatch.setattr("app.services.model_points.change_user_points", change_points)
    policy = {
        "type": "image",
        "default_resolution": "1.5k",
        "resolution_multipliers": {"1.5k": "1", "2k": "2"},
        "free_reference_images": 1,
        "points_per_additional_reference_image": "0.2",
    }
    task_record = SimpleNamespace(
        user_id="user-id",
        title="Seedream 5.0 Pro",
        points_cost=7,
        extra={
            "model_extra": {
                "resolution": "2K",
                "image_urls": [f"https://example.com/{index}.png" for index in range(4)],
            }
        },
    )

    await settle_image_task_points(
        SimpleNamespace(),
        task_record,
        _model("image", policy, model_id="seedream-5-0-pro", base_points=3),
        {"provider_response": {"credits_cost": "0.9"}},
        remark_prefix="图像生成",
    )

    assert task_record.points_cost == 7
    assert task_record.extra["points_billing"]["user_points_cost"] == 7
    assert task_record.extra["provider_cost_billing"]["pricing_mode"] == "admin_image_policy"
    assert transactions == []


@pytest.mark.asyncio
async def test_actual_cost_supplement_is_recorded_even_when_it_overdraws_balance(
    monkeypatch,
) -> None:
    transactions = []

    async def change_points(_db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id="supplement-transaction")

    monkeypatch.setattr("app.services.model_points.change_user_points", change_points)
    task_record = SimpleNamespace(
        user_id="user-id",
        title="文本生成",
        points_cost=5,
        extra={},
    )

    await settle_text_task_points(
        SimpleNamespace(),
        task_record,
        _model("text", {}, platform_rate="1.2"),
        {"provider_response": {"credits_cost": "2"}},
        remark_prefix="文本生成",
    )

    assert task_record.points_cost == 17
    assert task_record.extra["points_settled"] is True
    assert "points_settlement_failed" not in task_record.extra
    assert transactions[0]["amount"] == -12
    assert transactions[0]["allow_negative_balance"] is True


@pytest.mark.asyncio
async def test_apimart_usage_credits_settlement_refunds_precharge(monkeypatch) -> None:
    transactions = []

    async def change_points(_db, **kwargs):
        transactions.append(kwargs)
        return SimpleNamespace(id="refund-transaction")

    monkeypatch.setattr("app.services.model_points.change_user_points", change_points)
    task_record = SimpleNamespace(
        user_id="user-id",
        title="APIMart 文本生成",
        points_cost=10,
        extra={},
    )

    await settle_text_task_points(
        SimpleNamespace(),
        task_record,
        _model(
            "text",
            _apimart_text_credits_policy(),
            platform_rate="1.2",
        ),
        {
            "provider_response": {
                "usage": {
                    "prompt_tokens": 786,
                    "completion_tokens": 1692,
                }
            }
        },
        remark_prefix="文本生成",
    )

    assert task_record.points_cost == 5
    assert task_record.extra["points_settled"] is True
    assert task_record.extra["points_billing"] == {
        "billing_type": "apimart_usage_credits",
        "billing_source": "provider_usage",
        "user_points_cost": 5,
        "user_charge_cny": "0.5",
    }
    assert task_record.extra["provider_cost_billing"]["provider_cost_points"] == 4
    assert transactions[0]["amount"] == 5
    assert transactions[0]["transaction_type"] == "refund"


@pytest.mark.asyncio
async def test_only_explicit_actual_cost_settlement_can_overdraw_balance() -> None:
    user = SimpleNamespace(id=uuid4(), points_balance=3)
    result = SimpleNamespace(scalar_one_or_none=lambda: user)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    transaction = await change_user_points(
        db,
        user_id=user.id,
        amount=-5,
        transaction_type="consume",
        allow_negative_balance=True,
    )

    assert user.points_balance == -2
    assert transaction.balance_after == -2

    user.points_balance = 3
    with pytest.raises(AppException) as exc_info:
        await change_user_points(
            db,
            user_id=user.id,
            amount=-5,
            transaction_type="consume",
        )
    assert exc_info.value.code == 40003


@pytest.mark.asyncio
async def test_fixed_or_unmetered_success_marks_precharge_as_final() -> None:
    image_task = SimpleNamespace(points_cost=9, extra={})
    text_task = SimpleNamespace(points_cost=4, extra={})

    await settle_image_task_points(
        SimpleNamespace(),
        image_task,
        _model("image", {}, vendor="comfly", base_points=9),
        {},
        remark_prefix="图像生成",
    )
    await settle_text_task_points(
        SimpleNamespace(),
        text_task,
        _model("text", {}, vendor="comfly", base_points=4),
        {},
        remark_prefix="文本生成",
    )

    assert image_task.extra["points_settled"] is True
    assert image_task.extra["points_billing"]["billing_type"] == "fixed_image"
    assert text_task.extra["points_settled"] is True
    assert text_task.extra["points_billing"]["billing_type"] == "text_precharge_fallback"


def test_text_and_video_base_points_are_minimum_balance_not_precharge_floor() -> None:
    assert default_model_platform_multiplier("apimart", "text") == Decimal("1.2")
    assert default_model_platform_multiplier("apimart", "image") == 1
    assert default_model_platform_multiplier("apimart", "video") == Decimal("1.2")

    text_model = _model("text", {}, base_points=12)
    video_model = _model(
        "video",
        {},
        model_id="seedance-2.5",
        base_points=99,
        platform_rate="1.2",
    )
    image_model = _model("image", {}, base_points=9)

    assert calculate_text_submission_points_cost(text_model) == 0
    assert calculate_video_model_points_cost(video_model, {"duration": 8}) == 96
    assert model_minimum_balance_points(text_model) == 12
    assert model_minimum_balance_points(video_model) == 99
    assert model_minimum_balance_points(image_model) == 0


def test_precharge_recommendation_uses_nearest_rank_p95() -> None:
    summary = summarize_base_points_recommendation(
        [2, 3, 4, 5, 6, 7, 8, 9, 10, 100],
        current_base_points=5,
    )

    assert summary["p50_points"] == 6
    assert summary["p95_points"] == 100
    assert summary["recommended_base_points"] == 100
    assert summary["recommended_precharge_points"] == 100
    assert summary["confidence"] == "medium"
    assert summary["safe_to_apply"] is True


@pytest.mark.asyncio
async def test_apimart_recommendation_uses_current_platform_rate() -> None:
    records = [
        SimpleNamespace(extra={"provider_response": {"credits_cost": "3.104"}}) for _ in range(5)
    ]
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: records),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    model = _model(
        "text",
        {},
        id=uuid4(),
        base_points=30,
        platform_rate="1.2",
    )

    recommendation = await get_ai_model_billing_recommendation(
        db,
        model,
        sample_limit=100,
    )

    assert recommendation["sample_count"] == 5
    assert recommendation["p95_points"] == 27
    assert recommendation["recommended_base_points"] == 27
    assert recommendation["recommended_precharge_points"] == 27
    assert recommendation["suggested_patch"] == {"configuration": {"billing": {"base_points": 27}}}


def test_provider_cost_billing_is_hidden_from_public_task_extra() -> None:
    sanitized = sanitize_public_data(
        {
            "points_billing": {"user_points_cost": 32},
            "provider_cost_billing": {"provider_cost_cny": "2.1728"},
        }
    )

    assert sanitized == {"points_billing": {"user_points_cost": 32}}
