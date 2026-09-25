import logging
import base64
import io
import time
from collections import defaultdict, deque
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.utils.supabase_client import get_supabase
from app.utils.auth import exigir_duenio

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fondo"])

# ── Límite de uso por IP ──────────────────────────────────────────────────
# rembg no cuesta dinero de API, pero sí consume bastante CPU del servidor —
# sin límite, alguien podría saturar Render llamándolo en bucle.
_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 30
VENTANA_SEGUNDOS  = 3600


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    historial = _peticiones_por_ip[ip]
    while historial and ahora - historial[0] > VENTANA_SEGUNDOS:
        historial.popleft()
    if len(historial) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiadas peticiones desde esta conexión. Intenta de nuevo más tarde.")
    historial.append(ahora)


class FondoRequest(BaseModel):
    imagen_base64: str
    empresa_id:    str = ""


@router.post("/quitar-fondo")
async def quitar_fondo(data: FondoRequest, request: Request):
    """
    Recibe imagen en base64, quita el fondo con rembg,
    sube el PNG transparente a Supabase y retorna la URL.
    """
    # Antes cualquiera podía subir archivos a la carpeta de CUALQUIER empresa
    # en el bucket público (la ruta salía del empresa_id que mandaba el navegador).
    await exigir_duenio(request, data.empresa_id)
    _verificar_limite_ip(request)
    try:
        imagen_bytes = base64.b64decode(data.imagen_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="La imagen no es válida.")
    if len(imagen_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="La imagen supera los 10 MB.")
    try:
        from rembg import remove

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