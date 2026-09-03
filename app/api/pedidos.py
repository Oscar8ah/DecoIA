import logging
import time
import secrets
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["pedidos"])

# ── Límite por IP ─────────────────────────────────────────────────────────
_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 30
VENTANA_SEGUNDOS  = 3600
MAX_ITEMS         = 50


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    hist = _peticiones_por_ip[ip]
    while hist and ahora - hist[0] > VENTANA_SEGUNDOS:
        hist.popleft()
    if len(hist) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera un momento.")
    hist.append(ahora)


class ItemPedido(BaseModel):
    producto_id: str
    cantidad: int = Field(ge=1, le=999)


class CrearPedidoRequest(BaseModel):
    tienda_id: str
    items: list[ItemPedido] = Field(default_factory=list, max_length=MAX_ITEMS)
    comprador_nombre:    str = ""
    comprador_email:     str = ""
    comprador_telefono:  str = ""
    comprador_direccion: str = ""


@router.post("/crear-pedido")
async def crear_pedido(data: CrearPedidoRequest, request: Request):
    """
    Crea un pedido ANTES de mandar al cliente a pagar.

    Punto clave de seguridad: los precios se leen de la BASE DE DATOS, nunca
    del navegador. Si se confiara en el precio que manda el frontend,
    cualquiera podría editarlo y comprar un piso de $200.000 por $1.
    """
    _verificar_limite_ip(request)

    if not data.items:
        raise HTTPException(status_code=400, detail="El pedido no tiene productos.")

    supabase = get_supabase()

    # 1. Verificar la tienda y traer su porcentaje de comisión
    r = supabase.table("tiendas") \
        .select("id, nombre, empresa_id, activa, comision_porcentaje") \
        .eq("id", data.tienda_id).maybe_single().execute()

    if not r.data:
        raise HTTPException(status_code=404, detail="Tienda no encontrada.")
    tienda = r.data
    if not tienda.get("activa"):
        raise HTTPException(status_code=400, detail="Esta tienda no está activa en este momento.")

    # 2. Traer los precios REALES de la base de datos
    ids = [i.producto_id for i in data.items]
    rp = supabase.table("productos") \
        .select("id, nombre, precio, unidad, tienda_id, activo") \
        .in_("id", ids).execute()
    productos = {p["id"]: p for p in (rp.data or [])}

    items_final = []
    subtotal = 0.0
    for item in data.items:
        p = productos.get(item.producto_id)
        if not p:
            raise HTTPException(status_code=400, detail=f"Un producto del carrito ya no está disponible.")
        if not p.get("activo"):
            raise HTTPException(status_code=400, detail=f"'{p.get('nombre')}' ya no está disponible.")
        if p.get("tienda_id") != data.tienda_id:
            # No se pueden mezclar tiendas en un mismo pedido: cada tienda
            # recibe su propio pago, así que cada una necesita su pedido.
            raise HTTPException(status_code=400, detail="Todos los productos deben ser de la misma tienda.")

        precio = float(p.get("precio") or 0)
        importe = precio * item.cantidad
        subtotal += importe
        items_final.append({
            "producto_id": p["id"],
            "nombre":      p.get("nombre"),
            "precio":      precio,
            "unidad":      p.get("unidad"),
            "cantidad":    item.cantidad,
            "importe":     importe,
        })

    if subtotal <= 0:
        raise HTTPException(status_code=400, detail="El total del pedido no es válido.")

    # 3. Calcular el split
    pct = float(tienda.get("comision_porcentaje") or 5.0)
    comision = round(subtotal * pct / 100.0, 2)
    para_tienda = round(subtotal - comision, 2)

    # Referencia única e impredecible (no secuencial, para que nadie pueda
    # adivinar referencias de otros pedidos)
    referencia = f"DECO-{secrets.token_urlsafe(12)}"

    try:
        ins = supabase.table("pedidos").insert({
            "referencia":          referencia,
            "tienda_id":           data.tienda_id,
            "empresa_id":          tienda.get("empresa_id"),
            "comprador_nombre":    data.comprador_nombre.strip()[:150] or None,
            "comprador_email":     data.comprador_email.strip()[:150] or None,
            "comprador_telefono":  data.comprador_telefono.strip()[:40] or None,
            "comprador_direccion": data.comprador_direccion.strip()[:300] or None,
            "items":               items_final,
            "subtotal":            subtotal,
            "total":               subtotal,
            "comision_porcentaje": pct,
            "comision_monto":      comision,
            "monto_tienda":        para_tienda,
            "estado":              "pendiente",
        }).select().single().execute()
    except Exception as e:
        logger.error(f"Error creando pedido: {e}")
        raise HTTPException(status_code=502, detail="No se pudo registrar el pedido. Intenta de nuevo.")

    logger.info(f"Pedido {referencia} creado — tienda {tienda.get('nombre')}, total {subtotal}, comisión {comision}")

    return {
        "status":      "ok",
        "referencia":  referencia,
        "pedido_id":   ins.data["id"] if ins.data else None,
        "total":       subtotal,
        "total_centavos": int(round(subtotal * 100)),   # Wompi cobra en centavos
        "tienda":      tienda.get("nombre"),
        # El desglose NO se manda al navegador para no exponer el margen del
        # negocio al comprador. Queda solo en la base de datos.
    }


@router.get("/pedido/{referencia}")
async def consultar_pedido(referencia: str):
    """Consulta pública del estado de un pedido, por su referencia."""
    supabase = get_supabase()
    r = supabase.table("pedidos") \
        .select("referencia, estado, total, items, created_at, pagado_at, tiendas(nombre)") \
        .eq("referencia", referencia).maybe_single().execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Pedido no encontrado.")
    d = r.data
    return {
        "referencia": d["referencia"],
        "estado":     d["estado"],
        "total":      d["total"],
        "items":      d["items"],
        "tienda":     (d.get("tiendas") or {}).get("nombre"),
        "creado":     d.get("created_at"),
        "pagado":     d.get("pagado_at"),
    }