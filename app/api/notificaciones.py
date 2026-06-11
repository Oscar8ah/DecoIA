import logging
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from app.utils.config import get_settings, Settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["notificaciones"])


class RegistroNuevo(BaseModel):
    nombre: str
    email:  str
    plan:   str
    precio: str


class PedidoMarketplace(BaseModel):
    tienda_nombre: str
    tienda_ciudad: str
    productos:     str
    subtotal:      int
    origen:        str = "marketplace_web"


# ── HELPER ────────────────────────────────────────────────────────────────
async def enviar_wa(mensaje: str, settings: Settings):
    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to":   settings.whatsapp_asesor_number,
        "type": "text",
        "text": {"body": mensaje}
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            logger.error(f"Error WA: {r.status_code} — {r.text[:100]}")
        return r.status_code == 200


# ── NOTIFICAR REGISTRO ────────────────────────────────────────────────────
@router.post("/notificar-registro")
async def notificar_registro(data: RegistroNuevo):
    """Envía notificación WhatsApp a Oscar cuando se registra un nuevo cliente."""
    settings = get_settings()
    mensaje = (
        f"🎉 *¡Nuevo cliente en DecoIArte!*\n\n"
        f"🏢 *Negocio:* {data.nombre}\n"
        f"📧 *Email:* {data.email}\n"
        f"📦 *Plan:* {data.plan}\n"
        f"💰 *Valor:* {data.precio}\n\n"
        f"👉 Actívalo en: decoiarte.com/admin"
    )
    try:
        await enviar_wa(mensaje, settings)
        logger.info(f"Notificación registro enviada — {data.email}")
    except Exception as e:
        logger.error(f"Error notificación registro: {type(e).__name__} — {e}")
    return {"status": "ok"}


# ── NOTIFICAR PEDIDO MARKETPLACE ──────────────────────────────────────────
@router.post("/notificar-pedido")
async def notificar_pedido(data: PedidoMarketplace):
    """Notifica a Oscar cuando alguien hace un pedido desde el marketplace."""
    settings = get_settings()
    subtotal_fmt = f"${data.subtotal:,}".replace(",", ".")
    mensaje = (
        f"🛒 *Nuevo pedido — DecoIArte Marketplace*\n\n"
        f"🏪 *Tienda:* {data.tienda_nombre}\n"
        f"📍 *Ciudad:* {data.tienda_ciudad}\n"
        f"📦 *Productos:* {data.productos}\n"
        f"💰 *Subtotal:* {subtotal_fmt}\n"
        f"🌐 *Origen:* {data.origen}\n\n"
        f"El cliente está solicitando cotización de domicilio por WhatsApp."
    )
    try:
        await enviar_wa(mensaje, settings)
        logger.info(f"Notificación pedido enviada — {data.tienda_nombre}")
    except Exception as e:
        logger.error(f"Error notificación pedido: {type(e).__name__} — {e}")
    return {"status": "ok"}