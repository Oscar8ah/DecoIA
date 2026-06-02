import logging
import httpx
from fastapi import APIRouter, Request, HTTPException, Depends, BackgroundTasks
from fastapi.responses import PlainTextResponse
from app.utils.config import get_settings, Settings
from app.services.imagen_service import descargar_imagen_whatsapp, generar_imagen_remodelada
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])

mensajes_procesados_cache = set()


async def mensaje_ya_procesado(message_id: str) -> bool:
    if message_id in mensajes_procesados_cache:
        return True
    try:
        supabase = get_supabase()
        result = supabase.table("mensajes_procesados").select("id").eq("message_id", message_id).execute()
        if result.data:
            mensajes_procesados_cache.add(message_id)
            return True
        return False
    except Exception as e:
        logger.error(f"Error verificando mensaje en Supabase: {e}")
        return False


async def marcar_mensaje_procesado(message_id: str):
    mensajes_procesados_cache.add(message_id)
    try:
        supabase = get_supabase()
        supabase.table("mensajes_procesados").insert({
            "message_id": message_id
        }).execute()
    except Exception as e:
        logger.error(f"Error guardando mensaje en Supabase: {e}")


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


async def notificar_asesor(telefono_cliente: str, settings: Settings):
    """Notifica al asesor que un cliente quiere ser atendido"""
    mensaje = (
        f"🔔 *Nuevo cliente solicita asesor*\n\n"
        f"📱 Número: +{telefono_cliente}\n\n"
        f"El cliente está esperando tu respuesta en WhatsApp."
    )
    await enviar_mensaje_whatsapp(settings.whatsapp_asesor_number, mensaje, settings)


async def procesar_imagen_background(sender: str, image_id: str, settings: Settings):
    try:
        await enviar_mensaje_whatsapp(
            sender,
            "📸 ¡Recibí tu foto! Estoy generando la visualización con IA... "
            "Esto toma unos segundos ⏳🤖",
            settings
        )
        imagen_bytes = await descargar_imagen_whatsapp(image_id, settings.whatsapp_token)
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
        logger.error(f"Error en background: {type(e).__name__} - {e}")
        await enviar_mensaje_whatsapp(
            sender,
            "😅 Hubo un error procesando tu foto. Por favor intenta de nuevo.",
            settings
        )


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
async def receive_message(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings)
):
    data = await request.json()
    logger.info("Mensaje recibido de WhatsApp")

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        if "messages" not in value:
            return {"status": "ok"}

        message = value["messages"][0]
        message_id = message.get("id", "")

        if await mensaje_ya_procesado(message_id):
            logger.info(f"Mensaje {message_id} ya procesado, ignorando reintento")
            return {"status": "ok"}

        await marcar_mensaje_procesado(message_id)

        sender = message["from"]
        msg_type = message["type"]

        if msg_type == "text":
            text = message["text"]["body"].strip().lower()
            logger.info(f"Texto de {sender}: {text}")

            # Menú de bienvenida
            if any(saludo in text for saludo in ["hola", "buenas", "buenos", "hi", "hello", "inicio", "start", "menu", "menú"]):
                await enviar_mensaje_whatsapp(
                    sender,
                    "👋 ¡Bienvenido a *DECOIARTE.COM*!\n\n"
                    "Soy tu asistente de remodelación con IA 🏠✨\n\n"
                    "¿Qué deseas hacer?\n\n"
                    "1️⃣ *Remodelar mi espacio con IA*\n"
                    "   Envía una foto y te muestro cómo quedaría\n\n"
                    "2️⃣ *Hablar con un asesor*\n"
                    "   Un experto te atenderá personalmente\n\n"
                    "Responde con *1* o *2*",
                    settings
                )

            # Opción 1 — Remodelar
            elif text in ["1", "remodelar", "remodelacion", "remodelación"]:
                await enviar_mensaje_whatsapp(
                    sender,
                    "🏠 ¡Perfecto! Estoy listo para transformar tu espacio.\n\n"
                    "📸 *Envíame una foto* del espacio que deseas remodelar "
                    "(sala, habitación, cocina, baño, etc.)\n\n"
                    "La IA analizará tu espacio y te mostrará cómo podría quedar. ✨",
                    settings
                )

            # Opción 2 — Asesor
            elif text in ["2", "asesor", "ayuda", "hablar", "persona"]:
                await enviar_mensaje_whatsapp(
                    sender,
                    "👨‍💼 ¡Perfecto! Un asesor experto se pondrá en contacto "
                    "contigo muy pronto.\n\n"
                    "⏰ Tiempo de respuesta: *menos de 24 horas*\n\n"
                    "Mientras tanto puedes enviarnos fotos de tu espacio "
                    "para que el asesor pueda preparar tu propuesta. 🏠",
                    settings
                )
                # Notificar al asesor
                background_tasks.add_task(notificar_asesor, sender, settings)

            # Estilos de remodelación
            elif text in ["clasico", "clásico", "minimalista", "rustico", "rústico", "industrial", "moderno"]:
                await enviar_mensaje_whatsapp(
                    sender,
                    f"🎨 ¡Excelente elección! Procesando estilo *{text}*...\n\n"
                    "📸 Envíame la foto de tu espacio y lo transformo con ese estilo. ✨",
                    settings
                )

            # Cualquier otro texto
            else:
                await enviar_mensaje_whatsapp(
                    sender,
                    "👋 ¡Hola! Soy el asistente de *DECOIARTE.COM* 🏠\n\n"
                    "¿Qué deseas hacer?\n\n"
                    "1️⃣ *Remodelar mi espacio con IA*\n"
                    "2️⃣ *Hablar con un asesor*\n\n"
                    "Responde con *1* o *2*",
                    settings
                )

        elif msg_type == "image":
            image_id = message["image"]["id"]
            logger.info(f"Imagen recibida de {sender}, agregando a background")
            background_tasks.add_task(
                procesar_imagen_background,
                sender,
                image_id,
                settings
            )

    except Exception as e:
        logger.error(f"Error procesando mensaje: {type(e).__name__} - {e}")

    return {"status": "ok"}