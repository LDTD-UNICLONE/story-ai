from fastapi.responses import RedirectResponse

from app.integrations.oss import OssClient
from app.models.material import Material


async def stream_material_image(material: Material) -> RedirectResponse:
    oss_client = OssClient()
    signed_url = oss_client.signed_download_url(
        material.image_object_key,
        material.filename,
    )
    return RedirectResponse(
        signed_url,
        status_code=307,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
