import hmac
import hashlib
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from app.utils.config import get_settings, Settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])


def verify_whatsapp_signature(request: Request, body: bytes, settings: Settings) -> bool:
    """Verifica que el mensaje viene realmente de Meta - OWASP A02"""
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        settings.whatsapp_token.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


@router.get("")
async def verify_webhook(request: Request, settings: Settings = Depends(get_settings)):
    """Meta verifica el webhook con este endpoint GET"""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        logger.info("Webhook verificado correctamente")
        return PlainTextResponse(content=challenge)

    raise HTTPException(status_code=403, detail="Token de verificacion invalido")


@router.post("")
async def receive_message(
    request: Request,
    settings: Settings = Depends(get_settings)
):
    """Recibe mensajes de WhatsApp"""
    body = await request.body()

    if not verify_whatsapp_signature(request, body, settings):
        logger.warning("Firma invalida - posible request malicioso")
        raise HTTPException(status_code=401, detail="Firma invalida")

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
                text = message["text"]["body"]
                logger.info(f"Mensaje de texto de {sender}")
                return {"status": "ok", "tipo": "texto", "de": sender}

            elif msg_type == "image":
                logger.info(f"Imagen recibida de {sender}")
                return {"status": "ok", "tipo": "imagen", "de": sender}

    except (KeyError, IndexError) as e:
        logger.error(f"Error procesando mensaje: {e}")

    return {"status": "ok"}