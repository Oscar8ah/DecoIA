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


async def describir_imagen_con_gpt(imagen_bytes: bytes, api_key: str) -> str:
    """Usa GPT-4o Vision para describir el espacio"""
    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
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
                        "text": (
                            "Describe this interior space in english, max 60 words: "
                            "room type, floor material, wall color, furniture, lighting. "
                            "Focus on architectural details."
                        )
                    }
                ]
            }
        ],
        "max_tokens": 100
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers=headers
        )
        if response.status_code == 200:
            descripcion = response.json()["choices"][0]["message"]["content"]
            logger.info(f"Descripcion GPT-4o: {descripcion[:80]}")
            return descripcion
        else:
            logger.error(f"Error GPT-4o: {response.status_code}")
            return "modern residential room with concrete walls and tile floor"


async def generar_imagen_stability(prompt: str, api_key: str) -> bytes:
    """Genera imagen con Stability AI"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "image/*"
    }

    data = {
        "prompt": prompt,
        "output_format": "jpeg",
        "width": 1024,
        "height": 1024,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.stability.ai/v2beta/stable-image/generate/core",
            headers=headers,
            data=data
        )

        logger.info(f"Stability AI status: {response.status_code}")

        if response.status_code == 200:
            return response.content
        else:
            logger.error(f"Stability error: {response.text[:200]}")
            raise RuntimeError(f"Stability AI error: {response.status_code}")


async def subir_imagen_a_imgbb(imagen_bytes: bytes) -> str:
    """Sube imagen a imgbb para obtener URL publica"""
    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.imgbb.com/1/upload",
            data={
                "key": "0e3e4e4e4e4e4e4e4e4e4e4e4e4e4e4e",
                "image": imagen_base64
            }
        )
        if response.status_code == 200:
            return response.json()["data"]["url"]
        else:
            raise RuntimeError("Error subiendo imagen a imgbb")


async def generar_imagen_remodelada(imagen_bytes: bytes, estilo: str = "moderno") -> str:
    """
    Pipeline completo:
    1. GPT-4o describe el espacio
    2. Stability AI genera la remodelacion
    3. Retorna URL publica de la imagen
    """
    settings = get_settings()

    estilos_map = {
        "moderno": "modern minimalist",
        "clasico": "classic elegant",
        "minimalista": "ultra minimalist",
        "rustico": "rustic warm",
        "industrial": "industrial loft"
    }
    estilo_en = estilos_map.get(estilo, "modern minimalist")

    descripcion = await describir_imagen_con_gpt(imagen_bytes, settings.openai_api_key)

    prompt = (
        f"Professional interior design photograph, {estilo_en} style renovation. "
        f"Original space: {descripcion}. "
        f"Transform with: luxury hardwood flooring, freshly painted walls, "
        f"recessed LED lighting, contemporary furniture, premium finishes. "
        f"Photorealistic, 8K quality, architectural magazine style, "
        f"warm natural light, no people."
    )

    logger.info(f"Prompt Stability: {prompt[:100]}")

    imagen_generada = await generar_imagen_stability(prompt, settings.stability_api_key)
    url_imagen = await subir_imagen_a_imgbb(imagen_generada)

    return url_imagen