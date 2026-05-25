import logging
import httpx
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from app.utils.config import get_settings, Settings
from app.services.imagen_service import descargar_imagen_whatsapp, generar_imagen_remodelada

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])


async def enviar_mensaje_whatsapp(telefono: str, mensaje: str, settings: Settings):
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


async def enviar_imagen_whatsapp(telefono: str, url_imagen: str, caption: str, settings: Settings):
    """Envía imagen generada por IA al usuario"""
    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "image",
        "image": {
            "link": url_imagen,
            "caption": caption
        }
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=data, headers=headers)
        logger.info(f"Imagen enviada: {response.status_code}")


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
    data = await request.json()
    logger.info("Mensaje recibido de WhatsApp")

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return {"status": "ok"}

        message = value["messages"][0]
        sender = message["from"]
        msg_type = message["type"]

        if msg_type == "text":
            text = message["text"]["body"].strip()
            logger.info(f"Texto de {sender}: {text}")
            respuesta = (
                "👋 ¡Bienvenido a *DECOIA.COM*!\n\n"
                "Soy tu asistente de remodelación con IA 🏠✨\n\n"
                "📸 Envíame una *foto de tu espacio* y te mostraré "
                "cómo puede quedar remodelado con pisos y acabados nuevos.\n\n"
                "¿Listo para comenzar?"
            )
            await enviar_mensaje_whatsapp(sender, respuesta, settings)

        elif msg_type == "image":
            logger.info(f"Imagen recibida de {sender}")

            await enviar_mensaje_whatsapp(
                sender,
                "📸 ¡Recibí tu foto! Estoy generando la visualización con IA... "
                "Esto toma unos segundos ⏳🤖",
                settings
            )

            image_id = message["image"]["id"]
            imagen_bytes = await descargar_imagen_whatsapp(
                image_id, settings.whatsapp_token
            )

            url_generada = await generar_imagen_remodelada(imagen_bytes, "moderno")

            await enviar_imagen_whatsapp(
                sender,
                url_generada,
                "✨ ¡Así podría quedar tu espacio remodelado! "
                "Diseño moderno con acabados premium 🏠\n\n"
                "¿Te gustaría ver otro estilo? Responde con:\n"
                "• *clasico*\n• *minimalista*\n• *rustico*\n• *industrial*",
                settings
            )

    except Exception as e:
        logger.error(f"Error procesando mensaje: {type(e).__name__} - {e}")
        await enviar_mensaje_whatsapp(
            sender,
            "😅 Hubo un error procesando tu solicitud. Por favor intenta de nuevo.",
            settings
        )

    return {"status": "ok"}