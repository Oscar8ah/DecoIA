import logging
import httpx
from openai import OpenAI
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


async def descargar_imagen_whatsapp(image_id: str, token: str) -> bytes:
    """Descarga imagen desde WhatsApp Cloud API - OWASP: validacion de origen"""
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
    Genera visualizacion de remodelacion usando OpenAI
    Retorna URL de imagen generada
    """
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)

    estilos_permitidos = ["moderno", "clasico", "minimalista", "rustico", "industrial"]
    if estilo not in estilos_permitidos:
        estilo = "moderno"

    prompt = f"""
    Renderiza este espacio con un diseño de interiores {estilo} de alta calidad.
    Mantén la misma perspectiva y estructura de la habitación.
    Aplica pisos nuevos, paredes limpias, iluminación moderna y acabados premium.
    Estilo fotorrealista, luz natural, calidad arquitectónica profesional.
    """

    try:
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt.strip(),
            size="1024x1024",
            quality="standard",
            n=1
        )
        url_generada = response.data[0].url
        logger.info("Imagen generada exitosamente con DALL-E")
        return url_generada

    except Exception as e:
        logger.error(f"Error generando imagen: {type(e).__name__}")
        raise RuntimeError("Error al generar la visualizacion")