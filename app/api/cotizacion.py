import logging
import time
from collections import defaultdict, deque
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services.cotizacion_service import generar_pdf_cotizacion, guardar_cotizacion_pdf

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cotizacion"])

# ── Límite básico de uso por IP ──────────────────────────────────────────
# No llama IA ni servicios de pago por sí solo, pero sí genera archivos y
# escribe en Storage — igual conviene un límite razonable contra abuso.
_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 30
VENTANA_SEGUNDOS = 3600  # 30 cotizaciones por hora por IP

MAX_ITEMS = 50


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    historial = _peticiones_por_ip[ip]
    while historial and ahora - historial[0] > VENTANA_SEGUNDOS:
        historial.popleft()
    if len(historial) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiadas cotizaciones generadas desde esta conexión. Intenta de nuevo más tarde.")
    historial.append(ahora)


class ItemCotizacion(BaseModel):
    nombre: str
    cantidad: float
    unidad: str = ""
    precio_unitario: float


class CotizacionRequest(BaseModel):
    tienda_id: str
    tienda_nombre: str
    items: list[ItemCotizacion] = Field(min_length=1, max_length=MAX_ITEMS)
    descuento_pct: float = 0
    cliente_nombre: str = ""
    cliente_telefono: str = ""
    validez_dias: int = 15
    notas: str = ""


@router.post("/generar-cotizacion")
async def generar_cotizacion(data: CotizacionRequest, request: Request):
    """
    Genera el PDF de una cotización (estimado de precio, sin validez fiscal DIAN)
    y lo sube a Storage. NO escribe en la tabla `cotizaciones` — ese INSERT lo
    hace el frontend con la sesión propia del usuario, para que quede protegido
    por las políticas RLS (solo el dueño de la tienda puede registrar sus
    propias cotizaciones). Este endpoint solo genera el archivo y devuelve la
    URL + el número de cotización para que el frontend guarde el registro.
    """
    _verificar_limite_ip(request)

    if data.descuento_pct < 0 or data.descuento_pct > 100:
        raise HTTPException(status_code=400, detail="El descuento debe estar entre 0 y 100%.")

    items_dict = []
    subtotal = 0.0
    for it in data.items:
        if it.cantidad <= 0:
            raise HTTPException(status_code=400, detail=f"La cantidad de '{it.nombre}' debe ser mayor a 0.")
        if it.precio_unitario < 0:
            raise HTTPException(status_code=400, detail=f"El precio de '{it.nombre}' no puede ser negativo.")
        sub = round(it.cantidad * it.precio_unitario, 2)
        subtotal += sub
        items_dict.append({
            "nombre": it.nombre.strip()[:200],
            "cantidad": it.cantidad,
            "unidad": it.unidad.strip()[:20],
            "precio_unitario": it.precio_unitario,
            "subtotal": sub,
        })

    descuento_valor = round(subtotal * (data.descuento_pct / 100), 2)
    total = round(subtotal - descuento_valor, 2)

    # Número de cotización simple: fecha + timestamp corto (no depende de
    # una secuencia en BD, así este endpoint no necesita tocar Supabase con
    # permisos de escritura en tablas — solo Storage, que sí usa service_role).
    numero = f"COT-{datetime.now().strftime('%Y%m%d')}-{int(time.time()) % 100000}"

    try:
        pdf_bytes = generar_pdf_cotizacion(
            numero=numero,
            tienda_nombre=data.tienda_nombre,
            items=items_dict,
            subtotal=subtotal,
            descuento_pct=data.descuento_pct,
            descuento_valor=descuento_valor,
            total=total,
            cliente_nombre=data.cliente_nombre,
            cliente_telefono=data.cliente_telefono,
            validez_dias=data.validez_dias,
            notas=data.notas,
        )
    except Exception as e:
        logger.error(f"Error generando PDF de cotización: {e}")
        raise HTTPException(status_code=500, detail="No se pudo generar el PDF de la cotización.")

    url_pdf = await guardar_cotizacion_pdf(pdf_bytes, numero)
    if not url_pdf:
        raise HTTPException(status_code=502, detail="El PDF se generó pero no se pudo guardar. Intenta de nuevo.")

    return {
        "numero": numero,
        "url_pdf": url_pdf,
        "subtotal": subtotal,
        "descuento_valor": descuento_valor,
        "total": total,
        "items": items_dict,
    }