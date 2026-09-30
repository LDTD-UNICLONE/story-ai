import time

import pytest

from app.core.exceptions import AppException
from app.integrations.wechat_pay import _validate_notify_timestamp
from app.services.billing import recharges


def test_wechat_notify_rejects_stale_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(recharges.settings, "wechat_pay_notify_tolerance_seconds", 300)

    with pytest.raises(AppException, match="已过期"):
        _validate_notify_timestamp(str(int(time.time()) - 301))


def test_paid_transaction_requires_expected_merchant_and_currency(monkeypatch) -> None:
    monkeypatch.setattr(recharges.settings, "wechat_pay_mock_enabled", False)
    monkeypatch.setattr(recharges.settings, "wechat_pay_appid", "expected-app")
    monkeypatch.setattr(recharges.settings, "wechat_pay_mchid", "expected-merchant")
    valid = {
        "appid": "expected-app",
        "mchid": "expected-merchant",
        "amount": {"total": 100, "currency": "CNY"},
    }

    recharges._validate_wechat_paid_transaction(valid)

    with pytest.raises(AppException, match="商户号不一致"):
        recharges._validate_wechat_paid_transaction({**valid, "mchid": "other"})

    with pytest.raises(AppException, match="支付币种不一致"):
        recharges._validate_wechat_paid_transaction(
            {**valid, "amount": {"total": 100, "currency": "USD"}}
        )
