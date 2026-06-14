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
    """
    img = Image.open(io.BytesIO(imagen_bytes)).convert("RGBA")
    img = img.resize((1024, 1024), Image.LANCZOS)
    ancho, alto = 1024, 1024

    mascara = Image.new("RGBA", (ancho, alto), (0, 0, 0, 255))
    draw = ImageDraw.Draw(mascara)

    # PISO: 38% inferior → editable
    piso_y = int(alto * 0.62)
    draw.rectangle([0, piso_y, ancho, alto], fill=(0, 0, 0, 0))

    # PARED IZQUIERDA
    draw.rectangle(
        [0, int(alto * 0.45), int(ancho * 0.18), int(alto * 0.62)],
        fill=(0, 0, 0, 0)
    )

    # PARED DERECHA
    draw.rectangle(
        [int(ancho * 0.82), int(alto * 0.45), ancho, int(alto * 0.62)],
        fill=(0, 0, 0, 0)
    )

    # PARED FONDO BAJA
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
        "moderno":     "modern minimalist style with white walls and light oak hardwood floors",
        "clasico":     "classic elegant style with beige walls and dark walnut hardwood floors",
        "minimalista": "ultra minimalist style with grey walls and light concrete floors",
        "rustico":     "rustic warm style with exposed brick walls and dark wood plank floors",
        "industrial":  "industrial loft style with grey concrete walls and polished cement floors",
    }
    estilo_en = estilos_map.get(estilo, estilos_map["moderno"])

    imagen_png  = imagen_a_png_1024(imagen_bytes)
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
                "image": ("room.png", imagen_png,  "image/png"),
                "mask":  ("mask.png", mascara_png, "image/png"),
            },
            data={
                "model":   "gpt-image-1",
                "prompt":  prompt,
                "n":       "1",
                "size":    "1024x1024",
                "quality": "medium",
            }
        )

        logger.info(f"GPT-image-1 status: {response.status_code}")
        logger.info(f"GPT-image-1 response: {response.text[:300]}")

        if response.status_code == 200:
            data = response.json()
            imagen_b64 = data["data"][0].get("b64_json")
            if imagen_b64:
                resultado_bytes = base64.b64decode(imagen_b64)
                return await subir_imagen_a_imgbb(resultado_bytes, settings.imgbb_api_key)
            else:
                return data["data"][0].get("url")
        else:
            logger.error(f"Error gpt-image-1: {response.text[:500]}")
            raise RuntimeError(f"Error generando imagen: {response.status_code}")


async def generar_imagen_con_producto(
    foto_bytes: bytes,
    producto_bytes: bytes,
    producto_nombre: str,
    categoria: str = "material",
) -> str:
    """
    Aplica un producto específico (piso, enchape, pintura) a la foto del espacio.
    Usa la imagen del producto como referencia visual para gpt-image-1.
    """
    settings = get_settings()

    instrucciones = {
        "pisos":      f"Replace the floor with the exact flooring material shown in the reference image: {producto_nombre}. Keep all furniture and walls unchanged.",
        "enchapes":   f"Apply the tile/enchape material from the reference image ({producto_nombre}) to the walls and floor. Keep furniture unchanged.",
        "pintura":    f"Paint the walls with the exact color and finish shown in the reference image: {producto_nombre}. Keep all furniture and floor unchanged.",
        "materiales": f"Apply the material from the reference image ({producto_nombre}) to the floor. Keep everything else unchanged.",
    }
    instruccion = instrucciones.get(
        categoria,
        f"Apply the product from the reference image ({producto_nombre}) to the space. Keep furniture unchanged."
    )

    prompt = (
        f"Interior design renovation. {instruccion} "
        f"Photorealistic result. Professional architectural photography. "
        f"Same room layout, same furniture positions, same lighting angle. "
        f"No text, no watermarks."
    )

    imagen_png  = imagen_a_png_1024(foto_bytes)
    mascara_png = crear_mascara_piso_paredes(foto_bytes)

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={
                "image": ("room.png", imagen_png,  "image/png"),
                "mask":  ("mask.png", mascara_png, "image/png"),
            },
            data={
                "model":   "gpt-image-1",
                "prompt":  prompt,
                "n":       "1",
                "size":    "1024x1024",
                "quality": "high",
            }
        )

        logger.info(f"generar_imagen_con_producto status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            imagen_b64 = data["data"][0].get("b64_json")
            if imagen_b64:
                resultado_bytes = base64.b64decode(imagen_b64)
                return await subir_imagen_a_imgbb(resultado_bytes, settings.imgbb_api_key)
            else:
                return data["data"][0].get("url")
        else:
            logger.error(f"Error generar_imagen_con_producto: {response.text[:300]}")
            raise RuntimeError(f"Error aplicando producto: {response.status_code}")


async def generar_vista_isometrica(imagen_bytes: bytes, info_plano: dict) -> str:
    """
    Genera una vista 3D isométrica a partir de un plano 2D.
    Máxima fidelidad arquitectónica al plano original.
    """
    settings = get_settings()

    tipo         = info_plano.get("tipo_plano", "apartamento")
    habitaciones = info_plano.get("habitaciones", "")
    area         = info_plano.get("area_estimada", "por determinar")
    distribucion = info_plano.get("distribucion", "")
    num_banos    = info_plano.get("num_banos", "")
    tiene_cocina = info_plano.get("tiene_cocina", True)
    tiene_sala   = info_plano.get("tiene_sala", True)

    validacion = (
        f"FLOOR PLAN VALIDATION — match exactly:\n"
        f"- Property type: {tipo}\n"
        f"- Detected rooms: {habitaciones}\n"
        f"- Detected bathrooms: {num_banos}\n"
        f"- Kitchen detected: {'Yes' if tiene_cocina else 'No'}\n"
        f"- Living room detected: {'Yes' if tiene_sala else 'No'}\n"
        f"- Estimated area: {area}\n"
        f"- Layout: {distribucion}\n"
    )

    prompt = (
        "You are a specialized technical architect focused on converting 2D floor plans "
        "into precise isometric 3D visualizations. "
        "Architectural accuracy is your absolute priority.\n\n"
        "OBJECTIVE: Convert the attached 2D architectural floor plan into a technically "
        "accurate isometric 3D view that faithfully preserves the original layout.\n\n"
        "PRIMARY RULE: Geometric precision over aesthetics. "
        "Reproduce EXACTLY what is shown in the floor plan. Do NOT invent, add, remove, "
        "or relocate any architectural element.\n\n"
        f"{validacion}\n"
        "MANDATORY INSTRUCTIONS:\n"
        "1. Analyze carefully: exterior walls, interior walls, rooms, bathrooms, "
        "kitchen, living room, dining room, corridors, doors and windows.\n"
        "2. Preserve EXACTLY: number of rooms, number of bathrooms, relative position "
        "of each space, general shape of the property, internal layout, door positions, "
        "window positions, and spatial relationships between areas.\n"
        "3. STRICTLY PROHIBITED: adding rooms, removing rooms, moving walls, "
        "changing proportions, reorganizing spaces, inventing architectural elements.\n"
        "4. If ambiguity exists: maintain the original geometry.\n"
        "5. BEFORE generating: verify room count, bathroom count, kitchen and living room presence.\n\n"
        "OUTPUT SPECIFICATIONS:\n"
        "- View angle: isometric 45-degree architectural view.\n"
        "- Show: walls, doors, windows, and exact room distribution.\n"
        "- Style: realistic architectural 3D render with warm colors. "
        "Use natural wood floors (light oak), warm white walls, modern furniture in grey and beige tones. "
        "Add realistic textures and materials. Vibrant and photorealistic, not a technical drawing.\n"
        "- Lighting: soft uniform architectural lighting, no dramatic shadows.\n"
        "- Quality: professional architectural visualization.\n\n"
        "REQUIRED FIDELITY LEVEL: 95% to 100%.\n"
        "PRIORITY ORDER: Architectural fidelity > Visual aesthetics."
    )

    imagen_png = imagen_a_png_1024(imagen_bytes)

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            files={
                "image": ("plano.png", imagen_png, "image/png"),
            },
            data={
                "model":   "gpt-image-1",
                "prompt":  prompt,
                "n":       "1",
                "size":    "1024x1024",
                "quality": "high",
            }
        )

        logger.info(f"Vista isométrica status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            imagen_b64 = data["data"][0].get("b64_json")
            if imagen_b64:
                resultado_bytes = base64.b64decode(imagen_b64)
                return await subir_imagen_a_imgbb(resultado_bytes, settings.imgbb_api_key)
            else:
                return data["data"][0].get("url")
        else:
            logger.error(f"Error vista isométrica: {response.text[:500]}")
            raise RuntimeError(f"Error generando vista isométrica: {response.status_code}")