import logging
import base64
import httpx
from openai import OpenAI
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
    client = OpenAI(api_key=settings.openai_api_key)

    estilos_permitidos = ["moderno", "clasico", "minimalista", "rustico", "industrial"]
    if estilo not in estilos_permitidos:
        estilo = "moderno"

    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")

    try:
        vision_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
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
                                "Describe this interior space for design purposes: "
                                "room type, elements, lighting and materials. "
                                "Max 80 words in english."
                            )
                        }
                    ]
                }
            ],
            max_tokens=120
        )
        descripcion = vision_response.choices[0].message.content
        logger.info(f"Descripcion: {descripcion[:80]}...")

    except Exception as e:
        logger.error(f"Error GPT-4o Vision: {e}")
        descripcion = "residential room with walls and floor"

    prompt_dalle = (
        f"Professional interior design, {estilo} style. "
        f"{descripcion}. "
        f"New premium flooring, clean walls, modern lighting, "
        f"contemporary furniture. Photorealistic, no people."
    )

    try:
        dalle_response = client.images.generate(
            model="dall-e-2",
            prompt=prompt_dalle[:1000],
            size="512x512",
            n=1
        )
        url_generada = dalle_response.data[0].url
        logger.info("Imagen generada con DALL-E 2")
        return url_generada

    except Exception as e:
        logger.error(f"Error DALL-E 2: {type(e).__name__} - {e}")
        raise RuntimeError("Error al generar la visualizacion")