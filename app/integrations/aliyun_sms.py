import asyncio
import json
import logging
from typing import Dict

from app.core.config import settings
from app.core.exceptions import AppException

logger = logging.getLogger(__name__)


class AliyunSmsClient:
    async def send_register_code(self, phone: str, code: str) -> None:
        if settings.aliyun_sms_mock_enabled:
            logger.info("Aliyun SMS mock register code sent to %s: %s", phone, code)
            return

        access_key_id = settings.aliyun_sms_access_key_id or settings.oss_access_key_id
        access_key_secret = settings.aliyun_sms_access_key_secret or settings.oss_access_key_secret
        if (
            not access_key_id
            or not access_key_secret
            or not settings.aliyun_sms_sign_name
            or not settings.aliyun_sms_register_template_code
        ):
            raise AppException("短信服务未配置", code=50020, status_code=500)

        template_param = {
            settings.aliyun_sms_register_template_param_key: code,
        }
        await asyncio.to_thread(self._send_sms, phone, template_param, access_key_id, access_key_secret)

    def _send_sms(
        self,
        phone: str,
        template_param: Dict[str, str],
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        try:
            from aliyunsdkcore.client import AcsClient
            from aliyunsdkcore.request import CommonRequest
        except ImportError as exc:
            raise AppException("短信服务依赖未安装", code=50022, status_code=500) from exc

        client = AcsClient(
            access_key_id,
            access_key_secret,
            settings.aliyun_sms_region_id,
        )
        request = CommonRequest()
        request.set_accept_format("json")
        request.set_domain("dysmsapi.aliyuncs.com")
        request.set_method("POST")
        request.set_protocol_type("https")
        request.set_version("2017-05-25")
        request.set_action_name("SendSms")
        request.add_query_param("RegionId", settings.aliyun_sms_region_id)
        request.add_query_param("PhoneNumbers", phone)
        request.add_query_param("SignName", settings.aliyun_sms_sign_name)
        request.add_query_param("TemplateCode", settings.aliyun_sms_register_template_code)
        request.add_query_param("TemplateParam", json.dumps(template_param, ensure_ascii=False))

        try:
            raw_response = client.do_action_with_exception(request)
            response = json.loads(raw_response.decode("utf-8") if isinstance(raw_response, bytes) else raw_response)
        except Exception as exc:
            logger.exception("Aliyun SMS send failed")
            raise AppException("短信发送失败，请稍后再试", code=50021, status_code=502) from exc

        if response.get("Code") != "OK":
            logger.warning("Aliyun SMS send failed: %s", response)
            raise AppException("短信发送失败，请稍后再试", code=50021, status_code=502)


aliyun_sms_client = AliyunSmsClient()
