import logging
import base64
import httpx
from openai import OpenAI
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


async def descargar_imagen_whatsapp(image_id: str, token: str) -> bytes:
    """Descarga imagen desde WhatsApp Cloud API"""
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
    """
    1. Usa GPT-4o Vision para describir el espacio
    2. Usa DALL-E 3 para generar la version remodelada
    """
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
                                "Describe este espacio en detalle para un prompt de diseño de interiores: "
                                "tipo de habitación, dimensiones aproximadas, distribución, "
                                "elementos presentes, iluminación y materiales actuales. "
                                "Responde solo con la descripción, sin comentarios adicionales."
                            )
                        }
                    ]
                }
            ],
            max_tokens=300
        )

        descripcion = vision_response.choices[0].message.content
        logger.info(f"Descripción del espacio: {descripcion[:100]}...")

    except Exception as e:
        logger.error(f"Error en GPT-4o Vision: {e}")
        descripcion = "habitación residencial con paredes y piso"

    prompt_dalle = (
        f"Fotografía arquitectónica profesional de diseño de interiores estilo {estilo}. "
        f"Espacio: {descripcion}. "
        f"Aplicar: pisos nuevos de alta gama, paredes renovadas, iluminación moderna LED, "
        f"acabados premium, muebles contemporáneos. "
        f"Fotorrealista, luz natural cálida, calidad revista de arquitectura. "
        f"Sin personas, vista frontal clara."
    )

    try:
        dalle_response = client.images.generate(
            model="dall-e-3",
            prompt=prompt_dalle[:1000],
            size="1024x1024",
            quality="standard",
            n=1
        )
        url_generada = dalle_response.data[0].url
        logger.info("Imagen generada exitosamente con DALL-E 3")
        return url_generada

    except Exception as e:
        logger.error(f"Error en DALL-E 3: {type(e).__name__} - {e}")
        raise RuntimeError("Error al generar la visualizacion")