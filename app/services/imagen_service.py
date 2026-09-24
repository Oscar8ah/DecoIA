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


# Errores que NO son culpa del usuario ni de la imagen: son caídas pasajeras
# del proveedor. Un 500 de OpenAI mataba la conversación entera y el cliente
# veía "Hubo un error, intenta de nuevo" — si eso le pasa al cliente de un
# ferretero, se va. Se reintenta solo, sin que se entere.
ESTADOS_REINTENTABLES = {429, 500, 502, 503, 504}


async def _post_con_reintentos(client, url, *, headers, files=None, data=None,
                               intentos=3, espera_base=2.0, etiqueta="OpenAI"):
    """
    Reintenta solo ante fallos pasajeros del servidor, con espera creciente.
    Un 400 (imagen inválida, moderación) NO se reintenta: sería gastar dinero
    repitiendo algo que va a fallar igual.
    """
    import asyncio
    ultima = None
    for intento in range(1, intentos + 1):
        try:
            r = await client.post(url, headers=headers, files=files, data=data)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            ultima = e
            logger.warning(f"{etiqueta}: intento {intento}/{intentos} falló por red — {type(e).__name__}")
            if intento == intentos:
                raise
            await asyncio.sleep(espera_base * intento)
            continue

        if r.status_code in ESTADOS_REINTENTABLES and intento < intentos:
            logger.warning(
                f"{etiqueta}: {r.status_code} (caída pasajera del proveedor). "
                f"Reintento {intento}/{intentos - 1} en {espera_base * intento:.0f}s"
            )
            await asyncio.sleep(espera_base * intento)
            continue
        return r
    return ultima if isinstance(ultima, httpx.Response) else r


async def subir_imagen_a_supabase(imagen_bytes: bytes, carpeta: str = "whatsapp") -> str:
    """
    Respaldo cuando imgbb falla. Se usa el mismo bucket 'portafolio' y el mismo
    patrón que render3d.py, que ya funciona en producción.
    """
    from app.utils.supabase_client import get_supabase
    import uuid as _uuid
    supabase = get_supabase()
    ruta = f"{carpeta}/{_uuid.uuid4()}.png"
    supabase.storage.from_("portafolio").upload(
        ruta, imagen_bytes, {"content-type": "image/png", "upsert": "true"}
    )
    return supabase.storage.from_("portafolio").get_public_url(ruta)


async def subir_imagen_a_imgbb(imagen_bytes: bytes, imgbb_key: str) -> str:
    """
    Sube una imagen y devuelve su URL pública.

    Dos cambios respecto a la versión anterior, los dos por el mismo incidente:
    una foto se generó bien en OpenAI, imgbb devolvió 400, y el cliente recibió
    "Hubo un error" — con la imagen ya generada y pagada.

    1. El error de imgbb se registra con su cuerpo. Antes se lanzaba un
       RuntimeError seco que descartaba justo lo único que explica el fallo
       (clave inválida, imagen muy pesada, límite de la cuenta...), y por eso
       había que adivinar.
    2. Si imgbb falla se cae a Supabase Storage en vez de reventar. Una imagen
       ya generada y cobrada NO se puede perder porque un servicio externo
       gratuito esté de mal humor.
    """
    if not imgbb_key:
        logger.warning("No hay clave de imgbb configurada — se usa Supabase Storage")
        return await subir_imagen_a_supabase(imagen_bytes)

    imagen_base64 = base64.b64encode(imagen_bytes).decode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.imgbb.com/1/upload",
                data={"key": imgbb_key, "image": imagen_base64}
            )
        if response.status_code == 200:
            return response.json()["data"]["url"]
        logger.error(
            f"imgbb devolvió {response.status_code} "
            f"(imagen de {len(imagen_bytes)/1024:.0f} KB): {response.text[:400]}"
        )
    except Exception as e:
        logger.error(f"imgbb no respondió: {type(e).__name__} — {e}")

    logger.info("Cayendo a Supabase Storage para no perder la imagen")
    return await subir_imagen_a_supabase(imagen_bytes)


def buffer_desde_mascara(mascara: Image.Image) -> bytes:
    buffer = io.BytesIO()
    mascara.save(buffer, format="PNG")
    return buffer.getvalue()


# gpt-image-1 solo acepta tres formatos de salida. Se elige el que más se
# parezca a la foto del cliente para no deformarla.
TAMANOS_GPT_IMAGE = {
    "cuadrado": (1024, 1024),
    "vertical": (1024, 1536),
    "apaisado": (1536, 1024),
}


