import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request

from app.services.openai_service import analizar_plano_completo
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["planos"])

# Igual que en catalogo.py — llama a IA de pago, hay que protegerlo de abuso
_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 6
VENTANA_SEGUNDOS = 3600


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    historial = _peticiones_por_ip[ip]
    while historial and ahora - historial[0] > VENTANA_SEGUNDOS:
        historial.popleft()
    if len(historial) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta de nuevo más tarde.")
    historial.append(ahora)


@router.post("/analizar-plano-arquitectonico")
async def analizar_plano_arquitectonico(
    request: Request,
    archivo: UploadFile = File(...),
    empresa_id: str = Form(...),
):
    """
    Recibe una foto/escaneo de un plano arquitectónico, lo interpreta con IA
    (misma función que usa el bot de WhatsApp) y genera el modelo 3D listo
    para cargar en el Visor 3D. Exclusivo del plan Corporativo.
    """
    _verificar_limite_ip(request)
    supabase = get_supabase()

    # ── Verificar plan Corporativo server-side (no confiar solo en el frontend) ──
    empresa_res = supabase.table("empresas").select("id, planes(nombre)").eq("id", empresa_id).maybeSingle().execute()
    if not empresa_res.data:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")
    plan_nombre = (empresa_res.data.get("planes") or {}).get("nombre", "basico")
    if plan_nombre != "corporativo":
        raise HTTPException(status_code=403, detail="Esta función es exclusiva del plan Corporativo.")

    contenido = await archivo.read()
    if len(contenido) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="La imagen supera el máximo de 10MB.")

    try:
        resultado_completo = analizar_plano_completo(contenido)
    except Exception as e:
        logger.error(f"Error analizando plano arquitectónico: {e}")
        raise HTTPException(status_code=502, detail="Error interpretando el plano con IA.")

    info      = resultado_completo.get("info", {})
    modelo_3d = resultado_completo.get("modelo_3d")

    if not info.get("es_plano", True):
        raise HTTPException(status_code=400, detail="La imagen no parece un plano arquitectónico. Sube una foto más clara del plano.")

    if not modelo_3d:
        raise HTTPException(status_code=502, detail="No se pudo generar el modelo 3D a partir de este plano.")

    # Guardar el modelo generado — mismo formato/tabla que usa el bot de WhatsApp
    try:
        r = supabase.table("modelos_3d_plano").insert({
            "empresa_id":  empresa_id,
            "modelo_json": json.dumps(modelo_3d),
            "plano_info":  json.dumps(info),
            "created_at":  datetime.now().isoformat(),
        }).execute()
        modelo_id = r.data[0]["id"] if r.data else None
    except Exception as e:
        logger.error(f"Error guardando modelo 3D del plano: {e}")
        raise HTTPException(status_code=500, detail="El plano se interpretó pero no se pudo guardar.")

    return {
        "status":     "ok",
        "info":       info,
        "modelo_id":  modelo_id,
        "url_visor3d": f"/visor3d?plano={modelo_id}" if modelo_id else None,
    }