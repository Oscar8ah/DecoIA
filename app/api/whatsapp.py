import logging
import httpx
from fastapi import APIRouter, Request, HTTPException, Depends, BackgroundTasks
from fastapi.responses import PlainTextResponse
from app.utils.config import get_settings, Settings
from app.services.imagen_service import descargar_imagen_whatsapp, generar_imagen_remodelada, generar_vista_isometrica
from app.services.openai_service import analizar_espacio_foto, analizar_plano, analizar_mensaje_texto
from app.utils.supabase_client import get_supabase
from datetime import datetime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])

mensajes_procesados_cache = set()
estado_usuarios = {}


def get_saludo() -> str:
    hora = datetime.now().hour
    if 5 <= hora < 12:
        return "¡Buenos días"
    elif 12 <= hora < 18:
        return "¡Buenas tardes"
    else:
        return "¡Buenas noches"


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


async def enviar_botones_whatsapp(telefono: str, mensaje: str, botones: list, settings: Settings):
    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": mensaje},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": btn["id"],
                            "title": btn["title"]
                        }
                    }
                    for btn in botones
                ]
            }
        }
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=data, headers=headers)
        logger.info(f"Botones enviados: {response.status_code}")
        if response.status_code != 200:
            logger.error(f"Error botones: {response.text}")


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


async def enviar_menu_principal(telefono: str, settings: Settings):
    saludo = get_saludo()
    await enviar_botones_whatsapp(
        telefono,
        f"{saludo}! 👋 Bienvenido a *DECOIARTE.COM* 🏠✨\n\n"
        f"Soy tu asistente de remodelación con IA.\n\n"
        f"¿Qué te gustaría hacer hoy?",
        [
            {"id": "btn_remodelar", "title": "🏠 Remodelar"},
            {"id": "btn_plano",    "title": "📐 Mi plano"},
            {"id": "btn_asesor",   "title": "👨‍💼 Asesor"},
        ],
        settings
    )


async def procesar_imagen_background(sender: str, image_id: str, settings: Settings):
    """Procesa foto de espacio o plano según el modo del usuario"""
    try:
        imagen_bytes = await descargar_imagen_whatsapp(image_id, settings.whatsapp_token)
        modo = estado_usuarios.get(sender, {}).get("modo", "remodelar")

        # ── MODO PLANO ─────────────────────────────────────────────────────
        if modo == "plano":
            await enviar_mensaje_whatsapp(
                sender,
                "📐 ¡Recibí tu plano! Analizando distribución con IA y normas NTC colombianas...\n"
                "Esto toma unos segundos ⏳🤖",
                settings
            )

            resultado = analizar_plano(imagen_bytes)

            if not resultado.get("es_plano", True):
                await enviar_mensaje_whatsapp(
                    sender,
                    "🤔 La imagen que enviaste no parece un plano arquitectónico.\n\n"
                    "Por favor envía:\n"
                    "• Una foto del plano impreso 📄\n"
                    "• Una foto de tu boceto dibujado ✏️\n"
                    "• O una foto del espacio real 📸",
                    settings
                )
                await enviar_menu_principal(sender, settings)
                return

            habitaciones  = resultado.get("habitaciones", "")
            area          = resultado.get("area_estimada", "Por determinar")
            pregunta      = resultado.get("pregunta", "¿Qué espacio quieres visualizar primero?")
            tipo          = resultado.get("tipo_plano", "espacio")
            distribucion  = resultado.get("distribucion", "")

            await enviar_mensaje_whatsapp(
                sender,
                f"📐 *Plano analizado con IA* ✅\n\n"
                f"🏠 *Tipo:* {tipo}\n"
                f"📋 *Habitaciones detectadas:* {habitaciones}\n"
                f"📏 *Área estimada:* {area}\n"
                f"🗺️ *Distribución:* {distribucion}\n\n"
                f"💡 {pregunta}",
                settings
            )

            # Generar vista 3D isométrica
            await enviar_mensaje_whatsapp(
                sender,
                "🎨 Generando vista 3D de tu espacio...\n"
                "Esto toma unos segundos ⏳✨",
                settings
            )

            url_generada = await generar_vista_isometrica(imagen_bytes, resultado)

            await enviar_imagen_whatsapp(
                sender,
                url_generada,
                f"🏠 *Vista 3D isométrica de tu {tipo}* ✨\n\n"
                f"📏 Área estimada: {area}\n"
                f"🛋️ Con mobiliario y acabados modernos aplicados",
                settings
            )

            estado_usuarios[sender] = {
                "modo": "plano_analizado",
                "plano_info": resultado
            }

            await enviar_botones_whatsapp(
                sender,
                "¿Qué deseas hacer ahora?",
                [
                    {"id": "btn_remodelar", "title": "🔄 Otro estilo"},
                    {"id": "btn_asesor",    "title": "👨‍💼 Asesor"},
                ],
                settings
            )

        # ── MODO REMODELAR ─────────────────────────────────────────────────
        else:
            await enviar_mensaje_whatsapp(
                sender,
                "📸 ¡Recibí tu foto! Analizando tu espacio con IA...\n"
                "Esto toma unos segundos ⏳🤖",
                settings
            )

            analisis    = analizar_espacio_foto(imagen_bytes)
            tipo_espacio = analisis.get("tipo_espacio", "espacio")
            pregunta    = analisis.get("pregunta", "¿Qué te gustaría cambiar?")

            await enviar_mensaje_whatsapp(
                sender,
                f"🔍 *Espacio detectado:* {tipo_espacio}\n\n"
                f"💡 {pregunta}",
                settings
            )

            url_generada = await generar_imagen_remodelada(imagen_bytes, "moderno")

            await enviar_imagen_whatsapp(
                sender,
                url_generada,
                f"✨ ¡Así podría quedar tu {tipo_espacio} remodelado!\n"
                "Diseño moderno con acabados premium 🏠",
                settings
            )

            await enviar_botones_whatsapp(
                sender,
                "¿Qué deseas hacer ahora?",
                [
                    {"id": "btn_remodelar", "title": "🔄 Otro estilo"},
                    {"id": "btn_asesor",    "title": "👨‍💼 Asesor"},
                ],
                settings
            )

    except Exception as e:
        logger.error(f"Error en background: {type(e).__name__} - {e}")
        await enviar_mensaje_whatsapp(
            sender,
            "😅 Hubo un error procesando tu imagen. Por favor intenta de nuevo.",
            settings
        )
        await enviar_menu_principal(sender, settings)


