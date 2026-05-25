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
            raise RuntimeError(f"Error obteniendo info imagen: {response.status_code}")

        data = response.json()
        url_imagen = data.get("url")
        if not url_imagen:
            raise RuntimeError("URL de imagen no encontrada")

        img_response = await client.get(url_imagen, headers=headers)
        if img_response.status_code != 200:
            raise RuntimeError("Error descargando imagen")

        return img_response.content


async def generar_imagen_remodelada(imagen_bytes: bytes, estilo: str = "moderno") -> str:
    settings = get_settings()
    api_key = settings.openai_api_key

    estilos_permitidos = ["moderno", "clasico", "minimalista", "rustico", "industrial"]
    if estilo not in estilos_permitidos:
        estilo = "moderno"

    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=60.0) as client:

        vision_payload = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{imagen_base64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": "Describe this interior space in max 60 words in english: room type, elements, lighting, materials."
                        }
                    ]
                }
            ],
            "max_tokens": 100
        }

        vision_response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=vision_payload,
            headers=headers
        )

        if vision_response.status_code == 200:
            descripcion = vision_response.json()["choices"][0]["message"]["content"]
            logger.info(f"Descripcion: {descripcion[:80]}")
        else:
            logger.error(f"Error Vision: {vision_response.status_code}")
            descripcion = "residential room with walls and floor"

        prompt = (
            f"Professional interior design photo, {estilo} style. "
            f"{descripcion}. "
            f"New premium flooring, renovated walls, modern lighting. "
            f"Photorealistic, no people, architecture magazine quality."
        )

        dalle_payload = {
            "model": "dall-e-2",
            "prompt": prompt[:1000],
            "n": 1,
            "size": "512x512"
        }

        dalle_response = await client.post(
            "https://api.openai.com/v1/images/generations",
            json=dalle_payload,
            headers=headers
        )

        logger.info(f"DALL-E status: {dalle_response.status_code}")
        logger.info(f"DALL-E response: {dalle_response.text[:300]}")

        if dalle_response.status_code == 200:
            url_generada = dalle_response.json()["data"][0]["url"]
            logger.info("Imagen generada exitosamente")
            return url_generada
        else:
            raise RuntimeError(f"DALL-E error: {dalle_response.status_code}")