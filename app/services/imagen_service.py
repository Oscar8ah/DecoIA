import logging
import base64
import httpx
import io
from PIL import Image, ImageDraw
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


def buffer_desde_mascara(mascara: Image.Image) -> bytes:
    buffer = io.BytesIO()
    mascara.save(buffer, format="PNG")
    return buffer.getvalue()


def crear_mascara_piso_paredes(imagen_bytes: bytes) -> bytes:
    """
    Máscara PNG con canal alpha:
    - Transparente (alpha=0)  → EDITABLE (piso + paredes sin decoración)
    - Opaco (alpha=255)       → PROTEGIDO (muebles, cuadros, ventanas, objetos)

    Zonas protegidas:
    - Centro horizontal: muebles y objetos
    - Franja superior completa: cuadros colgados en paredes
    """
    img = Image.open(io.BytesIO(imagen_bytes)).convert("RGBA")
    img = img.resize((1024, 1024), Image.LANCZOS)
    ancho, alto = 1024, 1024

    # Empezamos todo OPACO (protegido)
    mascara = Image.new("RGBA", (ancho, alto), (0, 0, 0, 255))
    draw = ImageDraw.Draw(mascara)

    # PISO: 38% inferior → editable
    piso_y = int(alto * 0.62)
    draw.rectangle([0, piso_y, ancho, alto], fill=(0, 0, 0, 0))

    # PARED IZQUIERDA: franja lateral izquierda (entre cuadros y piso)
    draw.rectangle(
        [0, int(alto * 0.45), int(ancho * 0.18), int(alto * 0.62)],
        fill=(0, 0, 0, 0)
    )

    # PARED DERECHA: franja lateral derecha (entre cuadros y piso)
    draw.rectangle(
        [int(ancho * 0.82), int(alto * 0.45), ancho, int(alto * 0.62)],
        fill=(0, 0, 0, 0)
    )

    # PARED FONDO BAJA: zona debajo de cuadros, encima de muebles
    draw.rectangle(
        [int(ancho * 0.15), int(alto * 0.40), int(ancho * 0.85), int(alto * 0.55)],
        fill=(0, 0, 0, 0)
    )

    return buffer_desde_mascara(mascara)


def imagen_a_png_1024(imagen_bytes: bytes) -> bytes:
    """Convierte imagen a PNG 1024x1024 que requiere gpt-image-1"""
    img = Image.open(io.BytesIO(imagen_bytes)).convert("RGBA")
    img = img.resize((1024, 1024), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


async def generar_imagen_remodelada(imagen_bytes: bytes, estilo: str = "moderno") -> str:
    settings = get_settings()

    estilos_map = {
        "moderno": "modern minimalist style with white walls and light oak hardwood floors",
        "clasico": "classic elegant style with beige walls and dark walnut hardwood floors",
        "minimalista": "ultra minimalist style with grey walls and light concrete floors",
        "rustico": "rustic warm style with exposed brick walls and dark wood plank floors",
        "industrial": "industrial loft style with grey concrete walls and polished cement floors"
    }
    estilo_en = estilos_map.get(estilo, "modern minimalist style with white walls and light oak hardwood floors")

    # Preparar imagen y máscara
    imagen_png = imagen_a_png_1024(imagen_bytes)
    mascara_png = crear_mascara_piso_paredes(imagen_bytes)

    prompt = (
        f"Interior design renovation: {estilo_en}. "
        f"Apply new flooring and wall paint ONLY in the transparent mask areas. "
        f"Keep ALL furniture, windows, doors, picture frames and objects exactly "
        f"in their original positions. "
        f"Photorealistic lighting. Do not move or add any furniture."
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={
                "image": ("room.png", imagen_png, "image/png"),
                "mask":  ("mask.png",  mascara_png, "image/png"),
            },
            data={
                "model": "gpt-image-1",
                "prompt": prompt,
                "n": "1",
                "size": "1024x1024",
                "quality": "medium",
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
                return data["data"][0].get("url")
        else:
            logger.error(f"Error gpt-image-1: {response.text[:500]}")
            raise RuntimeError(f"Error generando imagen: {response.status_code}")