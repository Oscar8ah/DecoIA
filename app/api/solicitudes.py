import logging
import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.whatsapp import ASESOR_NUMERO, enviar_mensaje_whatsapp
from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["solicitudes"])

_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 20
VENTANA_SEGUNDOS  = 3600
MAX_ITEMS = 30
# Solo se aceptan imágenes de nuestro propio Storage de Supabase
STORAGE_PERMITIDO = "https://cqwbsikyzdtegutyvgfo.supabase.co/storage/"


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    historial = _peticiones_por_ip[ip]
    while historial and ahora - historial[0] > VENTANA_SEGUNDOS:
        historial.popleft()
    if len(historial) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes desde esta conexión. Intenta más tarde.")
    historial.append(ahora)


class ItemCarrito(BaseModel):
    id: str = ""
    nombre: str
    precio: float = 0
    unidad: str = ""


class SolicitudAsesorRequest(BaseModel):
    tienda_id: str
    cliente_telefono: str = ""
    items: list[ItemCarrito] = Field(default_factory=list, max_length=MAX_ITEMS)
    imagen_render_url: str = ""


def _formatear_cop(valor) -> str:
    return f"${valor:,.0f}".replace(",", ".")


@router.post("/notificar-carrito-asesor")
async def notificar_carrito_asesor(data: SolicitudAsesorRequest, request: Request):
    """
    Se llama desde remodelar.html cuando el cliente, con productos ya en su
    carrito, pide hablar con un asesor en vez de pagar directo. A diferencia
    del viejo flujo de selector.html (nunca conectado), este SÍ recibe datos
    reales del carrito, y notifica al asesor de la tienda correspondiente con
    la lista completa de productos — no un texto genérico.
    """
    _verificar_limite_ip(request)

    if not data.tienda_id:
        raise HTTPException(status_code=400, detail="Falta tienda_id.")

    supabase = get_supabase()
    settings = get_settings()

    r = supabase.table("tiendas") \
        .select("id, nombre, empresa_id, empresas(id, nombre, whatsapp_numero_solicitado, whatsapp_phone_number_id, planes(nombre))") \
        .eq("id", data.tienda_id).maybe_single().execute()

    if not r.data:
        raise HTTPException(status_code=404, detail="Tienda no encontrada.")

    tienda  = r.data
    empresa = tienda.get("empresas") or {}
    plan_nombre = (empresa.get("planes") or {}).get("nombre", "").lower()

    # Multiasesor (número propio) solo si el plan lo incluye — mismo criterio
    # que ya usa el bot. Si no, cae al número genérico de DecoIArte.
    if plan_nombre in ("premium", "corporativo") and empresa.get("whatsapp_numero_solicitado"):
        asesor_numero   = empresa["whatsapp_numero_solicitado"]
        phone_number_id = empresa.get("whatsapp_phone_number_id")
    else:
        asesor_numero   = ASESOR_NUMERO
        phone_number_id = None

    # Este endpoint lo usa el CLIENTE (no el dueño), así que no se exige dueño.
    # Pero lo que llega al WhatsApp del asesor no puede ser inventado:
    # - nombre y precio salen de la base, por id, y solo de ESA tienda
    # - el link de la imagen solo puede ser de nuestro Storage (nada de links de estafa)
    # - el teléfono solo dígitos
    ids = [it.id for it in data.items if it.id][:MAX_ITEMS]
    reales = {}
    if ids:
        try:
            rp = supabase.table("productos").select("id, nombre, precio, unidad") \
                .in_("id", ids).eq("tienda_id", data.tienda_id).execute()
            reales = {str(p["id"]): p for p in (rp.data or [])}
        except Exception as e:
            logger.error(f"No se pudieron leer los productos del carrito: {e}")
    items_dict = []
    for it in data.items:
        p = reales.get(str(it.id)) if it.id else None
        if p:
            items_dict.append({"nombre": (p.get("nombre") or "")[:200], "precio": float(p.get("precio") or 0),
                               "unidad": (p.get("unidad") or "")[:20]})
        elif not it.id:
            # Sin id (pantalla vieja en caché): se muestra el nombre, pero el precio no se cree
            items_dict.append({"nombre": it.nombre.strip()[:200] + " (precio por confirmar)", "precio": 0.0,
                               "unidad": it.unidad.strip()[:20]})
        # con id que no es de esta tienda: se descarta
    telefono = re.sub(r"\D", "", data.cliente_telefono or "")
    if not (7 <= len(telefono) <= 15):
        telefono = ""
    imagen_url = data.imagen_render_url or ""
    if not imagen_url.startswith(STORAGE_PERMITIDO):
        imagen_url = ""
    total = sum(it["precio"] for it in items_dict)

    # ── Notificar al asesor por WhatsApp con la info real del carrito ──────
    if items_dict:
        lineas_productos = "\n".join(f"   • {it['nombre']} — {_formatear_cop(it['precio'])}/{it['unidad'] or 'unidad'}" for it in items_dict)
        cuerpo_productos = f"🛍️ *Productos en su carrito:*\n{lineas_productos}\n\n💰 *Total estimado:* {_formatear_cop(total)}\n\n"
    else:
        cuerpo_productos = "🛍️ *Sin productos en el carrito todavía — quiere hablar antes de elegir.*\n\n"

    mensaje = (
        f"🔔 *Cliente pide asesor — {tienda.get('nombre', 'tu tienda')}*\n\n"
        f"📱 *Cliente:* {('+' + telefono) if telefono else 'no identificado'}\n\n"
        f"{cuerpo_productos}"
        + (f"🖼️ *Imagen generada:* {imagen_url}\n\n" if imagen_url else "")
        + "⚡ Contáctalo cuanto antes."
    )

    try:
        await enviar_mensaje_whatsapp(asesor_numero, mensaje, settings, phone_number_id)
    except Exception as e:
        logger.error(f"Error notificando al asesor desde carrito: {e}")
        # No tumbamos la petición solo porque falló el WhatsApp — igual
        # guardamos la solicitud para que el asesor la vea en el dashboard.

    # ── Guardar la solicitud (para la lista del dashboard + cotizador) ─────
    try:
        supabase.table("solicitudes_asesor").insert({
            "tienda_id":         data.tienda_id,
            "empresa_id":        empresa.get("id"),
            "cliente_telefono":  telefono or None,
            "items":             items_dict,
            "imagen_render_url": imagen_url or None,
            "total":             total,
        }).execute()
    except Exception as e:
        logger.error(f"Error guardando solicitud de asesor: {e}")
        raise HTTPException(status_code=502, detail="No se pudo registrar la solicitud. Intenta de nuevo.")

    return {"status": "ok", "asesor_numero": asesor_numero}