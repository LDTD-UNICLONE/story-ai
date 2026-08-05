import base64
import json
import logging
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime

logger = logging.getLogger(__name__)


class WechatPayClient:
    async def create_native_order(
        self,
        *,
        out_trade_no: str,
        description: str,
        amount_cents: int,
        notify_url: str,
    ) -> Dict[str, Any]:
        if settings.wechat_pay_mock_enabled:
            return {
                "code_url": f"weixin://wxpay/mock/{out_trade_no}",
                "pay_type": "wechat_native",
                "trade_type": "NATIVE",
            }

        payload = {
            "appid": settings.wechat_pay_appid,
            "mchid": settings.wechat_pay_mchid,
            "description": description,
            "out_trade_no": out_trade_no,
            "time_expire": (
                beijing_datetime() + timedelta(minutes=settings.wechat_pay_native_expire_minutes)
            ).isoformat(timespec="seconds"),
            "notify_url": notify_url,
            "amount": {"total": amount_cents, "currency": "CNY"},
        }
        result = await self._request("POST", "/v3/pay/transactions/native", payload)
        return {
            **result,
            "pay_type": "wechat_native",
            "trade_type": "NATIVE",
        }

    async def create_refund(
        self,
        *,
        out_trade_no: str,
        out_refund_no: str,
        reason: str,
        amount_cents: int,
    ) -> Dict[str, Any]:
        if settings.wechat_pay_mock_enabled:
            return {
                "refund_id": f"mock-refund-{out_refund_no}",
                "out_refund_no": out_refund_no,
                "status": "SUCCESS",
            }

        payload: Dict[str, Any] = {
            "out_trade_no": out_trade_no,
            "out_refund_no": out_refund_no,
            "reason": reason[:80],
            "amount": {
                "refund": amount_cents,
                "total": amount_cents,
                "currency": "CNY",
            },
        }
        return await self._request("POST", "/v3/refund/domestic/refunds", payload)

    async def query_order_by_out_trade_no(self, out_trade_no: str) -> Dict[str, Any]:
        if settings.wechat_pay_mock_enabled:
            return {
                "out_trade_no": out_trade_no,
                "trade_state": "NOTPAY",
                "amount": {"total": 0, "currency": "CNY"},
            }
        path = f"/v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={settings.wechat_pay_mchid}"
        return await self._request("GET", path, None)

    async def query_refund_by_out_refund_no(self, out_refund_no: str) -> Dict[str, Any]:
        if settings.wechat_pay_mock_enabled:
            return {
                "out_refund_no": out_refund_no,
                "refund_id": f"mock-refund-{out_refund_no}",
                "status": "SUCCESS",
            }
        path = f"/v3/refund/domestic/refunds/{out_refund_no}"
        return await self._request("GET", path, None)

    def verify_notify_signature(self, *, headers: Dict[str, str], body: bytes) -> None:
        if settings.wechat_pay_mock_enabled:
            return
        if not settings.wechat_pay_platform_cert_path:
            raise AppException("微信支付平台证书未配置", code=50030, status_code=500)

        timestamp = headers.get("wechatpay-timestamp")
        nonce = headers.get("wechatpay-nonce")
        signature = headers.get("wechatpay-signature")
        serial = headers.get("wechatpay-serial")
        if not timestamp or not nonce or not signature:
            raise AppException("微信支付回调签名缺失", code=40030, status_code=400)
        _validate_notify_timestamp(timestamp)

        message = f"{timestamp}\n{nonce}\n{body.decode('utf-8')}\n".encode("utf-8")
        cert_bytes = Path(settings.wechat_pay_platform_cert_path).read_bytes()
        try:
            certificate = x509.load_pem_x509_certificate(cert_bytes)
            if serial and format(certificate.serial_number, "x").lower() != serial.lower():
                raise AppException("微信支付平台证书序列号不匹配", code=40033, status_code=400)
            public_key = certificate.public_key()
        except ValueError:
            public_key = serialization.load_pem_public_key(cert_bytes)
        try:
            public_key.verify(
                base64.b64decode(signature),
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as exc:
            raise AppException("微信支付回调签名无效", code=40031, status_code=400) from exc

    def decrypt_notify_resource(self, resource: Dict[str, Any]) -> Dict[str, Any]:
        try:
            ciphertext = base64.b64decode(resource["ciphertext"])
            nonce = resource["nonce"].encode("utf-8")
            associated_data = resource.get("associated_data", "").encode("utf-8")
            plaintext = AESGCM(settings.wechat_pay_api_v3_key.encode("utf-8")).decrypt(
                nonce,
                ciphertext,
                associated_data,
            )
            return json.loads(plaintext.decode("utf-8"))
        except Exception as exc:
            raise AppException("微信支付回调解密失败", code=40032, status_code=400) from exc

    async def _request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        body = (
            "" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization(method, path, body),
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(
            base_url="https://api.mch.weixin.qq.com",
            timeout=settings.wechat_pay_timeout_seconds,
        ) as client:
            response = await client.request(
                method,
                path,
                content=body.encode("utf-8") if body else None,
                headers=headers,
            )

        if response.status_code >= 400:
            logger.warning(
                "WeChat Pay request failed: status=%s path=%s response=%s",
                response.status_code,
                path,
                response.text,
            )
            raise AppException("微信支付请求失败，请稍后再试", code=50230, status_code=502)
        return response.json() if response.content else {}

    def _authorization(self, method: str, path: str, body: str) -> str:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
        signature = self._sign(message)
        return (
            "WECHATPAY2-SHA256-RSA2048 "
            f'mchid="{settings.wechat_pay_mchid}",'
            f'nonce_str="{nonce}",'
            f'signature="{signature}",'
            f'timestamp="{timestamp}",'
            f'serial_no="{settings.wechat_pay_merchant_serial_no}"'
        )

    def _sign(self, message: str) -> str:
        if not settings.wechat_pay_private_key_path:
            raise AppException("微信支付商户私钥未配置", code=50031, status_code=500)
        private_key = serialization.load_pem_private_key(
            Path(settings.wechat_pay_private_key_path).read_bytes(),
            password=None,
        )
        signature = private_key.sign(
            message.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")


def _validate_notify_timestamp(value: str) -> None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise AppException("微信支付回调时间戳无效", code=40034, status_code=400) from exc
    tolerance = max(1, settings.wechat_pay_notify_tolerance_seconds)
    if abs(int(time.time()) - timestamp) > tolerance:
        raise AppException("微信支付回调已过期", code=40034, status_code=400)

wechat_pay_client = WechatPayClient()