@router.get("")
async def verify_webhook(request: Request, settings: Settings = Depends(get_settings)):
    mode      = request.query_params.get("hub.mode")
    token     = request.query_params.get("hub.verify_token")
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
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        if "messages" not in value:
            return {"status": "ok"}

        message    = value["messages"][0]
        message_id = message.get("id", "")

        if await mensaje_ya_procesado(message_id):
            logger.info(f"Mensaje {message_id} ya procesado, ignorando")
            return {"status": "ok"}

        await marcar_mensaje_procesado(message_id)

        sender   = message["from"]
        msg_type = message["type"]

        # ── BOTONES INTERACTIVOS ───────────────────────────────────────────
        if msg_type == "interactive":
            interactive = message["interactive"]
            if interactive["type"] == "button_reply":
                button_id = interactive["button_reply"]["id"]
                logger.info(f"Botón presionado: {button_id} por {sender}")

                if button_id == "btn_remodelar":
                    estado_usuarios[sender] = {"modo": "remodelar"}
                    await enviar_mensaje_whatsapp(
                        sender,
                        "🏠 ¡Perfecto! Estoy listo para transformar tu espacio.\n\n"
                        "📸 *Envíame una foto* del espacio que deseas remodelar\n"
                        "(sala, habitación, cocina, baño, etc.)\n\n"
                        "La IA analizará tu espacio y te mostrará cómo podría quedar ✨",
                        settings
                    )

                elif button_id == "btn_plano":
                    estado_usuarios[sender] = {"modo": "plano"}
                    await enviar_mensaje_whatsapp(
                        sender,
                        "📐 ¡Perfecto! Voy a analizar tu plano con IA.\n\n"
                        "Puedes enviarme:\n"
                        "• 📄 Foto del plano impreso\n"
                        "• ✏️ Foto de tu boceto dibujado\n"
                        "• 📱 Captura de pantalla del plano\n\n"
                        "La IA detectará habitaciones, áreas y distribución\n"
                        "usando normas NTC colombianas 🇨🇴\n"
                        "y generará una *vista 3D* de tu espacio 🏗️✨",
                        settings
                    )

                elif button_id == "btn_asesor":
                    numero_asesor = settings.whatsapp_asesor_number
                    await enviar_mensaje_whatsapp(
                        sender,
                        f"👨‍💼 ¡Con gusto! Nuestro asesor experto te atenderá personalmente.\n\n"
                        f"📱 *Escríbele directamente aquí:*\n"
                        f"https://wa.me/{numero_asesor}\n\n"
                        f"⏰ Tiempo de respuesta: menos de 24 horas 🕐\n\n"
                        f"_También puedes enviarnos fotos de tu espacio para "
                        f"que el asesor prepare tu propuesta_ 🏠",
                        settings
                    )

        # ── MENSAJES DE TEXTO ──────────────────────────────────────────────
        elif msg_type == "text":
            text = message["text"]["body"].strip()
            logger.info(f"Texto de {sender}: {text[:50]}")

            # ── SEGURIDAD: Prompt Injection Prevention ─────────────────────
            # OWASP LLM01 — detectar intentos de manipular la IA
            analisis_seguridad = analizar_mensaje_texto(text)

            if analisis_seguridad.get("es_injection"):
                logger.warning(f"Prompt injection bloqueado de {sender}")
                await enviar_mensaje_whatsapp(
                    sender,
                    analisis_seguridad.get(
                        "respuesta_segura",
                        "Por favor envíame una foto de tu espacio 🏠 o selecciona una opción del menú."
                    ),
                    settings
                )
                return {"status": "ok"}

            # Texto limpio y validado
            text_lower = analisis_seguridad.get("mensaje_limpio", text).lower()

            if any(saludo in text_lower for saludo in [
                "hola", "buenas", "buenos", "hi", "hello",
                "inicio", "start", "menu", "menú", "comenzar"
            ]):
                await enviar_menu_principal(sender, settings)

            elif text_lower in [
                "clasico", "clásico", "minimalista",
                "rustico", "rústico", "industrial", "moderno"
            ]:
                await enviar_mensaje_whatsapp(
                    sender,
                    f"🎨 ¡Excelente elección! Procesando estilo *{text_lower}*...\n\n"
                    "📸 Envíame la foto de tu espacio y lo transformo. ✨",
                    settings
                )

            else:
                await enviar_menu_principal(sender, settings)

        # ── IMÁGENES ───────────────────────────────────────────────────────
        elif msg_type == "image":
            image_id = message["image"]["id"]
            logger.info(f"Imagen recibida de {sender}")
            background_tasks.add_task(
                procesar_imagen_background,
                sender,
                image_id,
                settings
            )

    except Exception as e:
        logger.error(f"Error procesando mensaje: {type(e).__name__} - {e}")

    return {"status": "ok"}