"""
Cuenta del comprador y ventas de la tienda.

Tres reglas que este módulo hace cumplir en el servidor, no en la pantalla:

  1. Cada comprador ve SOLO sus pedidos.
  2. La tienda ve el celular y la dirección de un comprador SOLO cuando el
     pedido ya está pagado, y solo los de ese pedido. Antes la tabla de
     pedidos se leía directo desde el navegador; ahora nadie la lee desde
     ahí (ver sql/cuenta_comprador.sql) y todo pasa por aquí.
  3. Solo reseña quien compró de verdad: el servidor comprueba que exista un
     pedido PAGADO de esa persona con ese producto. Así nadie inventa
     reseñas buenas para su tienda ni malas para la competencia. La tienda
     puede responder; no califica compradores.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.compras import usuario_de_sesion
from app.utils.supabase_client import get_supabase
from app.services.email_service import enviar_correo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cuenta", tags=["cuenta"])

PAGADOS = ("pagado", "enviado", "entregado")
ADMIN_EMAIL = "oscar8a.cds@gmail.com"


def _pedidos_del_comprador(usuario: dict) -> list:
    sb = get_supabase()
    propios = sb.table("pedidos").select("*").eq("user_id", usuario["id"]) \
        .order("created_at", desc=True).limit(60).execute().data or []
    # Pedidos viejos, de antes de ligarlos a la cuenta, se reconocen por el correo
    if usuario["email"]:
        viejos = sb.table("pedidos").select("*").eq("comprador_email", usuario["email"]) \
            .is_("user_id", "null").order("created_at", desc=True).limit(60).execute().data or []
        propios += viejos
    return propios


def _productos_del_pedido(pedido: dict) -> list:
    return [str(i.get("producto_id")) for i in (pedido.get("items") or []) if i.get("producto_id")]


# ── MIS PEDIDOS (comprador) ─────────────────────────────────────────────
@router.get("/pedidos")
async def mis_pedidos(request: Request):
    usuario = await usuario_de_sesion(request)
    pedidos = _pedidos_del_comprador(usuario)
    sb = get_supabase()

    ids_tienda = list({p["tienda_id"] for p in pedidos if p.get("tienda_id")})
    nombres = {}
    if ids_tienda:
        for t in sb.table("tiendas").select("id, nombre").in_("id", ids_tienda).execute().data or []:
            nombres[str(t["id"])] = t["nombre"]

    ya_resenados = {(r["pedido_referencia"], str(r["producto_id"]))
                    for r in (sb.table("resenas").select("pedido_referencia, producto_id")
                              .eq("user_id", usuario["id"]).execute().data or [])}

    salida = []
    for p in pedidos:
        pagado = p.get("estado") in PAGADOS
        salida.append({
            "referencia": p.get("referencia"),
            "tienda":     nombres.get(str(p.get("tienda_id")), "Tienda"),
            "estado":     p.get("estado"),
            "total":      p.get("total"),
            "subtotal":   p.get("subtotal"),
            "domicilio":  p.get("domicilio"),
            "items":      p.get("items") or [],
            "created_at": p.get("created_at"),
            "direccion":  p.get("comprador_direccion"),
            # Productos que ya puede reseñar: pedido pagado y aún sin reseña
            "resenables": [pid for pid in _productos_del_pedido(p)
                           if pagado and (p.get("referencia"), pid) not in ya_resenados],
        })
    return {"pedidos": salida}


# ── MIS VENTAS (tienda) ─────────────────────────────────────────────────
@router.get("/ventas")
async def mis_ventas(request: Request):
    usuario = await usuario_de_sesion(request)
    sb = get_supabase()
    emp = sb.table("empresas").select("id").eq("email", usuario["email"]).maybe_single().execute()
    if not emp or not emp.data:
        raise HTTPException(status_code=403, detail="Esta sección es para cuentas de empresa.")
    pedidos = sb.table("pedidos").select("*").eq("empresa_id", emp.data["id"]) \
        .order("created_at", desc=True).limit(100).execute().data or []

    for p in pedidos:
        if p.get("estado") in PAGADOS:
            p["datos_entrega_visibles"] = True
        else:
            # Antes de pagar la tienda no ve cómo contactar al comprador:
            # solo el primer nombre, para reconocer el pedido.
            nombre = (p.get("comprador_nombre") or "").strip().split(" ")[0]
            p["comprador_nombre"] = nombre or None
            p["comprador_telefono"] = None
            p["comprador_direccion"] = None
            p["comprador_email"] = None
            p["datos_entrega_visibles"] = False
        p.pop("user_id", None)
    return {"pedidos": pedidos}


# ── RESEÑAS ─────────────────────────────────────────────────────────────
class NuevaResena(BaseModel):
    pedido_referencia: str
    producto_id: str
    calificacion: int = Field(ge=1, le=5)
    comentario: str = Field(default="", max_length=600)


class RespuestaResena(BaseModel):
    respuesta: str = Field(min_length=1, max_length=600)


def _limpio(texto: str, largo: int) -> str:
    return " ".join((texto or "").replace("\x00", "").split())[:largo]


@router.post("/resenas")
async def crear_resena(data: NuevaResena, request: Request):
    usuario = await usuario_de_sesion(request)
    pedido = next((p for p in _pedidos_del_comprador(usuario)
                   if p.get("referencia") == data.pedido_referencia), None)
    if not pedido:
        raise HTTPException(status_code=404, detail="Ese pedido no es tuyo.")
    if pedido.get("estado") not in PAGADOS:
        raise HTTPException(status_code=400, detail="Solo puedes reseñar un pedido pagado.")
    if data.producto_id not in _productos_del_pedido(pedido):
        raise HTTPException(status_code=400, detail="Ese producto no está en tu pedido.")

    sb = get_supabase()
    con = sb.table("consumidores").select("nombre").eq("email", usuario["email"]).maybe_single().execute()
    nombre = ((con.data or {}).get("nombre") if con else None) or usuario["email"].split("@")[0]
    # En público solo se muestra el primer nombre y la inicial del apellido
    partes = nombre.split()
    autor = partes[0] + (f" {partes[1][0]}." if len(partes) > 1 and partes[1] else "")

    try:
        r = sb.table("resenas").insert({
            "producto_id":       data.producto_id,
            "tienda_id":         pedido.get("tienda_id"),
            "pedido_referencia": data.pedido_referencia,
            "user_id":           usuario["id"],
            "autor_nombre":      autor[:40],
            "calificacion":      data.calificacion,
            "comentario":        _limpio(data.comentario, 600) or None,
        }).execute()
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Ya reseñaste este producto de ese pedido.")
        logger.error(f"No se pudo guardar la reseña: {e}")
        raise HTTPException(status_code=502, detail="No se pudo guardar la reseña.")
    # Aviso a la tienda (panel + correo): si no se entera, no responde
    try:
        t = sb.table("tiendas").select("empresa_id, nombre").eq("id", pedido.get("tienda_id")).maybe_single().execute()
        tienda = (t.data if t else None) or {}
        estrellas = "⭐" * int(data.calificacion)
        if tienda.get("empresa_id"):
            sb.table("notificaciones").insert({
                "empresa_id": tienda["empresa_id"], "tipo": "resena", "leida": False,
                "titulo": f"{estrellas} Nueva reseña de {autor[:40]}",
                "mensaje": (_limpio(data.comentario, 140) or "Sin comentario") + " — respóndela desde tu dashboard.",
                "datos": {"pedido_referencia": data.pedido_referencia, "producto_id": data.producto_id},
            }).execute()
            e = sb.table("empresas").select("email").eq("id", tienda["empresa_id"]).maybe_single().execute()
            await enviar_correo(((e.data if e else None) or {}).get("email"), f"{estrellas} Nueva reseña en tu tienda",
                f"<p style='font-size:15px'><b>{autor[:40]}</b> calificó tu producto con {estrellas}.</p>"
                f"<p>{_limpio(data.comentario, 600) or '<i>Sin comentario</i>'}</p>"
                "<p>Responder a tiempo genera confianza en los próximos compradores. "
                "<a href='https://decoiarte.com/dashboard' style='color:#7C3AED;font-weight:700'>Responder →</a></p>")
    except Exception as ex:
        logger.error(f"No se pudo avisar de la reseña a la tienda: {ex}")
    return {"ok": True, "id": (r.data or [{}])[0].get("id")}


@router.get("/resenas")
async def ver_resenas(producto_id: str | None = None, tienda_id: str | None = None):
    """Público: reseñas de un producto o de una tienda, sin datos de quién las escribió."""
    if not producto_id and not tienda_id:
        raise HTTPException(status_code=400, detail="Indica el producto o la tienda.")
    q = get_supabase().table("resenas").select(
        "id, producto_id, autor_nombre, calificacion, comentario, respuesta_tienda, respondida_en, created_at")
    q = q.eq("producto_id", producto_id) if producto_id else q.eq("tienda_id", tienda_id)
    crudas = q.order("created_at", desc=True).limit(50).execute().data or []
    # Se arma campo por campo: aunque la consulta cambie algún día, a lo
    # público nunca puede salir quién escribió la reseña.
    PUBLICOS = ("id", "producto_id", "autor_nombre", "calificacion", "comentario",
                "respuesta_tienda", "respondida_en", "created_at")
    filas = [{k: f.get(k) for k in PUBLICOS} for f in crudas]
    promedio = round(sum(f["calificacion"] for f in filas) / len(filas), 1) if filas else None
    return {"resenas": filas, "promedio": promedio, "total": len(filas)}


def _tienda_de_la_empresa(email: str):
    sb = get_supabase()
    emp = sb.table("empresas").select("id").eq("email", email).maybe_single().execute()
    if not emp or not emp.data:
        return None
    t = sb.table("tiendas").select("id").eq("empresa_id", emp.data["id"]).limit(1).execute()
    return str(t.data[0]["id"]) if t and t.data else None


@router.get("/resenas-tienda")
async def resenas_de_mi_tienda(request: Request):
    usuario = await usuario_de_sesion(request)
    tienda = _tienda_de_la_empresa(usuario["email"])
    if not tienda:
        return {"resenas": [], "promedio": None, "total": 0}
    return await ver_resenas(tienda_id=tienda)


@router.post("/resenas/{resena_id}/responder")
async def responder_resena(resena_id: str, data: RespuestaResena, request: Request):
    usuario = await usuario_de_sesion(request)
    sb = get_supabase()
    r = sb.table("resenas").select("id, tienda_id, user_id, respuesta_tienda").eq("id", resena_id).maybe_single().execute()
    if not r or not r.data:
        raise HTTPException(status_code=404, detail="Esa reseña no existe.")
    if usuario["email"] != ADMIN_EMAIL and _tienda_de_la_empresa(usuario["email"]) != str(r.data["tienda_id"]):
        raise HTTPException(status_code=403, detail="Solo la tienda reseñada puede responder.")
    ya_respondida = bool(r.data.get("respuesta_tienda"))   # antes de guardar: ¿es la primera respuesta?
    sb.table("resenas").update({
        "respuesta_tienda": _limpio(data.respuesta, 600),
        "respondida_en":    datetime.now(timezone.utc).isoformat(),
    }).eq("id", resena_id).execute()
    # Aviso al comprador la primera vez que la tienda le responde
    if not ya_respondida and r.data.get("user_id"):
        try:
            u = sb.auth.admin.get_user_by_id(str(r.data["user_id"]))
            correo = getattr(getattr(u, "user", None), "email", None)
            await enviar_correo(correo, "💬 La tienda respondió tu reseña",
                f"<p style='font-size:15px'>La tienda te respondió:</p><p><i>{_limpio(data.respuesta, 600)}</i></p>"
                "<p><a href='https://decoiarte.com/marketplace' style='color:#7C3AED;font-weight:700'>Verla en DecoIArte →</a></p>")
        except Exception as ex:
            logger.error(f"No se pudo avisar al comprador de la respuesta: {ex}")
    return {"ok": True}