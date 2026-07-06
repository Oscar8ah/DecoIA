import logging
import base64
import io
import time
import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from openai import OpenAI
from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase
from app.services.limites_service import tiene_fotos_disponibles, descontar_foto

logger = logging.getLogger(__name__)
router = APIRouter(tags=["render3d"])


class RenderRequest(BaseModel):
    imagen_base64:        str
    prompt:               str
    empresa_id:            Optional[str] = None
    producto_imagen_url:   Optional[str] = None   # foto real del producto elegido (piso, mueble, etc.)
    categoria_producto:    Optional[str] = None    # "muebles", "pisos", "enchapes", "pintura", ...
    producto_nombre:       Optional[str] = None


@router.post("/generar-render-3d")
async def generar_render_3d(data: RenderRequest):
    """
    Recibe captura del visor 3D + prompt.
    Genera render fotorrealista con gpt-image-1.
    Guarda en Supabase Storage y retorna URL pública.
    """
    settings = get_settings()

    # ── Revisar cupo de fotos del plan ANTES de gastar en la IA ───────────
    if data.empresa_id:
        if not await tiene_fotos_disponibles(data.empresa_id):
            logger.warning(f"Empresa {data.empresa_id} sin fotos disponibles — render bloqueado")
            return {"status": "error", "error": "sin_fotos_disponibles",
                    "mensaje": "Ya usaste todas las fotos incluidas en tu plan este mes. Actualiza tu plan para seguir generando renders."}

    try:
        client = OpenAI(api_key=settings.openai_api_key)

        # Decodificar imagen base64
        imagen_bytes = base64.b64decode(data.imagen_base64)

        logger.info(f"Generando render 3D para empresa: {data.empresa_id}")

        # ── FIX: images.edit requiere un objeto tipo file con nombre y tipo MIME ──
        # Envolver bytes en BytesIO con nombre para que la librería lo procese bien
        imagen_file = io.BytesIO(imagen_bytes)
        imagen_file.name = "render_3d.png"          # atributo name necesario

        prompt_final   = data.prompt
        imagenes_envio = imagen_file

        # ── Si viene una foto real de producto, mandarla también a la IA ──
        # (antes solo se describía el producto en texto, la IA nunca lo veía)
        if data.producto_imagen_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as http_client:
                    resp_prod = await http_client.get(data.producto_imagen_url)
                    resp_prod.raise_for_status()
                producto_file = io.BytesIO(resp_prod.content)
                producto_file.name = "producto_referencia.png"

                if data.categoria_producto == "muebles":
                    nombre_prod = data.producto_nombre or "el mueble de referencia"
                    prompt_final = (
                        f"Interior design photo edit. This is a photo of a room. "
                        f"STEP 1: Remove ALL existing furniture and decor objects currently in the room "
                        f"(sofas, chairs, tables, beds, shelves, lamps, rugs, curtains, decorative objects) — "
                        f"leave the room completely empty of furniture. "
                        f"STEP 2: Add this exact furniture piece, matching its design, color, material and "
                        f"proportions EXACTLY as shown in the second reference image: \"{nombre_prod}\". "
                        f"Place it in a natural, realistic position appropriate for the room's scale and use. "
                        f"Keep the room's architecture EXACTLY unchanged: same walls, same wall color, same "
                        f"floor material, same windows, same doors, same ceiling, same camera angle and lighting. "
                        f"Photorealistic result, professional real estate photography, no text, no watermarks."
                    )
                else:
                    # Materiales (piso/enchape/pintura): usar el prompt que ya arma el frontend,
                    # pero con la foto real del producto como segunda referencia.
                    prompt_final = data.prompt + " Match the exact material/color shown in the second reference image."

                imagenes_envio = [imagen_file, producto_file]
            except Exception as e:
                logger.warning(f"No se pudo descargar la imagen del producto ({data.producto_imagen_url}): {e} — se sigue solo con texto")

        response = client.images.edit(
            model  = "gpt-image-1",
            image  = imagenes_envio,                # BytesIO único, o lista [cuarto, producto]
            prompt = prompt_final,
            size   = "1024x1024",
        )

        # Obtener imagen generada (b64_json)
        imagen_generada_b64   = response.data[0].b64_json
        imagen_generada_bytes = base64.b64decode(imagen_generada_b64)

        # ── Subir a Supabase Storage ──
        supabase   = get_supabase()
        timestamp  = time.time_ns()
        empresa_id = data.empresa_id or "sin_empresa"
        ruta       = f"{empresa_id}/render_{timestamp}.png"

        supabase.storage.from_("portafolio").upload(
            ruta,
            imagen_generada_bytes,
            {"content-type": "image/png", "upsert": "true"}
        )

        # URL pública
        url_publica = supabase.storage.from_("portafolio").get_public_url(ruta)

        # Guardar en tabla imagenes
        if data.empresa_id:
            supabase.table("imagenes").insert({
                "empresa_id":   data.empresa_id,
                "url_generada": url_publica,
                "tipo_espacio": "visor_3d",
                "estilo":       "render_ia",
            }).execute()
            await descontar_foto(data.empresa_id)

        logger.info(f"Render generado: {url_publica}")
        return {"url_imagen": url_publica, "status": "ok"}

    except Exception as e:
        logger.error(f"Error generando render 3D: {type(e).__name__} — {e}")
        return {"error": str(e), "status": "error"}