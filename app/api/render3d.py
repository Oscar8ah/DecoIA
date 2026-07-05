import logging
import base64
import io
import time
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
    imagen_base64: str
    prompt:        str
    empresa_id:    Optional[str] = None


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

        response = client.images.edit(
            model  = "gpt-image-1",
            image  = imagen_file,                   # BytesIO con .name, no tupla
            prompt = data.prompt,
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