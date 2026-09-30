import logging
import time
import secrets
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase
from app.api.compras import usuario_de_sesion
from app.utils.auth import email_de_sesion, exigir_duenio_tienda, ADMIN_EMAIL
from app.utils.dinero import repartir, pesos, en_letras, WOMPI_MINIMO_COP, DOMICILIO_MAXIMO_COP
from app.services.email_service import enviar_correo
from datetime import datetime, timezone, timedelta

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

    # Comprar exige cuenta de comprador. El pedido queda ligado a esa cuenta
    # (para "Mis pedidos" y para poder reseñar) y el correo sale del token,
    # no de lo que escriba el navegador.
    comprador = await usuario_de_sesion(request)

    # La tienda necesita a dónde llevarlo y a quién llamar. Estos datos la
    # tienda los ve SOLO cuando el pedido queda pagado.
    if not data.comprador_nombre.strip() or not data.comprador_telefono.strip() or not data.comprador_direccion.strip():
        raise HTTPException(status_code=400, detail="Completa tu nombre, celular y dirección de entrega.")

    if not data.items:
        raise HTTPException(status_code=400, detail="El pedido no tiene productos.")

    supabase = get_supabase()

    # 1. Verificar la tienda y traer su porcentaje de comisión
    r = supabase.table("tiendas") \
        .select("*") \
        .eq("id", data.tienda_id).maybe_single().execute()

    if not r or not r.data:
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

        precio = pesos(p.get("precio"))              # pesos enteros, sin centavos sueltos
        importe = precio * int(item.cantidad)
        subtotal += importe
        items_final.append({
            "producto_id": p["id"],
            "nombre":      p.get("nombre"),
            "precio":      precio,
            "unidad":      p.get("unidad"),
            "cantidad":    item.cantidad,
            "importe":     importe,
        })

    subtotal = pesos(subtotal)
    if subtotal <= 0:
        raise HTTPException(status_code=400, detail="El total del pedido no es válido.")
    if not tienda.get("empresa_id"):
        logger.error(f"Pedido rechazado: la tienda {tienda.get('nombre')} ({data.tienda_id}) no tiene cuenta dueña")
        raise HTTPException(status_code=400, detail=(
            f"{tienda.get('nombre')} no está recibiendo pedidos en este momento. Prueba con otra tienda."))
    minimo = pesos(tienda.get("pedido_minimo") or 0)
    if subtotal < minimo:
        raise HTTPException(status_code=400, detail=(
            f"{tienda.get('nombre')} recibe pedidos desde {_cop(minimo)} en productos; "
            f"tu pedido va en {_cop(subtotal)}. Agrega {_cop(minimo - subtotal)} más."))

    # 3. Reparto provisional (sin domicilio): el definitivo se hace cuando la
    #    tienda cotiza el domicilio. Todo con app/utils/dinero.py.
    pct = float(tienda.get("comision_porcentaje") or 5.0)
    reparto = repartir(subtotal, 0, pct)
    comision, para_tienda = reparto["comision"], reparto["para_tienda"]

    # Referencia única e impredecible (no secuencial, para que nadie pueda
    # adivinar referencias de otros pedidos)
    referencia = f"DECO-{secrets.token_urlsafe(12)}"

    try:
        ins = supabase.table("pedidos").insert({
            "referencia":          referencia,
            "tienda_id":           data.tienda_id,
            "empresa_id":          tienda.get("empresa_id"),
            "comprador_nombre":    data.comprador_nombre.strip()[:150] or None,
            "comprador_email":     comprador["email"] or None,
            "user_id":             comprador["id"],
            "comprador_telefono":  data.comprador_telefono.strip()[:40] or None,
            "comprador_direccion": data.comprador_direccion.strip()[:300] or None,
            "items":               items_final,
            "subtotal":            subtotal,
            "total":               subtotal,
            "comision_porcentaje": pct,
            "comision_monto":      comision,
            "monto_tienda":        para_tienda,
            # Primero la tienda cotiza el domicilio: un saco de cemento o un tanque
            # gigante no se pueden calcular como un celular. Luego el cliente paga.
            "estado":              "cotizando",
        }).execute()   # supabase 2.15 (Render): insert() no admite .select(); ya devuelve la fila
    except Exception as e:
        logger.error(f"Error creando pedido: {e}")
        raise HTTPException(status_code=502, detail="No se pudo registrar el pedido. Intenta de nuevo.")

    logger.info(f"Pedido {referencia} creado — tienda {tienda.get('nombre')}, productos {subtotal}, esperando domicilio")
    await enviar_correo(ADMIN_EMAIL, f"🛒 Nuevo pedido a {tienda.get('nombre')}: {_cop(subtotal)}",
        f"<p style='font-size:15px'>{data.comprador_nombre} le pidió a <b>{tienda.get('nombre')}</b> "
        f"{_cop(subtotal)} en productos (ref {referencia}). La tienda debe cotizar el domicilio.</p>"
        "<p><a href='https://decoiarte.com/admin' style='color:#7C3AED;font-weight:700'>Ver en el panel →</a></p>")
    await _avisar_tienda(tienda, "🚚 Nuevo pedido: cotiza el domicilio",
        f"{_cop(subtotal)} en productos · Ref {referencia}. Escribe el costo del domicilio para que el cliente pueda pagar.",
        {"referencia": referencia, "subtotal": subtotal, "accion": "cotizar_domicilio"})

    return {
        "status":      "ok",
        "referencia":  referencia,
        "pedido_id":   (ins.data or [{}])[0].get("id") if ins else None,
        "total":       subtotal,
        "total_centavos": subtotal * 100,   # Wompi cobra en centavos (pesos enteros × 100)
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
    if not r or not r.data:
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


# ════════════════════════════════════════════════════════════════════════
#  FLUJO COMPLETO DEL PEDIDO
#  cotizando → (tienda pone domicilio) → por_pagar → (Wompi) → pagado
#  → (tienda) enviado → (comprador) entregado  ·  liberado: la tienda ya
#  puede recibir su dinero  ·  pagado_tienda: el administrador le transfirió.
#  Si el comprador no confirma, se libera sola a los 5 días hábiles.
# ════════════════════════════════════════════════════════════════════════
DIAS_HABILES_PARA_LIBERAR = 5


def _cop(n) -> str:
    return "$" + f"{pesos(n):,}".replace(",", ".")


def _pedido(referencia: str) -> dict:
    r = get_supabase().table("pedidos").select("*").eq("referencia", referencia).maybe_single().execute()
    if not r or not r.data:
        raise HTTPException(status_code=404, detail="Ese pedido no existe.")
    return r.data


def _tienda(tienda_id: str) -> dict:
    r = get_supabase().table("tiendas").select("*").eq("id", tienda_id).maybe_single().execute()
    return (r.data if r else None) or {}


def _correo_empresa(empresa_id) -> str:
    try:
        r = get_supabase().table("empresas").select("email").eq("id", empresa_id).maybe_single().execute()
        return ((r.data if r else None) or {}).get("email") or ""
    except Exception:
        return ""


async def _avisar_tienda(tienda: dict, titulo: str, mensaje: str, datos: dict) -> None:
    """Aviso a la tienda: en su dashboard (campana) y por correo. Si la tienda no
    tiene cuenta que lo reciba, el aviso le llega al administrador: ningún
    pedido se puede quedar esperando sin que nadie se entere."""
    empresa_id = tienda.get("empresa_id")
    correo = _correo_empresa(empresa_id) if empresa_id else ""
    if not empresa_id or not correo:
        logger.error(f"⚠️ La tienda {tienda.get('nombre')} no tiene quién reciba el aviso: {titulo}")
        await enviar_correo(ADMIN_EMAIL, f"⚠️ Aviso sin destinatario — {tienda.get('nombre', 'tienda')}",
            f"<p style='font-size:15px'>La tienda <b>{tienda.get('nombre')}</b> no tiene una cuenta con correo que reciba este aviso:</p>"
            f"<p><b>{titulo}</b><br>{mensaje}</p><p>Atiéndelo tú o contacta a la tienda.</p>")
    if empresa_id:
        try:
            get_supabase().table("notificaciones").insert({
                "empresa_id": empresa_id, "tipo": "pedido", "titulo": titulo,
                "mensaje": mensaje, "leida": False, "datos": datos,
            }).execute()
        except Exception as e:
            logger.error(f"No se pudo guardar el aviso para la tienda {tienda.get('nombre')}: {e}")
        await enviar_correo(correo, titulo, f"<p style='font-size:15px'>{mensaje}</p>"
                            "<p><a href='https://decoiarte.com/dashboard' style='color:#7C3AED;font-weight:700'>Abrir mi dashboard →</a></p>")


def _horas_desde(fecha_iso) -> int:
    try:
        ini = datetime.fromisoformat(str(fecha_iso).replace("Z", "+00:00"))
        if ini.tzinfo is None:
            ini = ini.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - ini).total_seconds() // 3600))
    except Exception:
        return 0


