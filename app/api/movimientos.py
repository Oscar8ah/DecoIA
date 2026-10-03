"""
Movimientos de dinero de cada venta (sql/movimientos_dinero.sql).

Una venta ya guarda por separado el total, la comisión de DecoIArte, el costo de
Wompi y lo que le toca a la tienda. Aquí se registra lo que pasa DESPUÉS, una
fila por movimiento, con concepto, estado, comprobante y responsable:
  · desembolso_tienda        → lo que se le transfiere a la tienda
  · devolucion_comprador     → dinero devuelto al comprador (a cargo de la tienda o de DecoIArte)
  · descuento_administrativo → otro descuento a la tienda (retención, ajuste…)

Todos los montos se validan aquí, con los valores guardados en la base: el
navegador nunca decide cuánto se paga, se devuelve o se descuenta.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.utils.supabase_client import get_supabase
from app.utils.dinero import pesos
from app.utils.auth import email_de_sesion, ADMIN_EMAIL
from app.services.email_service import enviar_correo

logger = logging.getLogger(__name__)
router = APIRouter(tags=["movimientos"])

TIPOS = ("desembolso_tienda", "devolucion_comprador", "descuento_administrativo")
CON_DINERO = ("pagado", "enviado", "entregado", "reembolsado")   # el comprador ya pagó
NOMBRE = {"desembolso_tienda": "Pago a la tienda", "devolucion_comprador": "Devolución al comprador",
          "descuento_administrativo": "Descuento administrativo"}


def _admin(request: Request) -> str:
    email = email_de_sesion(request)
    if email != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Solo el administrador.")
    return email


def _cop(n) -> str:
    return "$" + f"{pesos(n):,}".replace(",", ".")


def movimientos_de(pedido_ids: list) -> dict:
    """{pedido_id: [movimientos]} en UNA consulta. Si la tabla aún no existe
    (falta correr el SQL), devuelve vacío y todo sigue como antes."""
    ids = [i for i in pedido_ids if i]
    if not ids:
        return {}
    try:
        r = get_supabase().table("movimientos_dinero").select("*").in_("pedido_id", ids) \
            .order("created_at").execute()
    except Exception as e:
        logger.warning(f"Movimientos no disponibles (¿falta sql/movimientos_dinero.sql?): {e}")
        return {}
    salida = {}
    for m in (r.data or []):
        salida.setdefault(m["pedido_id"], []).append(m)
    return salida


def saldo(p: dict, movs: list) -> dict:
    """Cuentas de UN pedido, solo con movimientos no anulados."""
    vivos = [m for m in (movs or []) if m.get("estado") != "anulado"]
    descuentos = sum(pesos(m["monto"]) for m in vivos if m["tipo"] == "descuento_administrativo")
    dev = [m for m in vivos if m["tipo"] == "devolucion_comprador"]
    dev_tienda = sum(pesos(m["monto"]) for m in dev if m.get("a_cargo_de") == "tienda")
    dev_decoiarte = sum(pesos(m["monto"]) for m in dev if m.get("a_cargo_de") == "decoiarte")
    desembolso = next((m for m in vivos if m["tipo"] == "desembolso_tienda"), None)
    base = pesos(p.get("monto_tienda"))
    return {
        "para_tienda_inicial": base,                       # lo que calculó la venta
        "descuentos_admin": descuentos,
        "devoluciones_a_cargo_tienda": dev_tienda,
        "devoluciones_a_cargo_decoiarte": dev_decoiarte,
        "devuelto_total": dev_tienda + dev_decoiarte,
        "devuelto_realizado": sum(pesos(m["monto"]) for m in dev if m.get("estado") == "realizado"),
        "para_tienda": max(base - descuentos - dev_tienda, 0),   # lo que de verdad le toca
        "desembolso_estado": desembolso.get("estado") if desembolso else None,
        "desembolsado": pesos(desembolso["monto"]) if desembolso and desembolso.get("estado") == "realizado" else 0,
    }


def _pedido(referencia: str) -> dict:
    r = get_supabase().table("pedidos").select("*").eq("referencia", referencia).maybe_single().execute()
    if not r or not r.data:
        raise HTTPException(status_code=404, detail="Ese pedido no existe.")
    return r.data


async def _avisar_tienda_pedido(p: dict, titulo: str, mensaje: str) -> None:
    from app.api.pedidos import _avisar_tienda, _tienda   # import tardío: evita el ciclo
    try:
        await _avisar_tienda(_tienda(p.get("tienda_id")), titulo, mensaje, {"referencia": p.get("referencia")})
    except Exception as e:
        logger.error(f"No se pudo avisar a la tienda del pedido {p.get('referencia')}: {e}")


async def _efectos(p: dict, m: dict, antes: Optional[str]) -> None:
    """Lo que cambia en el pedido y a quién se avisa cuando un movimiento queda
    realizado (o se anula uno que ya estaba realizado)."""
    sb = get_supabase()
    ref = p.get("referencia")
    if m["estado"] == "realizado":
        if m["tipo"] == "desembolso_tienda":
            sb.table("pedidos").update({"pagado_tienda_at": m.get("realizado_at"),
                                        "comprobante_pago_tienda": m.get("comprobante")}).eq("id", p["id"]).execute()
            await _avisar_tienda_pedido(p, f"💸 Te pagamos {_cop(m['monto'])}",
                f"Pedido {ref}. Comprobante: {m.get('comprobante') or 'sin número'}.")
        elif m["tipo"] == "descuento_administrativo":
            await _avisar_tienda_pedido(p, f"🧾 Descuento administrativo: {_cop(m['monto'])}",
                f"Pedido {ref}. Concepto: {m['concepto']}. Se descuenta de lo que recibes por esta venta.")
        elif m["tipo"] == "devolucion_comprador":
            movs = movimientos_de([p["id"]]).get(p["id"], [])
            s = saldo(p, movs)
            total = pesos(p.get("total"))
            if s["devuelto_realizado"] >= total and p.get("estado") != "reembolsado":
                sb.table("pedidos").update({"estado": "reembolsado",
                                            "reembolsado_at": datetime.now(timezone.utc).isoformat()}).eq("id", p["id"]).execute()
            cargo = " Se descuenta de lo que recibes por esta venta." if m.get("a_cargo_de") == "tienda" else ""
            await _avisar_tienda_pedido(p, f"↩️ Devolución al comprador: {_cop(m['monto'])}",
                f"Pedido {ref}. Motivo: {m['concepto']}.{cargo}")
            if p.get("comprador_email"):
                try:
                    await enviar_correo(p["comprador_email"], f"↩️ Te devolvimos {_cop(m['monto'])}",
                        f"<p style='font-size:15px'>Registramos la devolución de <b>{_cop(m['monto'])}</b> de tu pedido <b>{ref}</b>.</p>"
                        f"<p>Motivo: {m['concepto']}.</p><p>Según tu banco o medio de pago, puede tardar unos días en verse reflejada.</p>")
                except Exception as e:
                    logger.error(f"No se pudo avisar al comprador de la devolución {ref}: {e}")
    elif m["estado"] == "anulado" and antes == "realizado" and m["tipo"] == "desembolso_tienda":
        sb.table("pedidos").update({"pagado_tienda_at": None, "comprobante_pago_tienda": None}).eq("id", p["id"]).execute()


def _anotar_actividad(p: dict, m: dict, titulo: str) -> None:
    try:
        get_supabase().table("eventos").insert({
            "tipo": "movimiento_" + m["tipo"], "nivel": "dinero", "titulo": titulo,
            "detalle": {"concepto": m["concepto"], "estado": m["estado"], "a_cargo_de": m.get("a_cargo_de"),
                        "comprobante": m.get("comprobante"), "registrado_por": m.get("registrado_por")},
            "empresa_id": m.get("empresa_id"), "monto": pesos(m["monto"]), "referencia": p.get("referencia"),
        }).execute()
    except Exception as e:
        logger.error(f"No se pudo anotar el movimiento en la actividad: {e}")


# ── Consultar ───────────────────────────────────────────────────────────────
@router.get("/admin/pedido/{referencia}/movimientos")
async def ver_movimientos(referencia: str, request: Request):
    _admin(request)
    p = _pedido(referencia)
    movs = movimientos_de([p["id"]]).get(p["id"], [])
    return {"pedido": {"referencia": referencia, "estado": p.get("estado"), "total": pesos(p.get("total")),
                       "liberado_at": p.get("liberado_at")},
            "movimientos": movs, "saldo": saldo(p, movs)}


# ── Registrar ───────────────────────────────────────────────────────────────
class MovimientoRequest(BaseModel):
    tipo: str
    monto: int = Field(..., gt=0)
    concepto: str = Field(..., min_length=3, max_length=300)
    a_cargo_de: Optional[str] = None
    comprobante: str = Field("", max_length=200)
    realizado: bool = False
    clave: str = Field(..., min_length=8, max_length=80)


async def registrar(referencia: str, data: MovimientoRequest, registrado_por: str) -> dict:
    if data.tipo not in TIPOS:
        raise HTTPException(status_code=400, detail="Tipo de movimiento no válido.")
    sb = get_supabase()
    # Idempotencia: la misma clave devuelve el mismo movimiento (doble clic, reintento)
    ya = sb.table("movimientos_dinero").select("*").eq("clave", data.clave).limit(1).execute()
    if ya and ya.data:
        return {"status": "ok", "movimiento": ya.data[0], "repetido": True}

    p = _pedido(referencia)
    if p.get("estado") not in CON_DINERO:
        raise HTTPException(status_code=409, detail="Ese pedido no tiene un pago confirmado.")
    movs = movimientos_de([p["id"]]).get(p["id"], [])
    s = saldo(p, movs)
    monto = pesos(data.monto)
    hay_desembolso = s["desembolso_estado"] is not None

    if data.tipo == "descuento_administrativo":
        if hay_desembolso:
            raise HTTPException(status_code=409, detail="Ya hay un pago a la tienda registrado: anúlalo antes de descontar.")
        if monto > s["para_tienda"]:
            raise HTTPException(status_code=409, detail=f"No se puede descontar más de lo que recibe la tienda ({_cop(s['para_tienda'])}).")
    elif data.tipo == "devolucion_comprador":
        if data.a_cargo_de not in ("tienda", "decoiarte"):
            raise HTTPException(status_code=400, detail="Indica quién asume la devolución: la tienda o DecoIArte.")
        disponible = pesos(p.get("total")) - s["devuelto_total"]
        if monto > disponible:
            raise HTTPException(status_code=409, detail=f"No se puede devolver más de lo pagado ({_cop(disponible)} disponibles).")
        if data.a_cargo_de == "tienda":
            if hay_desembolso:
                raise HTTPException(status_code=409, detail="La tienda ya tiene un pago registrado: una devolución a su cargo se arregla aparte con ella.")
            if monto > s["para_tienda"]:
                raise HTTPException(status_code=409, detail=f"A cargo de la tienda no puede superar lo que ella recibe ({_cop(s['para_tienda'])}).")
    elif data.tipo == "desembolso_tienda":
        if not p.get("liberado_at"):
            raise HTTPException(status_code=409, detail="Primero debe estar liberado (recibido o 5 días hábiles).")
        if hay_desembolso:
            raise HTTPException(status_code=409, detail="Ese pedido ya tiene un pago a la tienda registrado.")
        if monto != s["para_tienda"] or monto <= 0:
            raise HTTPException(status_code=409, detail=f"El pago a la tienda debe ser exactamente {_cop(s['para_tienda'])}.")

    ahora = datetime.now(timezone.utc).isoformat()
    fila = {"pedido_id": p["id"], "referencia": referencia, "empresa_id": p.get("empresa_id"),
            "tipo": data.tipo, "monto": monto, "concepto": data.concepto.strip(),
            "a_cargo_de": data.a_cargo_de if data.tipo == "devolucion_comprador" else None,
            "estado": "realizado" if data.realizado else "pendiente",
            "comprobante": (data.comprobante or "").strip() or None,
            "registrado_por": registrado_por, "realizado_at": ahora if data.realizado else None,
            "clave": data.clave}
    try:
        r = sb.table("movimientos_dinero").insert(fila).execute()
    except Exception as e:
        # Dos clics simultáneos con la misma clave, o un segundo desembolso: la base lo frena
        ya = sb.table("movimientos_dinero").select("*").eq("clave", data.clave).limit(1).execute()
        if ya and ya.data:
            return {"status": "ok", "movimiento": ya.data[0], "repetido": True}
        logger.error(f"No se pudo registrar el movimiento de {referencia}: {e}")
        raise HTTPException(status_code=409, detail="No se pudo registrar (¿ya existe un pago a la tienda para este pedido?).")
    m = (r.data or [fila])[0]
    _anotar_actividad(p, m, f"{'💸' if data.tipo == 'desembolso_tienda' else '↩️' if data.tipo == 'devolucion_comprador' else '🧾'} "
                            f"{NOMBRE[data.tipo]} {_cop(monto)} · {referencia} ({m['estado']})")
    if m["estado"] == "realizado":
        await _efectos(p, m, None)
    return {"status": "ok", "movimiento": m}


@router.post("/admin/pedido/{referencia}/movimiento")
async def nuevo_movimiento(referencia: str, data: MovimientoRequest, request: Request):
    return await registrar(referencia, data, _admin(request))


# ── Cambiar estado ──────────────────────────────────────────────────────────
class EstadoRequest(BaseModel):
    estado: str
    comprobante: str = Field("", max_length=200)
    motivo: str = Field("", max_length=300)


@router.post("/admin/movimiento/{mov_id}/estado")
async def cambiar_estado(mov_id: str, data: EstadoRequest, request: Request):
    quien = _admin(request)
    sb = get_supabase()
    r = sb.table("movimientos_dinero").select("*").eq("id", mov_id).maybe_single().execute()
    m = r.data if r else None
    if not m:
        raise HTTPException(status_code=404, detail="Ese movimiento no existe.")
    antes = m["estado"]
    if data.estado == antes:
        return {"status": "ok", "movimiento": m, "repetido": True}
    permitido = {"pendiente": ("realizado", "anulado"), "realizado": ("anulado",)}
    if data.estado not in permitido.get(antes, ()):
        raise HTTPException(status_code=409, detail=f"Un movimiento {antes} no puede pasar a {data.estado}.")
    if data.estado == "anulado" and len(data.motivo.strip()) < 3:
        raise HTTPException(status_code=400, detail="Escribe el motivo de la anulación.")
    cambios = {"estado": data.estado}
    if data.estado == "realizado":
        cambios["realizado_at"] = datetime.now(timezone.utc).isoformat()
        if data.comprobante.strip():
            cambios["comprobante"] = data.comprobante.strip()
    else:
        cambios["anulado_motivo"] = f"{data.motivo.strip()} (por {quien})"
    # Solo cambia si sigue en el estado que se leyó: dos clics no aplican dos veces
    r2 = sb.table("movimientos_dinero").update(cambios).eq("id", mov_id).eq("estado", antes).execute()
    if not (r2 and r2.data):
        raise HTTPException(status_code=409, detail="Ese movimiento cambió mientras tanto: recarga.")
    m = r2.data[0]
    p = sb.table("pedidos").select("*").eq("id", m["pedido_id"]).maybe_single().execute().data
    _anotar_actividad(p, m, f"{NOMBRE[m['tipo']]} {_cop(m['monto'])} · {p.get('referencia')}: {antes} → {m['estado']}")
    await _efectos(p, m, antes)
    return {"status": "ok", "movimiento": m}


# ── Lista global para el Super Admin ────────────────────────────────────────
@router.get("/admin/movimientos")
async def lista_movimientos(request: Request, tipo: str = "", estado: str = "", limite: int = 200):
    _admin(request)
    q = get_supabase().table("movimientos_dinero").select("*").order("created_at", desc=True).limit(max(1, min(limite, 500)))
    if tipo in TIPOS:
        q = q.eq("tipo", tipo)
    if estado in ("pendiente", "realizado", "anulado"):
        q = q.eq("estado", estado)
    try:
        filas = q.execute().data or []
    except Exception as e:
        logger.warning(f"Movimientos no disponibles: {e}")
        return {"movimientos": [], "totales": {}, "falta_sql": True}
    tot = {t: {"pendiente": 0, "realizado": 0} for t in TIPOS}
    for m in filas:
        if m["estado"] in ("pendiente", "realizado"):
            tot[m["tipo"]][m["estado"]] += pesos(m["monto"])
    return {"movimientos": filas, "totales": tot, "limitado": len(filas) >= limite}