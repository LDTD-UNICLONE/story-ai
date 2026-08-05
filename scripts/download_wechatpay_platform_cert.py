#!/usr/bin/env python3
import argparse
import base64
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings  # noqa: E402

WECHAT_PAY_HOST = "https://api.mch.weixin.qq.com"
CERTIFICATES_PATH = "/v3/certificates"


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="下载并解密微信支付平台证书")
    parser.add_argument(
        "--output",
        default=settings.wechat_pay_platform_cert_path or "key/wechatpay_platform.pem",
        help="平台证书保存路径，默认使用 WECHAT_PAY_PLATFORM_CERT_PATH",
    )
    parser.add_argument(
        "--save-all",
        action="store_true",
        help="同时按序列号保存接口返回的所有平台证书",
    )
    args = parser.parse_args()

    _validate_config()
    output_path = Path(args.output)
    certificates = _fetch_certificates()
    selected = _select_latest_certificate(certificates)
    _write_certificate(output_path, selected["certificate"])

    if args.save_all:
        for item in certificates:
            serial_no = item["serial_no"]
            all_path = output_path.with_name(f"{output_path.stem}_{serial_no}{output_path.suffix}")
            _write_certificate(all_path, item["certificate"])

    print("微信支付平台证书下载完成")
    print(f"保存路径: {output_path}")
    print(f"平台证书序列号: {selected['serial_no']}")
    print(f"生效时间: {selected.get('effective_time') or '-'}")
    print(f"过期时间: {selected.get('expire_time') or '-'}")


def _validate_config() -> None:
    missing = []
    if not settings.wechat_pay_mchid:
        missing.append("WECHAT_PAY_MCHID")
    if not settings.wechat_pay_merchant_serial_no:
        missing.append("WECHAT_PAY_MERCHANT_SERIAL_NO")
    if not settings.wechat_pay_private_key_path:
        missing.append("WECHAT_PAY_PRIVATE_KEY_PATH")
    if not settings.wechat_pay_api_v3_key:
        missing.append("WECHAT_PAY_API_V3_KEY")
    if missing:
        raise SystemExit(f"缺少微信支付配置: {', '.join(missing)}")
    if len(settings.wechat_pay_api_v3_key.encode("utf-8")) != 32:
        raise SystemExit("WECHAT_PAY_API_V3_KEY 必须是 32 字节 APIv3 密钥")
    private_key_path = Path(settings.wechat_pay_private_key_path)
    if not private_key_path.exists():
        raise SystemExit(f"商户私钥文件不存在: {private_key_path}")


def _fetch_certificates() -> list[Dict[str, Any]]:
    response = httpx.get(
        f"{WECHAT_PAY_HOST}{CERTIFICATES_PATH}",
        headers={
            "Accept": "application/json",
            "Authorization": _authorization("GET", CERTIFICATES_PATH, ""),
        },
        timeout=settings.wechat_pay_timeout_seconds,
    )
    if response.status_code >= 400:
        raise SystemExit(f"微信支付平台证书下载失败: HTTP {response.status_code} {response.text}")

    data = response.json().get("data") or []
    if not data:
        raise SystemExit("微信支付未返回平台证书")

    certificates = []
    for item in data:
        resource = item.get("encrypt_certificate") or {}
        certificate = _decrypt_certificate(resource)
        parsed = x509.load_pem_x509_certificate(certificate.encode("utf-8"))
        serial_no = format(parsed.serial_number, "X")
        certificates.append(
            {
                "serial_no": serial_no,
                "effective_time": item.get("effective_time"),
                "expire_time": item.get("expire_time"),
                "certificate": certificate,
            }
        )
    return certificates


def _decrypt_certificate(resource: Dict[str, str]) -> str:
    try:
        ciphertext = base64.b64decode(resource["ciphertext"])
        nonce = resource["nonce"].encode("utf-8")
        associated_data = resource.get("associated_data", "").encode("utf-8")
        plaintext = AESGCM(settings.wechat_pay_api_v3_key.encode("utf-8")).decrypt(
            nonce,
            ciphertext,
            associated_data,
        )
    except Exception as exc:
        raise SystemExit(f"微信支付平台证书解密失败: {exc}") from exc
    return plaintext.decode("utf-8")


def _select_latest_certificate(certificates: list[Dict[str, Any]]) -> Dict[str, Any]:
    return sorted(certificates, key=lambda item: item.get("effective_time") or "", reverse=True)[0]


def _write_certificate(path: Path, certificate: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(certificate, encoding="utf-8")
    path.chmod(0o600)


def _authorization(method: str, path: str, body: str) -> str:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
    signature = _sign(message)
    return (
        "WECHATPAY2-SHA256-RSA2048 "
        f'mchid="{settings.wechat_pay_mchid}",'
        f'nonce_str="{nonce}",'
        f'signature="{signature}",'
        f'timestamp="{timestamp}",'
        f'serial_no="{settings.wechat_pay_merchant_serial_no}"'
    )


def _sign(message: str) -> str:
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


if __name__ == "__main__":
    main()
