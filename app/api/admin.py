"""
CENTRO DEL SUPER ADMIN
Visión central de lo que pasa en DecoIArte, para cualquier tienda, vendedor o
comprador (nada depende de cuentas de prueba):
  · /admin/actividad             → registro de eventos (lo llenan disparadores
                                   de la base: sql/centro_admin.sql)
  · /admin/transacciones/marketplace → A. compras de productos a las tiendas
  · /admin/transacciones/planes      → B. planes de DecoIArte que compran las tiendas
  · /admin/transacciones/imagenes    → imágenes IA vendidas
Solo el Super Admin (ADMIN_EMAIL). Los datos se leen con la llave del servidor.
"""
import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from app.utils.auth import email_de_sesion, ADMIN_EMAIL
from app.utils.supabase_client import get_supabase
from app.utils.dinero import pesos

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


TOPE_FILAS = 500


def _exigir_admin(request: Request) -> None:
    if (email_de_sesion(request) or "").lower() != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Solo para el administrador.")


def _rango(q, desde: str | None, hasta: str | None, campo: str = "created_at"):
    if desde: q = q.gte(campo, desde)
    if hasta: q = q.lte(campo, hasta + "T23:59:59")
    return q


@router.get("/actividad")
async def actividad(request: Request, nivel: str | None = None, tipo: str | None = None, limite: int = 150):
    _exigir_admin(request)
    q = get_supabase().table("eventos").select("*").order("created_at", desc=True).limit(max(1, min(limite, 500)))
    if nivel: q = q.eq("nivel", nivel)
    if tipo:  q = q.eq("tipo", tipo)
    try:
        filas = q.execute().data or []
    except Exception as e:
        logger.error(f"No se pudo leer eventos (¿se corrió sql/centro_admin.sql?): {e}")
        raise HTTPException(status_code=503, detail="Falta crear el registro de actividad: corre sql/centro_admin.sql en Supabase.")
    sin_leer = sum(1 for f in filas if not f.get("leido"))
    return {"eventos": filas, "sin_leer": sin_leer}


class Ids(BaseModel):
    ids: list[int] = []


@router.post("/actividad/leidos")
async def marcar_leidos(data: Ids, request: Request):
    _exigir_admin(request)
    q = get_supabase().table("eventos").update({"leido": True})
    q = q.in_("id", data.ids) if data.ids else q.eq("leido", False)
    q.execute()
    return {"ok": True}


@router.get("/transacciones/marketplace")
async def tx_marketplace(request: Request, tienda_id: str | None = None, estado: str | None = None,
                         desde: str | None = None, hasta: str | None = None, buscar: str | None = None):
    """A. Compras de productos: quién compró, qué, a qué tienda, cuánto, comisión,
    pasarela, neto para el vendedor, estado, fecha, método y transacción."""
    _exigir_admin(request)
    q = get_supabase().table("pedidos").select("*, tiendas(nombre, empresa_id)").order("created_at", desc=True).limit(TOPE_FILAS)
    if tienda_id: q = q.eq("tienda_id", tienda_id)
    if estado:    q = q.eq("estado", estado)
    filas = _rango(q, desde, hasta).execute().data or []
    b = (buscar or "").strip().lower()
    salida, tot = [], {"pedidos": 0, "cobrado": 0, "comision": 0, "pasarela": 0, "para_tiendas": 0}
    for p in filas:
        items = p.get("items") or []
        texto = " ".join([p.get("referencia") or "", p.get("comprador_nombre") or "", p.get("comprador_email") or "",
                          (p.get("tiendas") or {}).get("nombre") or "", p.get("transaccion_id") or ""] + [i.get("nombre", "") for i in items]).lower()
        if b and b not in texto:
            continue
        pagado = p.get("estado") in ("pagado", "enviado", "entregado")
        fila = {
            "referencia": p.get("referencia"), "fecha": p.get("created_at"), "pagado_at": p.get("pagado_at"),
            "estado": p.get("estado"), "tienda": (p.get("tiendas") or {}).get("nombre"), "tienda_id": p.get("tienda_id"),
            "comprador": p.get("comprador_nombre"), "comprador_email": p.get("comprador_email"),
            "productos": [f"{i.get('nombre')} × {i.get('cantidad')}" for i in items],
            "subtotal": pesos(p.get("subtotal")), "domicilio": p.get("domicilio"), "total": pesos(p.get("total")),
            "comision": pesos(p.get("comision_monto")), "comision_pct": p.get("comision_porcentaje"),
            "pasarela": pesos(p.get("costo_wompi")), "para_tienda": pesos(p.get("monto_tienda")),
            "metodo": p.get("metodo_pago"), "transaccion": p.get("transaccion_id"),
            "liberado_at": p.get("liberado_at"), "pagado_tienda_at": p.get("pagado_tienda_at"),
        }
        salida.append(fila)
        tot["pedidos"] += 1
        if pagado:
            tot["cobrado"] += fila["total"]; tot["comision"] += fila["comision"]
            tot["pasarela"] += fila["pasarela"]; tot["para_tiendas"] += fila["para_tienda"]
    # Tope de 500 filas por consulta (AUD-ADMIN-001): si se alcanza, se avisa para
    # acotar con fechas en vez de mostrar datos incompletos sin decirlo.
    return {"transacciones": salida, "totales": tot, "limitado": len(filas) >= TOPE_FILAS}


