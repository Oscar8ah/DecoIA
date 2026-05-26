import logging
import base64
import httpx
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


async def descargar_imagen_whatsapp(image_id: str, token: str) -> bytes:
    if not image_id or not token:
        raise ValueError("image_id y token son requeridos")

    url_info = f"https://graph.facebook.com/v25.0/{image_id}"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(url_info, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"Error obteniendo info: {response.status_code}")

        data = response.json()
        url_imagen = data.get("url")
        if not url_imagen:
            raise RuntimeError("URL no encontrada")

        img_response = await client.get(url_imagen, headers=headers)
        if img_response.status_code != 200:
            raise RuntimeError("Error descargando imagen")

        return img_response.content


async def subir_imagen_a_imgbb(imagen_bytes: bytes, imgbb_key: str) -> str:
    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.imgbb.com/1/upload",
            data={"key": imgbb_key, "image": imagen_base64}
        )
        if response.status_code == 200:
            return response.json()["data"]["url"]
        raise RuntimeError("Error subiendo a imgbb")


async def generar_imagen_remodelada(imagen_bytes: bytes, estilo: str = "moderno") -> str:
    settings = get_settings()

    estilos_map = {
        "moderno": "modern minimalist style",
        "clasico": "classic elegant style",
        "minimalista": "ultra minimalist style",
        "rustico": "rustic warm style",
        "industrial": "industrial loft style"
    }
    estilo_en = estilos_map.get(estilo, "modern minimalist style")

    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-image-1",
        "prompt": (
            f"Interior design renovation in {estilo_en}. "
            f"Keep EXACT same room, same perspective, same furniture position, same walls structure. "
            f"ONLY change: floor to luxury hardwood, walls to fresh modern paint, "
            f"add recessed LED lighting. Do NOT change room layout or add new furniture. "
            f"Photorealistic result."
        ),
        "n": 1,
        "size": "1024x1024",
        "quality": "medium"
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={
                "image": ("room.jpg", imagen_bytes, "image/jpeg"),
            },
            data={
                "model": "gpt-image-1",
                "prompt": (
                    f"Interior design renovation {estilo_en}. "
                    f"Keep EXACT same room structure, same perspective. "
                    f"Only change floor to luxury hardwood, repaint walls, "
                    f"add modern LED lighting. Photorealistic."
                ),
                "n": "1",
                "size": "1024x1024",
            }
        )

        logger.info(f"GPT-image-1 status: {response.status_code}")
        logger.info(f"GPT-image-1 response: {response.text[:300]}")

        if response.status_code == 200:
            data = response.json()
            imagen_b64 = data["data"][0].get("b64_json")
            if imagen_b64:
                imagen_bytes_result = base64.b64decode(imagen_b64)
                url = await subir_imagen_a_imgbb(imagen_bytes_result, settings.imgbb_api_key)
                return url
            else:
                url_directa = data["data"][0].get("url")
                return url_directa
        else:
            logger.error(f"Error gpt-image-1: {response.text[:500]}")
            raise RuntimeError(f"Error generando imagen: {response.status_code}")