import logging
import time
import asyncio
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["video-ia"])

# ── Runway API ────────────────────────────────────────────────────────────
# OJO: `gen4_aleph` fue RETIRADO el 30 de julio de 2026 — las peticiones con
# ese identificador fallan. El modelo vigente es `aleph2` (Aleph 2.0), que
# acepta videos de 2 a 30 segundos y hasta 5 imágenes de referencia.
RUNWAY_BASE      = "https://api.dev.runwayml.com/v1"
RUNWAY_VERSION   = "2024-11-06"
MODELO_ALEPH     = "aleph2"          # editar un recorrido YA grabado (más barato)
MODELO_SEEDANCE  = "seedance2_fast"  # generar desde cero (más caro)

# Planes que pueden usar esto — el video con IA es exclusivo de Corporativo
PLANES_CON_VIDEO_IA = {"corporativo"}

_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 10          # es caro: límite bajo a propósito
VENTANA_SEGUNDOS  = 3600


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    hist = _peticiones_por_ip[ip]
    while hist and ahora - hist[0] > VENTANA_SEGUNDOS:
        hist.popleft()
    if len(hist) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiadas generaciones seguidas. Espera un momento.")
    hist.append(ahora)


class VideoIARequest(BaseModel):
    empresa_id:  str
    video_url:   str                       # el recorrido .webm/.mp4 ya grabado
    prompt:      str = Field(max_length=1000)
    ratio:       str = "1280:720"


async def _verificar_plan(empresa_id: str) -> dict:
    """El video con IA cuesta dinero real: se valida el plan ANTES de gastar."""
    supabase = get_supabase()
    r = supabase.table("empresas") \
        .select("id, nombre, estado, planes(nombre)") \
        .eq("id", empresa_id).maybe_single().execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")
    emp = r.data
    plan = ((emp.get("planes") or {}).get("nombre") or "").lower()
    if plan not in PLANES_CON_VIDEO_IA or emp.get("estado") != "activo":
        raise HTTPException(
            status_code=403,
            detail="El video con IA está disponible solo en el plan Corporativo activo."
        )
    return emp


@router.post("/generar-video-ia")
async def generar_video_ia(data: VideoIARequest, request: Request):
    """
    Toma un recorrido YA grabado en el Visor 3D y lo transforma con IA,
    manteniendo el movimiento y la arquitectura originales.

    Se usa Aleph 2.0 (video→video) y NO un modelo de texto→video: editar algo
    que ya existe es mucho más barato que generar desde cero, y además respeta
    la geometría real del plano que hizo el usuario.
    """
    _verificar_limite_ip(request)
    settings = get_settings()

    api_key = getattr(settings, "runway_api_key", "") or ""
    if not api_key:
        raise HTTPException(status_code=503,
            detail="El video con IA aún no está configurado. Contacta a soporte.")

    await _verificar_plan(data.empresa_id)

    cuerpo = {
        "model":       MODELO_ALEPH,
        "videoUri":    data.video_url,
        "promptText":  data.prompt[:1000],
        "ratio":       data.ratio,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{RUNWAY_BASE}/video_to_video",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Runway-Version": RUNWAY_VERSION,
                    "Content-Type": "application/json",
                },
                json=cuerpo,
            )
        if r.status_code >= 400:
            logger.error(f"Runway rechazó la petición ({r.status_code}): {r.text[:400]}")
            raise HTTPException(status_code=502,
                detail="La IA de video rechazó la petición. Revisa que el recorrido dure entre 2 y 30 segundos.")

        tarea = r.json()
        tarea_id = tarea.get("id")
        logger.info(f"Video IA iniciado — tarea {tarea_id}, empresa {data.empresa_id}")

        return {
            "status":   "procesando",
            "tarea_id": tarea_id,
            "mensaje":  "La IA está trabajando en tu video. Puede tardar varios minutos.",
        }

    except httpx.RequestError as e:
        logger.error(f"No se pudo contactar a Runway: {e}")
        raise HTTPException(status_code=502, detail="No se pudo contactar el servicio de video.")


@router.get("/video-ia/{tarea_id}")
async def estado_video_ia(tarea_id: str):
    """
    Consulta cómo va la generación. La IA de video tarda minutos, así que el
    frontend consulta esto cada pocos segundos en vez de esperar bloqueado.
    """
    settings = get_settings()
    api_key = getattr(settings, "runway_api_key", "") or ""
    if not api_key:
        raise HTTPException(status_code=503, detail="Servicio no configurado.")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{RUNWAY_BASE}/tasks/{tarea_id}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Runway-Version": RUNWAY_VERSION,
                },
            )
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail="No se pudo consultar el estado.")

        d = r.json()
        estado = (d.get("status") or "").upper()

        if estado == "SUCCEEDED":
            salidas = d.get("output") or []
            return {"status": "listo", "video_url": salidas[0] if salidas else None}
        if estado == "FAILED":
            logger.error(f"Video IA falló — tarea {tarea_id}: {d.get('failure')}")
            return {"status": "fallido", "mensaje": "La IA no pudo procesar este recorrido."}

        return {"status": "procesando", "progreso": d.get("progress", 0)}

    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="No se pudo consultar el estado.")