def dias_habiles_desde(fecha_iso: str | None) -> int:
    """Días hábiles (lunes a viernes) cumplidos desde la fecha. No descuenta festivos."""
    if not fecha_iso:
        return 0
    try:
        ini = datetime.fromisoformat(str(fecha_iso).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if ini.tzinfo is None:
        ini = ini.replace(tzinfo=timezone.utc)
    hoy, d, n = datetime.now(timezone.utc).date(), ini.date(), 0
    while d < hoy:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


class DomicilioRequest(BaseModel):
    domicilio: int = Field(..., ge=0, le=DOMICILIO_MAXIMO_COP)


@router.post("/pedido/{referencia}/domicilio")
async def cotizar_domicilio(referencia: str, data: DomicilioRequest, request: Request):
    """La TIENDA escribe el costo del domicilio (de $0 a $50.000.000). Con eso
    queda el total que paga el cliente y el reparto definitivo."""
    p = _pedido(referencia)
    await exigir_duenio_tienda(request, p.get("tienda_id"))
    if p.get("estado") not in ("cotizando", "por_pagar"):
        raise HTTPException(status_code=409, detail="Este pedido ya fue pagado: el domicilio no se puede cambiar.")
    subtotal, domicilio = pesos(p.get("subtotal")), pesos(data.domicilio)
    reparto = repartir(subtotal, domicilio, float(p.get("comision_porcentaje") or 5.0))
    if reparto["total"] < WOMPI_MINIMO_COP:
        raise HTTPException(status_code=400, detail=(
            f"El total quedaría en {_cop(reparto['total'])} y Wompi no cobra menos de {_cop(WOMPI_MINIMO_COP)}. "
            "Revisa el domicilio o pídele al cliente que agregue productos."))
    if reparto["para_tienda"] < 0:
        raise HTTPException(status_code=400, detail=(
            f"Con ese total, los costos administrativos ({_cop(reparto['costos_administrativos'])}) superan lo que "
            f"cobras ({_cop(reparto['total'])}): perderías plata. Revisa el domicilio."))
    get_supabase().table("pedidos").update({
        "domicilio": domicilio, "total": reparto["total"], "comision_monto": reparto["comision"],
        "monto_tienda": reparto["para_tienda"], "costo_wompi": reparto["costo_wompi"],
        "neto_decoiarte": reparto["neto_decoiarte"], "estado": "por_pagar",
        "domicilio_cotizado_at": datetime.now(timezone.utc).isoformat(),
    }).eq("referencia", referencia).execute()
    logger.info(f"Domicilio de {referencia}: {domicilio} → total {reparto['total']} ({en_letras(reparto['total'])} pesos)")
    await enviar_correo(p.get("comprador_email"), f"🚚 Tu pedido está listo para pagar: {_cop(reparto['total'])}",
        f"<p style='font-size:15px'>La tienda cotizó tu domicilio.</p>"
        f"<p>Productos: <b>{_cop(subtotal)}</b><br>Domicilio: <b>{_cop(domicilio)}</b><br>"
        f"<span style='font-size:18px'>Total a pagar: <b>{_cop(reparto['total'])}</b></span><br>"
        f"<span style='color:#6B7280'>({en_letras(reparto['total'])} pesos)</span></p>"
        "<p><a href='https://decoiarte.com/mi-cuenta' style='color:#7C3AED;font-weight:700'>Pagar en Mi cuenta →</a></p>")
    return {"status": "ok", **reparto, "total_en_letras": en_letras(reparto["total"]) + " pesos"}


@router.post("/pedido/{referencia}/enviado")
async def marcar_enviado(referencia: str, request: Request):
    """La TIENDA avisa que despachó el pedido. Desde aquí corren los 5 días hábiles."""
    p = _pedido(referencia)
    await exigir_duenio_tienda(request, p.get("tienda_id"))
    if p.get("estado") != "pagado":
        raise HTTPException(status_code=409, detail="Solo se marca como enviado un pedido pagado y sin despachar.")
    get_supabase().table("pedidos").update({"estado": "enviado", "enviado_at": datetime.now(timezone.utc).isoformat()}) \
        .eq("referencia", referencia).execute()
    await enviar_correo(p.get("comprador_email"), "🚚 Tu pedido va en camino",
        "<p style='font-size:15px'>La tienda despachó tu pedido. Cuando te llegue, confírmalo en "
        "<a href='https://decoiarte.com/mi-cuenta' style='color:#7C3AED;font-weight:700'>Mi cuenta</a>.</p>")
    return {"status": "ok", "estado": "enviado"}


@router.post("/pedido/{referencia}/recibido")
async def confirmar_recibido(referencia: str, request: Request):
    """El COMPRADOR confirma que le llegó: el dinero de la tienda queda liberado."""
    usuario = await usuario_de_sesion(request)
    p = _pedido(referencia)
    if str(p.get("user_id")) != str(usuario["id"]):
        raise HTTPException(status_code=403, detail="Este pedido no es de tu cuenta.")
    if p.get("estado") != "enviado":
        raise HTTPException(status_code=409, detail="Este pedido todavía no figura como enviado.")
    ahora = datetime.now(timezone.utc).isoformat()
    get_supabase().table("pedidos").update({"estado": "entregado", "recibido_at": ahora,
                                            "liberado_at": ahora, "liberado_por": "comprador"}).eq("referencia", referencia).execute()
    await _avisar_liberado(p, "el comprador confirmó que lo recibió")
    return {"status": "ok", "estado": "entregado"}


async def _avisar_liberado(p: dict, motivo: str) -> None:
    tienda = _tienda(p.get("tienda_id"))
    monto = pesos(p.get("monto_tienda"))
    await _avisar_tienda(tienda, "✅ Tu pago está liberado",
        f"Pedido {p.get('referencia')}: {motivo}. Te corresponden {_cop(monto)}.", {"referencia": p.get("referencia"), "monto": monto})
    await enviar_correo(ADMIN_EMAIL, f"💸 Págale {_cop(monto)} a {tienda.get('nombre', 'la tienda')}",
        f"<p style='font-size:15px'>Pedido <b>{p.get('referencia')}</b>: {motivo}.</p>"
        f"<p>Le corresponden <b>{_cop(monto)}</b> ({en_letras(monto)} pesos) a <b>{tienda.get('nombre')}</b>.</p>"
        "<p><a href='https://decoiarte.com/admin' style='color:#7C3AED;font-weight:700'>Abrir el panel →</a></p>")


def _exigir_admin(request: Request) -> None:
    if email_de_sesion(request) != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Solo para el administrador.")


@router.get("/admin/pedidos")
async def admin_pedidos(request: Request):
    """Panel del administrador: cada venta de quién a quién, la comisión, el
    costo de Wompi, lo que le toca a cada tienda y cuántos días hábiles lleva.
    Al consultarlo se liberan solos los pedidos con 5 días hábiles enviados."""
    _exigir_admin(request)
    sb = get_supabase()
    r = sb.table("pedidos").select("*, tiendas(nombre)").neq("estado", "cancelado").order("created_at", desc=True).limit(300).execute()
    filas, tot = [], {"cobrado": 0, "comisiones": 0, "costo_wompi": 0, "neto": 0, "por_pagar_tiendas": 0, "pagado_tiendas": 0}
    for p in (r.data or []):
        dias = dias_habiles_desde(p.get("enviado_at")) if p.get("estado") == "enviado" else 0
        if p.get("estado") == "enviado" and not p.get("liberado_at") and dias >= DIAS_HABILES_PARA_LIBERAR:
            p["liberado_at"], p["liberado_por"] = datetime.now(timezone.utc).isoformat(), "automatico"
            sb.table("pedidos").update({"liberado_at": p["liberado_at"], "liberado_por": "automatico"}).eq("id", p["id"]).execute()
            await _avisar_liberado(p, f"pasaron {DIAS_HABILES_PARA_LIBERAR} días hábiles desde el envío sin reclamo")
        pagado = p.get("estado") in ("pagado", "enviado", "entregado")
        if pagado:
            tot["cobrado"] += pesos(p.get("total")); tot["comisiones"] += pesos(p.get("comision_monto"))
            tot["costo_wompi"] += pesos(p.get("costo_wompi")); tot["neto"] += pesos(p.get("neto_decoiarte"))
            if p.get("pagado_tienda_at"): tot["pagado_tiendas"] += pesos(p.get("monto_tienda"))
            elif p.get("liberado_at"): tot["por_pagar_tiendas"] += pesos(p.get("monto_tienda"))
        filas.append({
            "referencia": p.get("referencia"), "creado": p.get("created_at"), "estado": p.get("estado"),
            "comprador": p.get("comprador_nombre"), "comprador_email": p.get("comprador_email"),
            "tienda": (p.get("tiendas") or {}).get("nombre"), "subtotal": pesos(p.get("subtotal")),
            "domicilio": p.get("domicilio"), "total": pesos(p.get("total")), "comision": pesos(p.get("comision_monto")),
            "comision_porcentaje": p.get("comision_porcentaje"), "para_tienda": pesos(p.get("monto_tienda")),
            "costo_wompi": pesos(p.get("costo_wompi")), "neto_decoiarte": pesos(p.get("neto_decoiarte")),
            "costos_administrativos": pesos(p.get("comision_monto")) + pesos(p.get("costo_wompi")),
            "pagado_at": p.get("pagado_at"), "enviado_at": p.get("enviado_at"), "dias_habiles": dias,
            "horas_esperando": _horas_desde(p.get("created_at")) if p.get("estado") == "cotizando" else None,
            "dias_para_liberar": DIAS_HABILES_PARA_LIBERAR, "liberado_at": p.get("liberado_at"),
            "liberado_por": p.get("liberado_por"), "pagado_tienda_at": p.get("pagado_tienda_at"),
            "comprobante_tienda": p.get("comprobante_pago_tienda"),
        })
    return {"pedidos": filas, "totales": tot}


@router.post("/admin/pedido/{referencia}/liberar")
async def admin_liberar(referencia: str, request: Request):
    """El administrador decide liberar antes de los 5 días (p. ej. en la prueba)."""
    _exigir_admin(request)
    p = _pedido(referencia)
    if p.get("estado") not in ("enviado", "entregado") or p.get("liberado_at"):
        raise HTTPException(status_code=409, detail="Solo se libera un pedido enviado que no esté liberado.")
    get_supabase().table("pedidos").update({"liberado_at": datetime.now(timezone.utc).isoformat(), "liberado_por": "administrador"}) \
        .eq("referencia", referencia).execute()
    await _avisar_liberado(p, "el administrador lo liberó")
    return {"status": "ok"}


class PagoTiendaRequest(BaseModel):
    comprobante: str = Field("", max_length=200)


@router.post("/admin/pedido/{referencia}/pagado-tienda")
async def admin_pagado_tienda(referencia: str, data: PagoTiendaRequest, request: Request):
    """El administrador registra que ya le transfirió a la tienda (mientras no
    esté activo Wompi Pagos a Terceros, que lo haría automático)."""
    _exigir_admin(request)
    p = _pedido(referencia)
    if not p.get("liberado_at"):
        raise HTTPException(status_code=409, detail="Primero debe estar liberado (recibido o 5 días hábiles).")
    if p.get("pagado_tienda_at"):
        raise HTTPException(status_code=409, detail="Ya figura como pagado a la tienda.")
    get_supabase().table("pedidos").update({"pagado_tienda_at": datetime.now(timezone.utc).isoformat(),
                                            "comprobante_pago_tienda": (data.comprobante or "").strip()[:200] or None}) \
        .eq("referencia", referencia).execute()
    tienda = _tienda(p.get("tienda_id"))
    await _avisar_tienda(tienda, f"💸 Te pagamos {_cop(p.get('monto_tienda'))}",
        f"Pedido {referencia}. Comprobante: {data.comprobante or 'sin número'}.", {"referencia": referencia})
    return {"status": "ok"}


@router.post("/pedido/{referencia}/cancelar")
async def cancelar_pedido(referencia: str, request: Request):
    """Cancela un pedido que TODAVÍA NO se ha pagado (cotizando o esperando el
    pago). Lo puede cancelar el comprador dueño del pedido o la tienda. Así se
    borran cotizaciones viejas sin tocar ningún pedido pagado."""
    p = _pedido(referencia)
    if p.get("estado") not in ("cotizando", "por_pagar", "pendiente"):
        raise HTTPException(status_code=409, detail="Este pedido ya fue pagado o cerrado: no se puede cancelar desde aquí.")
    usuario = await usuario_de_sesion(request)
    if str(p.get("user_id")) == str(usuario["id"]):
        quien = "comprador"
    else:
        await exigir_duenio_tienda(request, p.get("tienda_id"))
        quien = "tienda"
    get_supabase().table("pedidos").update({"estado": "cancelado"}).eq("referencia", referencia).execute()
    logger.info(f"Pedido {referencia} cancelado por {quien}")
    if quien == "comprador":
        await _avisar_tienda(_tienda(p.get("tienda_id")), "✖ El cliente canceló un pedido",
            f"Pedido {referencia} ({_cop(p.get('subtotal'))} en productos). Ya no tienes que cotizarlo.", {"referencia": referencia})
    else:
        await enviar_correo(p.get("comprador_email"), "✖ La tienda canceló tu pedido",
            f"<p style='font-size:15px'>La tienda canceló el pedido <b>{referencia}</b>. No se te cobró nada.</p>"
            "<p><a href='https://decoiarte.com/marketplace' style='color:#7C3AED;font-weight:700'>Volver al marketplace →</a></p>")
    return {"status": "ok", "estado": "cancelado"}