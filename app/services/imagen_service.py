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
                            "Describe this interior space in english, max 50 words: "
                            "room type, floor material, wall color, furniture style, lighting. "
                            "Be specific and concise."
                        )
                    }
                ]
            }
        ],
        "max_tokens": 80
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
            return "residential room"


async def transformar_imagen_stability(imagen_bytes: bytes, prompt: str, api_key: str) -> bytes:
    """
    Usa Stability AI img2img para transformar la imagen original
    manteniendo la estructura y cambiando materiales y acabados
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "image/*"
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            "https://api.stability.ai/v2beta/stable-image/control/structure",
            headers=headers,
            files={
                "image": ("room.jpg", imagen_bytes, "image/jpeg"),
            },
            data={
                "prompt": prompt,
                "control_strength": "0.7",
                "output_format": "jpeg",
            }
        )

        logger.info(f"Stability img2img status: {response.status_code}")

        if response.status_code == 200:
            return response.content
        else:
            logger.error(f"Stability error: {response.text[:300]}")
            raise RuntimeError(f"Stability AI error: {response.status_code}")


async def subir_imagen_a_imgbb(imagen_bytes: bytes, imgbb_key: str) -> str:
    """Sube imagen a imgbb para obtener URL publica"""
    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.imgbb.com/1/upload",
            data={
                "key": imgbb_key,
                "image": imagen_base64
            }
        )
        if response.status_code == 200:
            return response.json()["data"]["url"]
        else:
            logger.error(f"imgbb error: {response.text[:200]}")
            raise RuntimeError("Error subiendo imagen a imgbb")


async def generar_imagen_remodelada(imagen_bytes: bytes, estilo: str = "moderno") -> str:
    """
    Pipeline img2img:
    1. GPT-4o describe el espacio original
    2. Stability AI transforma la imagen manteniendo estructura
    3. imgbb aloja la imagen
    4. Retorna URL publica
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
        f"Interior design renovation, {estilo_en} style. "
        f"Same room structure and perspective. "
        f"Replace flooring with luxury hardwood, "
        f"repaint walls with modern colors, "
        f"add recessed LED lighting, premium finishes. "
        f"Keep exact same camera angle and room layout. "
        f"Photorealistic, no people, 8K quality."
    )

    logger.info(f"Transformando imagen con prompt: {prompt[:100]}")

    imagen_transformada = await transformar_imagen_stability(
        imagen_bytes, prompt, settings.stability_api_key
    )

    url_imagen = await subir_imagen_a_imgbb(imagen_transformada, settings.imgbb_api_key)

    return url_imagen