def tamano_para(imagen_bytes: bytes):
    """
    Devuelve (ancho, alto, texto) del formato más cercano a la foto original.

    Antes todo se forzaba a 1024x1024 con un resize directo, SIN conservar la
    proporción: una foto apaisada de 1600x900 se aplastaba a un cuadrado. La IA
    veía una sala achatada, generaba sobre eso, y el resultado volvía deformado
    — muebles estirados y bordes perdidos.
    """
    img = Image.open(io.BytesIO(imagen_bytes))
    w, h = img.size
    prop = w / h if h else 1.0
    if prop >= 1.25:
        clave = "apaisado"
    elif prop <= 0.8:
        clave = "vertical"
    else:
        clave = "cuadrado"
    ancho, alto = TAMANOS_GPT_IMAGE[clave]
    return ancho, alto, f"{ancho}x{alto}"


def ajustar_a_lienzo(img: Image.Image, ancho: int, alto: int) -> Image.Image:
    """
    Encaja la imagen en el lienzo CONSERVANDO su proporción, y rellena lo que
    sobre replicando el borde. Nada se estira y nada se recorta: el cliente ve
    su espacio completo, no una versión achatada.
    """
    img = img.convert("RGBA")
    escala = min(ancho / img.width, alto / img.height)
    nuevo = img.resize((max(1, int(img.width * escala)),
                        max(1, int(img.height * escala))), Image.LANCZOS)
    # El relleno toma el color del borde en vez de negro: un marco negro haría
    # que la IA lo interprete como parte de la escena y genere sombras falsas.
    borde = nuevo.resize((1, 1), Image.LANCZOS).getpixel((0, 0))
    lienzo = Image.new("RGBA", (ancho, alto), borde)
    lienzo.paste(nuevo, ((ancho - nuevo.width) // 2, (alto - nuevo.height) // 2), nuevo)
    return lienzo


def recortar_al_original(resultado_bytes: bytes, original_bytes: bytes) -> bytes:
    """
    Quita el borde que se le agregó a la foto antes de mandarla a la IA.

    ajustar_a_lienzo() encaja la foto en el formato que acepta el modelo y
    rellena lo que sobra. El resultado vuelve CON ese relleno, así que en el
    comparador antes/después la foto nueva salía más pequeña y corrida
    respecto a la original: no calzaban. Se recorta exactamente la zona donde
    se pegó la foto (misma cuenta que ajustar_a_lienzo) y se devuelve al
    misma proporción de la original, para que las dos se superpongan.

    Si algo falla se entrega el resultado sin recortar: un borde de más es
    preferible a no entregarle nada al cliente.
    """
    try:
        orig = Image.open(io.BytesIO(original_bytes))
        w, h = orig.size
        ancho, alto, _ = tamano_para(original_bytes)
        escala = min(ancho / w, alto / h)
        nw, nh = max(1, int(w * escala)), max(1, int(h * escala))
        x0, y0 = (ancho - nw) // 2, (alto - nh) // 2
        res = Image.open(io.BytesIO(resultado_bytes)).convert("RGB")
        if res.size != (ancho, alto):
            res = res.resize((ancho, alto), Image.LANCZOS)
        # Se deja en su tamaño natural, SIN agrandar a la resolución original:
        # para que calce basta con la misma proporción, y una foto de iPhone
        # (4032x3024) agrandada pasaría de 5 MB, que es el tope de WhatsApp.
        recorte = res.crop((x0, y0, x0 + nw, y0 + nh))
        buf = io.BytesIO()
        recorte.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"No se pudo recortar el borde del resultado: {e}")
        return resultado_bytes


def ajustar_mascara_a_lienzo(mascara: Image.Image, ancho: int, alto: int) -> Image.Image:
    """
    Encaja la máscara con EXACTAMENTE la misma geometría que ajustar_a_lienzo
    usa para la foto, para que cada píxel de la máscara caiga sobre su píxel.
    El borde de relleno queda opaco: la IA no debe tocar lo que no es foto.
    Se pega SIN máscara de pegado, para copiar el alfa tal cual (el alfa 0 es
    lo que le dice a OpenAI dónde puede editar).
    """
    mascara = mascara.convert("RGBA")
    escala = min(ancho / mascara.width, alto / mascara.height)
    nueva = mascara.resize((max(1, int(mascara.width * escala)),
                            max(1, int(mascara.height * escala))), Image.NEAREST)
    lienzo = Image.new("RGBA", (ancho, alto), (0, 0, 0, 255))
    lienzo.paste(nueva, ((ancho - nueva.width) // 2, (alto - nueva.height) // 2))
    return lienzo


async def editar_objeto(escena_bytes: bytes, mascara_bytes: bytes, accion: str,
                        producto_bytes: bytes = None, producto_nombre: str = None) -> bytes:
    """
    Quita un objeto de la foto, o lo cambia por un producto del catálogo.

    Esto NO se puede hacer por cálculo: al quitar un sofá hay que inventar el
    piso y la pared que había detrás, y eso solo lo hace la IA. La máscara
    marca la zona del objeto; todo lo demás debe quedar idéntico.

    Devuelve los bytes PNG ya recortados a la proporción de la escena, para
    que el antes y el después calcen.
    """
    settings = get_settings()
    ancho, alto, tamano_salida = tamano_para(escena_bytes)
    escena  = ajustar_a_lienzo(Image.open(io.BytesIO(escena_bytes)), ancho, alto)
    mascara = ajustar_mascara_a_lienzo(Image.open(io.BytesIO(mascara_bytes)), ancho, alto)

    def a_png(img):
        b = io.BytesIO(); img.save(b, format="PNG"); return b.getvalue()

    archivos = [("image[]", ("escena.png", a_png(escena), "image/png"))]
    if accion == "cambiar":
        if not producto_bytes:
            raise RuntimeError("Falta la foto del producto para hacer el cambio")
        archivos.append(("image[]", ("producto_referencia.png", imagen_a_png_1024(producto_bytes), "image/png")))
        prompt = (
            f"Replace the object inside the masked area with the product shown in the second "
            f"reference image{': ' + producto_nombre if producto_nombre else ''}. "
            f"Place it in the same position, facing a natural direction, matching the room's "
            f"perspective, scale and lighting, with a realistic contact shadow on the floor. "
            f"Reproduce the product's real shape, color, materials and details faithfully from the "
            f"reference image — do not invent a different design. If the product is smaller than the "
            f"removed object, fill the rest with the floor and wall that would naturally be behind it. "
            f"Keep everything outside the masked area exactly as it is. Photorealistic."
        )
    else:
        prompt = (
            "Completely remove the object inside the masked area. Fill that area with what would "
            "naturally be behind it: continue the floor, wall and baseboard with exactly the same "
            "material, tile pattern, grout lines, perspective and lighting as the surrounding area. "
            "Remove its shadow too. Do not add any new object or decoration. Keep everything outside "
            "the masked area exactly as it is. Photorealistic."
        )
    archivos.append(("mask", ("mascara.png", a_png(mascara), "image/png")))

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await _post_con_reintentos(
            client,
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            etiqueta=f"gpt-image-1 objeto/{accion}",
            files=archivos,
            data={
                "model":          "gpt-image-1",
                "prompt":         prompt,
                "n":              "1",
                "size":           tamano_salida,
                "quality":        "high",
                "moderation":     "low",
                "input_fidelity": "high",
            },
        )
    if response.status_code != 200:
        logger.error(f"gpt-image-1 objeto/{accion} falló: {response.status_code} {response.text[:400]}")
        raise RuntimeError(f"El servicio de IA respondió {response.status_code}")
    b64 = (response.json().get("data") or [{}])[0].get("b64_json")
    if not b64:
        raise RuntimeError("La IA no devolvió imagen")
    return recortar_al_original(base64.b64decode(b64), escena_bytes)


def crear_mascara_piso_paredes(imagen_bytes: bytes) -> bytes:
    """
    Máscara PNG con canal alpha:
    - Transparente (alpha=0)  → EDITABLE (piso + paredes sin decoración)
    - Opaco (alpha=255)       → PROTEGIDO (muebles, cuadros, ventanas, objetos)
    """
    ancho, alto, _ = tamano_para(imagen_bytes)
    img = ajustar_a_lienzo(Image.open(io.BytesIO(imagen_bytes)), ancho, alto)

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
    """
    Prepara la foto para gpt-image-1 respetando su proporción.
    (El nombre queda por compatibilidad; ya no siempre es 1024x1024.)
    """
    ancho, alto, _ = tamano_para(imagen_bytes)
    img = ajustar_a_lienzo(Image.open(io.BytesIO(imagen_bytes)), ancho, alto)
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

    _, _, tamano_salida = tamano_para(imagen_bytes)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await _post_con_reintentos(
            client,
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            etiqueta="gpt-image-1",
            files={
                "image": ("room.png", imagen_png,  "image/png"),
                "mask":  ("mask.png", mascara_png, "image/png"),
            },
            data={
                "model":   "gpt-image-1",
                # Sin esto queda en "auto" (lo más estricto) y bloquea fotos de
                # obra normales con un "moderation_blocked / other" que no
                # explica nada. "low" sigue filtrando lo que de verdad importa.
                "moderation": "low",
                "prompt":  prompt,
                "n":       "1",
                "size":    tamano_salida,
                "quality": "medium",
            }
        )

        logger.info(f"GPT-image-1 status: {response.status_code}")
        logger.info(f"GPT-image-1 response: {response.text[:300]}")

        if response.status_code == 200:
            data = response.json()
            imagen_b64 = data["data"][0].get("b64_json")
            if imagen_b64:
                resultado_bytes = recortar_al_original(base64.b64decode(imagen_b64), imagen_bytes)
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
    Aplica un producto específico a la foto del espacio.
    Usa la FOTO REAL del producto como referencia visual para gpt-image-1
    (antes solo se mandaba el nombre en texto — la IA nunca veía la foto real).

    Para categoría "muebles": en vez de aplicar un material a piso/pared,
    QUITA el mobiliario existente de la foto y coloca el mueble real de la
    tienda en su lugar, con imagen de referencia real.
    """
    settings = get_settings()
    es_mueble = categoria == "muebles"

    if es_mueble:
        prompt = (
            f"Interior design photo edit. This is a photo of a room. "
            f"STEP 1: Remove ALL existing furniture and decor objects currently in the room "
            f"(sofas, chairs, tables, beds, shelves, lamps, rugs, curtains, decorative objects) — "
            f"leave the room completely empty of furniture. "
            f"STEP 2: Add this exact furniture piece, matching its design, color, material and "
            f"proportions EXACTLY as shown in the second reference image: \"{producto_nombre}\". "
            f"Place it in a natural, realistic position appropriate for the room's scale and use. "
            f"Keep the room's architecture EXACTLY unchanged: same walls, same wall color, same "
            f"floor material, same windows, same doors, same ceiling, same camera angle and lighting. "
            f"Photorealistic result, professional real estate photography, no text, no watermarks."
        )
    else:
        instrucciones = {
            "pisos":      f"Replace the floor with the exact flooring material shown in the second reference image: {producto_nombre}. Keep all furniture and walls unchanged.",
            "enchapes":   f"Apply the tile/enchape material from the second reference image ({producto_nombre}) to the walls and floor. Keep furniture unchanged.",
            "pintura":    f"Paint the walls with the exact color and finish shown in the second reference image: {producto_nombre}. Keep all furniture and floor unchanged.",
            "materiales": f"Apply the material from the second reference image ({producto_nombre}) to the floor. Keep everything else unchanged.",
        }
        instruccion = instrucciones.get(
            categoria,
            f"Apply the product from the second reference image ({producto_nombre}) to the space. Keep furniture unchanged."
        )
        prompt = (
            f"Interior design renovation. {instruccion} "
            f"Photorealistic result. Professional architectural photography. "
            f"Same room layout, same furniture positions, same lighting angle. "
            f"No text, no watermarks."
        )

    imagen_png   = imagen_a_png_1024(foto_bytes)
    producto_png = imagen_a_png_1024(producto_bytes)

    # La máscara de piso/pared solo tiene sentido para materiales — para
    # muebles necesitamos poder tocar toda la habitación, así que no se usa.
    archivos = [
        ("image[]", ("room.png", imagen_png, "image/png")),
        ("image[]", ("producto_referencia.png", producto_png, "image/png")),
    ]
    if not es_mueble:
        mascara_png = crear_mascara_piso_paredes(foto_bytes)
        archivos.append(("mask", ("mask.png", mascara_png, "image/png")))

    _, _, tamano_salida = tamano_para(foto_bytes)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await _post_con_reintentos(
            client,
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            etiqueta="gpt-image-1 producto",
            files=archivos,
            data={
                "model":   "gpt-image-1",
                # Sin esto queda en "auto" (lo más estricto) y bloquea fotos de
                # obra normales con un "moderation_blocked / other" que no
                # explica nada. "low" sigue filtrando lo que de verdad importa.
                "moderation": "low",
                "prompt":  prompt,
                "n":       "1",
                "size":    tamano_salida,
                "quality": "high",
            }
        )

        logger.info(f"generar_imagen_con_producto ({categoria}) status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            imagen_b64 = data["data"][0].get("b64_json")
            if imagen_b64:
                resultado_bytes = recortar_al_original(base64.b64decode(imagen_b64), foto_bytes)
                return await subir_imagen_a_imgbb(resultado_bytes, settings.imgbb_api_key)
            else:
                return data["data"][0].get("url")
        else:
            logger.error(f"Error generar_imagen_con_producto: {response.text[:300]}")
            raise RuntimeError(f"Error aplicando producto: {response.status_code}")


# ── REEMPLAZAR la función generar_vista_isometrica en imagen_service.py ──
# Solo el bloque del prompt cambia — todo lo demás igual

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
    num_banos    = info_plano.get("num_banos", "1")
    tiene_cocina = info_plano.get("tiene_cocina", True)
    tiene_sala   = info_plano.get("tiene_sala", True)

    # Construir descripción detallada de espacios para el prompt
    espacios = []
    if tiene_sala:    espacios.append("living room / sala de estar")
    if tiene_cocina:  espacios.append("kitchen / cocina")
    if num_banos:     espacios.append(f"{num_banos} bathroom(s) / baño(s)")
    if habitaciones:  espacios.append(f"rooms: {habitaciones}")
    espacios_str = ", ".join(espacios)

    prompt = (
        "TASK: Convert this exact 2D architectural floor plan into a photorealistic isometric 3D render. "
        "The floor plan image is the ONLY reference. You MUST reproduce its exact layout.\n\n"

        "═══ CRITICAL RULES — VIOLATIONS ARE NOT ACCEPTABLE ═══\n"
        "RULE 1 — EXACT GEOMETRY: Every wall, partition, room boundary must match the floor plan EXACTLY. "
        "Do NOT move, add, or remove any wall.\n"
        "RULE 2 — EXACT ROOM COUNT: The 3D render must have EXACTLY these spaces: "
        f"{espacios_str}. No more, no less.\n"
        "RULE 3 — EXACT PROPORTIONS: If a room is wider than tall in the plan, it must look wider in 3D. "
        "Preserve all size relationships between spaces.\n"
        "RULE 4 — EXACT POSITIONS: Doors and windows must appear in the SAME positions as in the floor plan. "
        "Verify each opening location before placing it.\n"
        "RULE 5 — NO INVENTION: Do NOT add rooms, corridors, balconies, or spaces not shown in the plan.\n\n"

        "═══ SPATIAL LAYOUT FROM PLAN ANALYSIS ═══\n"
        f"Property type: {tipo}\n"
        f"Detected spaces: {espacios_str}\n"
        f"Estimated area: {area}\n"
        f"Layout description: {distribucion}\n"
        f"Room details: {habitaciones}\n\n"

        "═══ VERIFICATION CHECKLIST (apply before rendering) ═══\n"
        f"☐ Exactly {num_banos} bathroom(s) visible\n"
        f"☐ Kitchen {'present' if tiene_cocina else 'NOT present'}\n"
        f"☐ Living room {'present' if tiene_sala else 'NOT present'}\n"
        "☐ All walls match the floor plan\n"
        "☐ Door positions match the floor plan\n\n"

        "═══ VISUAL STYLE ═══\n"
        "- View: isometric 45-degree bird's eye architectural render\n"
        "- Walls: warm white (#F5F0E8), thin clean lines\n"
        "- Floors: light natural oak hardwood throughout\n"
        "- Furniture: modern minimalist, beige and grey tones, correctly scaled\n"
        "- Kitchen: light wood cabinets, white countertops, appliances visible\n"
        "- Bathrooms: white fixtures (toilet, sink, bathtub/shower)\n"
        "- Living room: grey sofa set, coffee table, TV area\n"
        "- Lighting: soft warm uniform light, subtle shadows for depth\n"
        "- Quality: professional architectural visualization, photorealistic\n"
        "- No text, no labels, no watermarks, no scale bars\n\n"

        "FINAL PRIORITY: Geometric accuracy to the floor plan > Visual aesthetics. "
        "A wrong room layout is NEVER acceptable even if it looks beautiful."
    )

    imagen_png = imagen_a_png_1024(imagen_bytes)

    _, _, tamano_salida = tamano_para(imagen_bytes)
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await _post_con_reintentos(
            client,
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            etiqueta="gpt-image-1",
            files={
                "image": ("plano.png", imagen_png, "image/png"),
            },
            data={
                "model":   "gpt-image-1",
                # Sin esto queda en "auto" (lo más estricto) y bloquea fotos de
                # obra normales con un "moderation_blocked / other" que no
                # explica nada. "low" sigue filtrando lo que de verdad importa.
                "moderation": "low",
                "prompt":  prompt,
                "n":       "1",
                "size":    tamano_salida,
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