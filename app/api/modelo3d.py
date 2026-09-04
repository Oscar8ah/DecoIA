import logging
import time
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["modelo-3d"])

# ── Meshy API ─────────────────────────────────────────────────────────────
# Convierte la FOTO de un producto (un mueble, un sanitario) en un modelo 3D
# que después se puede ver en AR sobre el espacio real del cliente.
# Los pisos y enchapes NO necesitan esto: para ellos basta un plano con la
# textura, que ya se genera gratis en `ver-en-espacio.html`.
MESHY_BASE = "https://api.meshy.ai/openapi/v1"

# Solo estas categorías justifican un modelo 3D. Un piso no se "modela".
CATEGORIAS_CON_3D = {
    "muebles", "baños", "cocinas", "puertas",
    "electrodomesticos", "jardineria", "seguridad",
}

PLANES_CON_MESHY = {"premium", "corporativo"}

_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 15
VENTANA_SEGUNDOS  = 3600


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    hist = _peticiones_por_ip[ip]
    while hist and ahora - hist[0] > VENTANA_SEGUNDOS:
        hist.popleft()
    if len(hist) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiadas generaciones. Espera un momento.")
    hist.append(ahora)


class Modelo3DRequest(BaseModel):
    producto_id: str


@router.post("/generar-modelo-3d")
async def generar_modelo_3d(data: Modelo3DRequest, request: Request):
    """
    Genera el modelo 3D de un producto a partir de su foto, para poder verlo
    en AR. Cuesta créditos de Meshy, así que se valida bastante antes.
    """
    _verificar_limite_ip(request)
    settings = get_settings()

    api_key = getattr(settings, "meshy_api_key", "") or ""
    if not api_key:
        raise HTTPException(status_code=503,
            detail="La generación de modelos 3D aún no está configurada.")

    supabase = get_supabase()
    r = supabase.table("productos") \
        .select("id, nombre, categoria, imagen_url, modelo_3d_url, tiendas(empresa_id, empresas(estado, planes(nombre)))") \
        .eq("id", data.producto_id).maybe_single().execute()

    if not r.data:
        raise HTTPException(status_code=404, detail="Producto no encontrado.")
    prod = r.data

    # Si ya tiene modelo, no gastar créditos otra vez
    if prod.get("modelo_3d_url"):
        return {"status": "listo", "modelo_url": prod["modelo_3d_url"], "ya_existia": True}

    if not prod.get("imagen_url"):
        raise HTTPException(status_code=400,
            detail="Este producto no tiene foto. Sube una antes de generar el modelo 3D.")

    categoria = (prod.get("categoria") or "").lower()
    if categoria not in CATEGORIAS_CON_3D:
        raise HTTPException(status_code=400,
            detail="Los pisos, enchapes y pinturas no necesitan modelo 3D — ya se ven en AR como superficie.")

    # Validar el plan de la tienda dueña del producto
    tienda  = prod.get("tiendas") or {}
    empresa = tienda.get("empresas") or {}
    plan    = ((empresa.get("planes") or {}).get("nombre") or "").lower()
    if plan not in PLANES_CON_MESHY or empresa.get("estado") != "activo":
        raise HTTPException(status_code=403,
            detail="Los modelos 3D están disponibles desde el plan Premium.")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{MESHY_BASE}/image-to-3d",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "image_url":       prod["imagen_url"],
                    "ai_model":        "latest",
                    "should_texture":  True,
                    "should_remesh":   True,
                    # Menos polígonos = archivo más liviano = carga más rápida
                    # en el celular del cliente, que es donde se ve el AR.
                    "target_polycount": 20000,
                },
            )
        if resp.status_code >= 400:
            logger.error(f"Meshy rechazó la petición ({resp.status_code}): {resp.text[:300]}")
            raise HTTPException(status_code=502,
                detail="No se pudo generar el modelo 3D. Revisa que la foto muestre un solo objeto, centrado y con fondo limpio.")

        tarea_id = resp.json().get("result")
        logger.info(f"Modelo 3D iniciado — tarea {tarea_id}, producto {prod.get('nombre')}")

        return {
            "status":   "procesando",
            "tarea_id": tarea_id,
            "mensaje":  "Generando el modelo 3D. Suele tardar entre 2 y 5 minutos.",
        }

    except httpx.RequestError as e:
        logger.error(f"No se pudo contactar a Meshy: {e}")
        raise HTTPException(status_code=502, detail="No se pudo contactar el servicio de modelos 3D.")


@router.get("/modelo-3d/{tarea_id}")
async def estado_modelo_3d(tarea_id: str, producto_id: str = ""):
    """Consulta el avance. Al terminar, guarda la URL del modelo en el producto."""
    settings = get_settings()
    api_key = getattr(settings, "meshy_api_key", "") or ""
    if not api_key:
        raise HTTPException(status_code=503, detail="Servicio no configurado.")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{MESHY_BASE}/image-to-3d/{tarea_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail="No se pudo consultar el estado.")

        d = resp.json()
        estado = (d.get("status") or "").upper()

        if estado == "SUCCEEDED":
            urls = d.get("model_urls") or {}
            glb  = urls.get("glb")
            # Guardar en el producto para no volver a gastar créditos.
            # OJO: las URLs de Meshy EXPIRAN — lo ideal a futuro es descargar
            # el archivo y subirlo a Supabase Storage. Por ahora se guarda la
            # URL directa, suficiente para probar.
            if glb and producto_id:
                try:
                    get_supabase().table("productos").update(
                        {"modelo_3d_url": glb}
                    ).eq("id", producto_id).execute()
                except Exception as e:
                    logger.error(f"No se pudo guardar el modelo 3D en el producto: {e}")
            return {"status": "listo", "modelo_url": glb}

        if estado in ("FAILED", "CANCELED"):
            logger.error(f"Modelo 3D falló — tarea {tarea_id}: {d.get('task_error')}")
            return {"status": "fallido",
                    "mensaje": "No se pudo generar el modelo. Prueba con una foto de un solo objeto, centrado y con fondo simple."}

        return {"status": "procesando", "progreso": d.get("progress", 0)}

    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="No se pudo consultar el estado.")