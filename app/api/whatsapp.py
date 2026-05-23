import hmac
import hashlib
import logging
import httpx
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from app.utils.config import get_settings, Settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])


async def enviar_mensaje_whatsapp(telefono: str, mensaje: str, settings: Settings):
    """Envía mensaje de texto via WhatsApp Cloud API"""
    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje}
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=data, headers=headers)
        logger.info(f"Mensaje enviado: {response.status_code}")
        return response.status_code


@router.get("")
async def verify_webhook(request: Request, settings: Settings = Depends(get_settings)):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        logger.info("Webhook verificado correctamente")
        return PlainTextResponse(content=challenge)
    raise HTTPException(status_code=403, detail="Token invalido")


@router.post("")
async def receive_message(request: Request, settings: Settings = Depends(get_settings)):
    body = await request.body()
    data = await request.json()
    logger.info("Mensaje recibido de WhatsApp")

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" in value:
            message = value["messages"][0]
            sender = message["from"]
            msg_type = message["type"]

            if msg_type == "text":
                text = message["text"]["body"].strip()
                logger.info(f"Texto de {sender}: {text}")

                respuesta = (
                    "👋 ¡Bienvenido a *DECOIA.COM*!\n\n"
                    "Soy tu asistente de remodelación con IA 🏠✨\n\n"
                    "📸 Envíame una foto de tu espacio y te mostraré cómo puede quedar remodelado.\n\n"
                    "¿Listo para comenzar?"
                )
                await enviar_mensaje_whatsapp(sender, respuesta, settings)

            elif msg_type == "image":
                logger.info(f"Imagen de {sender}")
                await enviar_mensaje_whatsapp(
                    sender,
                    "📸 ¡Recibí tu imagen! Estoy procesando la visualización con IA... 🤖✨",
                    settings
                )

    except (KeyError, IndexError) as e:
        logger.error(f"Error: {e}")

    return {"status": "ok"}