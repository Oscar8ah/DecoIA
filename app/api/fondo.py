import logging
import base64
import io
import time
from fastapi import APIRouter
from pydantic import BaseModel
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fondo"])


class FondoRequest(BaseModel):
    imagen_base64: str
    empresa_id:    str = "sin_empresa"


@router.post("/quitar-fondo")
async def quitar_fondo(data: FondoRequest):
    """
    Recibe imagen en base64, quita el fondo con rembg,
    sube el PNG transparente a Supabase y retorna la URL.
    """
    try:
        from rembg import remove

        imagen_bytes    = base64.b64decode(data.imagen_base64)
        resultado_bytes = remove(imagen_bytes)   # PNG con fondo transparente

        # Subir a Supabase Storage
        supabase  = get_supabase()
        timestamp = time.time_ns()
        ruta      = f"{data.empresa_id}/sin_fondo_{timestamp}.png"

        supabase.storage.from_("portafolio").upload(
            ruta,
            resultado_bytes,
            {"content-type": "image/png", "upsert": "true"}
        )

        url_publica = supabase.storage.from_("portafolio").get_public_url(ruta)

        logger.info(f"Fondo removido: {url_publica}")
        return {"url_imagen": url_publica, "status": "ok"}

    except Exception as e:
        logger.error(f"Error quitando fondo: {type(e).__name__} — {e}")
        return {"error": str(e), "status": "error"}