@router.get("/transacciones/planes")
async def tx_planes(request: Request, desde: str | None = None, hasta: str | None = None, buscar: str | None = None):
    """B. Planes de DecoIArte comprados por las tiendas: qué tienda, quién, qué
    plan (y desde cuál), cuánto, cuándo, estado, método y transacción."""
    _exigir_admin(request)
    sb = get_supabase()
    pagos = _rango(sb.table("pagos").select("*, empresas(nombre, email)").eq("tipo", "cambio_plan")
                   .order("created_at", desc=True).limit(TOPE_FILAS), desde, hasta).execute().data or []
    planes = {p["id"]: p for p in (sb.table("planes").select("id, nombre, precio").execute().data or [])}
    b = (buscar or "").strip().lower()
    salida, total = [], 0
    for p in pagos:
        d = p.get("detalle") or {}
        emp = p.get("empresas") or {}
        fila = {
            "fecha": p.get("created_at"), "tienda": emp.get("nombre"), "email": emp.get("email") or d.get("email"),
            "plan": d.get("plan_nuevo") or "(sin detalle: pago anterior al registro)",
            "plan_anterior": (planes.get(d.get("plan_anterior_id")) or {}).get("nombre"),
            "precio_plan": pesos((planes.get(d.get("plan_nuevo_id")) or {}).get("precio")),
            "pagado": pesos(p.get("monto")), "estado": p.get("estado"), "metodo": p.get("metodo"),
            "referencia": p.get("referencia"), "transaccion": p.get("transaccion_id"),
            "vencimiento": "Pago único (sin vencimiento)",
        }
        if b and b not in " ".join(str(v or "") for v in fila.values()).lower():
            continue
        salida.append(fila)
        if (p.get("estado") or "") == "aprobado":
            total += fila["pagado"]
    return {"transacciones": salida, "total": total, "limitado": len(pagos) >= TOPE_FILAS}


@router.get("/transacciones/imagenes")
async def tx_imagenes(request: Request, desde: str | None = None, hasta: str | None = None):
    _exigir_admin(request)
    filas = _rango(get_supabase().table("imagenes_compra").select("id, email, monto, pagada, pagada_en, referencia, wompi_tx_id, created_at, tienda_id")
                   .eq("pagada", True).order("created_at", desc=True).limit(TOPE_FILAS), desde, hasta).execute().data or []
    return {"transacciones": [{"fecha": f.get("pagada_en") or f.get("created_at"), "comprador_email": f.get("email"),
                               "pagado": pesos(f.get("monto")), "referencia": f.get("referencia"), "transaccion": f.get("wompi_tx_id")} for f in filas],
            "total": sum(pesos(f.get("monto")) for f in filas), "limitado": len(filas) >= TOPE_FILAS}