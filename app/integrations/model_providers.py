from app.core.config import settings
from app.integrations.apimart import close_apimart_client, init_apimart_client
from app.integrations.comfly import close_comfly_client, init_comfly_client
from app.integrations.volcengine_ark import (
    close_volcengine_ark_client,
    init_volcengine_ark_client,
)


async def init_model_provider_clients() -> None:
    await init_comfly_client()
    if settings.volcengine_ark_api_key:
        await init_volcengine_ark_client()
    if settings.apimart_api_key:
        await init_apimart_client()


async def close_model_provider_clients() -> None:
    await close_comfly_client()
    await close_volcengine_ark_client()
    await close_apimart_client()
