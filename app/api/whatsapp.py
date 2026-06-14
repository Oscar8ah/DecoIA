import logging
import asyncio
import httpx
from fastapi import APIRouter, Request, HTTPException, Depends, BackgroundTasks
from fastapi.responses import PlainTextResponse
from app.utils.config import get_settings, Settings
from app.services.imagen_service import (
    descargar_imagen_whatsapp,
    generar_imagen_remodelada,
    generar_imagen_con_producto,
    generar_vista_isometrica,
)
from app.services.openai_service import analizar_espacio_foto, analizar_plano, analizar_mensaje_texto
from app.utils.supabase_client import get_supabase
from datetime import datetime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])

mensajes_procesados_cache = set()
estado_usuarios = {}

# ── Tienda demo — slug fijo hasta que cada empresa configure la suya ──
TIENDA_DEMO_SLUG = "pisos-demo"
BASE_URL         = "https://decoiarte.com"
ASESOR_NUMERO   = "573116280351"   # tu número personal Oscar


# ── UTILIDADES TIEMPO ─────────────────────────────────────────────────────
def get_saludo() -> str:
    hora = datetime.now().hour
    if 5 <= hora < 12:   return "¡Buenos días"
    elif 12 <= hora < 18: return "¡Buenas tardes"
    else:                 return "¡Buenas noches"


# ── DEDUPLICACIÓN ─────────────────────────────────────────────────────────
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
        logger.error(f"Error verificando mensaje: {e}")
        return False


async def marcar_mensaje_procesado(message_id: str):
    mensajes_procesados_cache.add(message_id)
    try:
        supabase = get_supabase()
        supabase.table("mensajes_procesados").insert({"message_id": message_id}).execute()
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")


# ── ENVÍOS WHATSAPP ───────────────────────────────────────────────────────
async def enviar_mensaje_whatsapp(telefono: str, mensaje: str, settings: Settings):
    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": telefono, "type": "text", "text": {"body": mensaje}}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=data, headers=headers)
        logger.info(f"Texto enviado a {telefono}: {r.status_code}")


async def enviar_botones_whatsapp(telefono: str, mensaje: str, botones: list, settings: Settings):
    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp", "to": telefono, "type": "interactive",
        "interactive": {
            "type": "button", "body": {"text": mensaje},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in botones]}
        }
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=data, headers=headers)
        logger.info(f"Botones enviados a {telefono}: {r.status_code}")
        if r.status_code != 200:
            logger.error(f"Error botones: {r.text}")


async def enviar_imagen_whatsapp(telefono: str, url_imagen: str, caption: str, settings: Settings):
    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp", "to": telefono, "type": "image",
        "image": {"link": url_imagen, "caption": caption}
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=data, headers=headers)
        logger.info(f"Imagen enviada a {telefono}: {r.status_code}")


# ── MENÚ PRINCIPAL ────────────────────────────────────────────────────────
async def enviar_menu_principal(telefono: str, settings: Settings):
    saludo = get_saludo()
    await enviar_botones_whatsapp(
        telefono,
        f"{saludo}! 👋 Bienvenido a *DecoIArte* 🏠✨\n\n"
        f"Soy tu asistente de remodelación con Inteligencia Artificial.\n\n"
        f"¿Qué te gustaría hacer hoy?",
        [
            {"id": "btn_remodelar", "title": "🏠 Remodelar"},
            {"id": "btn_plano",    "title": "📐 Mi plano"},
            {"id": "btn_asesor",   "title": "👨‍💼 Asesor"},
        ],
        settings
    )


# ── OBTENER SLUG DE TIENDA SEGÚN EMPRESA ──────────────────────────────────
async def obtener_slug_tienda(empresa_id: str = None) -> str:
    """
    Retorna el slug de la tienda activa de la empresa.
    Si no tiene tienda configurada, usa la demo.
    """
    if not empresa_id:
        return TIENDA_DEMO_SLUG
    try:
        supabase = get_supabase()
        r = supabase.table("tiendas").select("slug").eq("empresa_id", empresa_id).eq("activa", True).maybeSingle().execute()
        return r.data["slug"] if r.data else TIENDA_DEMO_SLUG
    except Exception:
        return TIENDA_DEMO_SLUG


# ── NOTIFICAR AL ASESOR ───────────────────────────────────────────────────
async def notificar_asesor(
    cliente_tel: str,
    tipo_trabajo: str,          # "remodelación" | "plano"
    producto_nombre: str,
    producto_precio: int,
    producto_categoria: str,
    url_foto_original: str,
    url_foto_generada: str,
    settings: Settings
):
    """Envía resumen completo al asesor cuando un cliente elige un producto."""
    precio_fmt = f"${producto_precio:,}".replace(",", ".")

    mensaje = (
        f"🔔 *Nueva selección de cliente — DecoIArte*\n\n"
        f"📱 *Cliente:* +{cliente_tel}\n"
        f"🔧 *Trabajo:* {tipo_trabajo}\n\n"
        f"🛍️ *Producto elegido:*\n"
        f"   • Nombre: {producto_nombre}\n"
        f"   • Categoría: {producto_categoria}\n"
        f"   • Precio: {precio_fmt} / m²\n\n"
        f"🖼️ *Foto original:* {url_foto_original}\n"
        f"✨ *Foto remodelada:* {url_foto_generada}\n\n"
        f"⚡ El cliente ya recibió el resultado por WhatsApp."
    )

    await enviar_mensaje_whatsapp(ASESOR_NUMERO, mensaje, settings)
    logger.info(f"Asesor notificado sobre selección de {cliente_tel}")


# ── ESPERAR SELECCIÓN DEL CLIENTE EN EL SELECTOR ─────────────────────────
async def esperar_seleccion_y_procesar(
    sender: str,
    session_id: str,
    url_foto_original: str,
    url_foto_generada: str,
    tipo_trabajo: str,
    settings: Settings,
    timeout_seg: int = 300   # 5 minutos de espera
):
    """
    Polling cada 5 segundos durante timeout_seg.
    Cuando el cliente elige un producto en el selector,
    lo aplica a la foto y notifica al asesor.
    """
    supabase  = get_supabase()
    intervalo = 5
    intentos  = timeout_seg // intervalo

    logger.info(f"Esperando selección para session_id={session_id}")

    for _ in range(intentos):
        await asyncio.sleep(intervalo)
        try:
            r = supabase.table("selecciones_producto") \
                .select("*") \
                .eq("session_id", session_id) \
                .eq("procesada", False) \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()

            if not r.data:
                continue

            seleccion = r.data[0]
            logger.info(f"Selección encontrada: {seleccion['nombre']}")

            # Marcar como procesada inmediatamente para evitar doble proceso
            supabase.table("selecciones_producto") \
                .update({"procesada": True}) \
                .eq("id", seleccion["id"]) \
                .execute()

            # Confirmar al cliente que recibimos su elección
            await enviar_mensaje_whatsapp(
                sender,
                f"✅ ¡Perfecto! Elegiste *{seleccion['nombre']}*\n\n"
                f"🤖 Aplicando el producto a tu foto...\n"
                f"Esto toma unos segundos ⏳",
                settings
            )

            # Descargar foto original y aplicar el producto
            url_resultado = url_foto_generada  # fallback si falla la aplicación
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    foto_r = await client.get(url_foto_original)
                    foto_bytes = foto_r.content

                # Si el producto tiene imagen, aplicarlo; si no, usar la foto genérica
                if seleccion.get("imagen_url"):
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        prod_r = await client.get(seleccion["imagen_url"])
                        prod_bytes = prod_r.content

                    url_resultado = await generar_imagen_con_producto(
                        foto_bytes,
                        prod_bytes,
                        seleccion["nombre"],
                        seleccion.get("categoria", "material"),
                    )
                else:
                    url_resultado = url_foto_generada

            except Exception as e:
                logger.error(f"Error aplicando producto: {e}")
                url_resultado = url_foto_generada

            # Enviar resultado al cliente
            await enviar_imagen_whatsapp(
                sender,
                url_resultado,
                f"✨ ¡Así quedaría tu espacio con *{seleccion['nombre']}*!\n\n"
                f"💰 Precio: ${seleccion.get('precio', '')} / m²\n"
                f"👨‍💼 Nuestro asesor te contactará pronto con la cotización completa 🏠",
                settings
            )

            await enviar_botones_whatsapp(
                sender,
                "¿Qué deseas hacer ahora?",
                [
                    {"id": "btn_remodelar", "title": "🔄 Nuevo espacio"},
                    {"id": "btn_asesor",    "title": "👨‍💼 Hablar asesor"},
                ],
                settings
            )

            # Notificar al asesor
            await notificar_asesor(
                cliente_tel      = sender,
                tipo_trabajo     = tipo_trabajo,
                producto_nombre  = seleccion["nombre"],
                producto_precio  = seleccion.get("precio", 0),
                producto_categoria = seleccion.get("categoria", ""),
                url_foto_original  = url_foto_original,
                url_foto_generada  = url_resultado,
                settings           = settings,
            )

            # Limpiar estado
            if sender in estado_usuarios:
                del estado_usuarios[sender]

            return  # éxito, salir del loop

        except Exception as e:
            logger.error(f"Error en polling selección: {e}")
            continue

    # Timeout — el cliente no eligió nada en 5 minutos
    logger.info(f"Timeout esperando selección para {sender}")
    await enviar_mensaje_whatsapp(
        sender,
        "⏰ El tiempo para elegir el producto expiró.\n\n"
        "Puedes volver a enviar una foto cuando quieras 🏠",
        settings
    )
    await enviar_menu_principal(sender, settings)


# ── PROCESAR IMAGEN (background task) ────────────────────────────────────
async def procesar_imagen_background(sender: str, image_id: str, settings: Settings):
    try:
        imagen_bytes = await descargar_imagen_whatsapp(image_id, settings.whatsapp_token)
        modo         = estado_usuarios.get(sender, {}).get("modo", "remodelar")

        # ── MODO PLANO ────────────────────────────────────────────────────
        if modo == "plano":
            await enviar_mensaje_whatsapp(
                sender,
                "📐 ¡Recibí tu plano! Analizando distribución con IA\n"
                "y normas NTC colombianas... ⏳🤖",
                settings
            )

            resultado = analizar_plano(imagen_bytes)

            if not resultado.get("es_plano", True):
                await enviar_mensaje_whatsapp(
                    sender,
                    "🤔 La imagen no parece un plano arquitectónico.\n\n"
                    "Puedes enviar:\n"
                    "• Foto del plano impreso 📄\n"
                    "• Foto de tu boceto ✏️\n"
                    "• Captura de pantalla del plano 📱",
                    settings
                )
                await enviar_menu_principal(sender, settings)
                return

            habitaciones = resultado.get("habitaciones", "")
            area         = resultado.get("area_estimada", "Por determinar")
            tipo         = resultado.get("tipo_plano", "espacio")
            distribucion = resultado.get("distribucion", "")

            await enviar_mensaje_whatsapp(
                sender,
                f"📐 *Plano analizado con IA* ✅\n\n"
                f"🏠 *Tipo:* {tipo}\n"
                f"📋 *Habitaciones:* {habitaciones}\n"
                f"📏 *Área estimada:* {area}\n"
                f"🗺️ *Distribución:* {distribucion}",
                settings
            )

            await enviar_mensaje_whatsapp(
                sender,
                "🎨 Generando vista 3D isométrica...\n"
                "Esto toma unos segundos ⏳✨",
                settings
            )

            url_isometrica = await generar_vista_isometrica(imagen_bytes, resultado)

            await enviar_imagen_whatsapp(
                sender,
                url_isometrica,
                f"🏠 *Vista 3D de tu {tipo}* ✨\n"
                f"📏 Área: {area} · Acabados modernos aplicados",
                settings
            )

            # Guardar estado con la foto generada
            session_id = f"{sender}_{int(datetime.now().timestamp())}"
            estado_usuarios[sender] = {
                "modo":             "plano_analizado",
                "session_id":       session_id,
                "url_foto_original": url_isometrica,
                "url_foto_generada": url_isometrica,
                "tipo_trabajo":     f"análisis de plano — {tipo}",
                "plano_info":       resultado,
            }

            # Obtener slug de la tienda
            slug = await obtener_slug_tienda()
            url_selector = f"{BASE_URL}/remodelar"

            await enviar_botones_whatsapp(
                sender,
                "💡 *¿Qué quieres hacer con tu plano?*\n\n"
                f"🛍️ Elige los materiales reales de nuestra tienda\n"
                f"o habla directamente con un asesor:",
                [
                    {"id": "btn_ver_productos", "title": "🛍️ Ver productos"},
                    {"id": "btn_asesor",        "title": "👨‍💼 Hablar asesor"},
                ],
                settings
            )

            # Guardar URL del selector en el estado
            estado_usuarios[sender]["url_selector"] = url_selector

        # ── MODO REMODELAR ────────────────────────────────────────────────
        else:
            await enviar_mensaje_whatsapp(
                sender,
                "📸 ¡Recibí tu foto! Analizando tu espacio con IA...\n"
                "Esto toma unos segundos ⏳🤖",
                settings
            )

            analisis     = analizar_espacio_foto(imagen_bytes)
            tipo_espacio = analisis.get("tipo_espacio", "espacio")

            await enviar_mensaje_whatsapp(
                sender,
                f"🔍 *Espacio detectado:* {tipo_espacio}\n\n"
                f"✨ Generando remodelación con IA...",
                settings
            )

            url_generada = await generar_imagen_remodelada(imagen_bytes, "moderno")

            # Subir foto original a imgbb para tenerla disponible
            from app.services.imagen_service import subir_imagen_a_imgbb
            settings_obj = get_settings()
            url_original = await subir_imagen_a_imgbb(imagen_bytes, settings_obj.imgbb_api_key)

            await enviar_imagen_whatsapp(
                sender,
                url_generada,
                f"✨ ¡Así podría quedar tu {tipo_espacio}!\n"
                f"Diseño moderno con acabados premium 🏠",
                settings
            )

            # Generar session_id y URL del selector
            session_id = f"{sender}_{int(datetime.now().timestamp())}"
            slug       = await obtener_slug_tienda()
            url_selector = f"{BASE_URL}/remodelar"

            estado_usuarios[sender] = {
                "modo":              "remodelado",
                "session_id":        session_id,
                "url_foto_original": url_original,
                "url_foto_generada": url_generada,
                "tipo_trabajo":      f"remodelación de {tipo_espacio}",
                "url_selector":      url_selector,
            }

            await enviar_botones_whatsapp(
                sender,
                "🎉 ¡Tu espacio transformado!\n\n"
                "¿Qué quieres hacer ahora?\n\n"
                f"🛍️ *Ver productos* — elige materiales reales\n"
                f"de nuestra tienda y los aplicamos a tu foto\n\n"
                f"👨‍💼 *Hablar con asesor* — cotización personalizada",
                [
                    {"id": "btn_ver_productos", "title": "🛍️ Ver productos"},
                    {"id": "btn_asesor",        "title": "👨‍💼 Hablar asesor"},
                ],
                settings
            )

    except Exception as e:
        logger.error(f"Error procesando imagen: {type(e).__name__} - {e}")
        await enviar_mensaje_whatsapp(
            sender,
            "😅 Hubo un error procesando tu imagen.\n"
            "Por favor intenta de nuevo 🙏",
            settings
        )
        await enviar_menu_principal(sender, settings)


# ── WEBHOOK VERIFICACIÓN ──────────────────────────────────────────────────
@router.get("")
async def verify_webhook(request: Request, settings: Settings = Depends(get_settings)):
    mode      = request.query_params.get("hub.mode")
    token     = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        logger.info("Webhook verificado")
        return PlainTextResponse(content=challenge)
    raise HTTPException(status_code=403, detail="Token inválido")


# ── WEBHOOK RECEPCIÓN ─────────────────────────────────────────────────────
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
            logger.info(f"Mensaje {message_id} ya procesado")
            return {"status": "ok"}

        await marcar_mensaje_procesado(message_id)

        sender   = message["from"]
        msg_type = message["type"]

        # ── BOTONES INTERACTIVOS ──────────────────────────────────────────
        if msg_type == "interactive":
            interactive = message["interactive"]
            if interactive["type"] == "button_reply":
                button_id = interactive["button_reply"]["id"]
                logger.info(f"Botón: {button_id} de {sender}")

                # ── REMODELAR ─────────────────────────────────────────────
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

                # ── PLANO ─────────────────────────────────────────────────
                elif button_id == "btn_plano":
                    estado_usuarios[sender] = {"modo": "plano"}
                    await enviar_mensaje_whatsapp(
                        sender,
                        "📐 ¡Perfecto! Voy a analizar tu plano con IA.\n\n"
                        "Puedes enviarme:\n"
                        "• 📄 Foto del plano impreso\n"
                        "• ✏️ Foto de tu boceto\n"
                        "• 📱 Captura de pantalla\n\n"
                        "La IA detectará habitaciones, áreas y distribución\n"
                        "con normas NTC colombianas 🇨🇴\n"
                        "y generará una *vista 3D isométrica* ✨",
                        settings
                    )

                # ── VER PRODUCTOS DE LA TIENDA ────────────────────────────
                elif button_id == "btn_ver_productos":
                    user_state = estado_usuarios.get(sender, {})
                    url_selector = user_state.get("url_selector")
                    session_id   = user_state.get("session_id")

                    if not url_selector:
                        # No hay estado previo — pedir foto primero
                        await enviar_mensaje_whatsapp(
                            sender,
                            "📸 Primero envíame una foto de tu espacio\n"
                            "para poder mostrarte los productos aplicados 🏠",
                            settings
                        )
                        await enviar_menu_principal(sender, settings)
                        return {"status": "ok"}

                    await enviar_mensaje_whatsapp(
                        sender,
                        f"🛍️ *Elige el producto que más te gusta*\n\n"
                        f"Toca el link para ver el catálogo completo\n"
                        f"y selecciona el material que quieres aplicar\n"
                        f"a tu foto:\n\n"
                        f"👉 {url_selector}\n\n"
                        f"_Una vez elijas, la IA lo aplicará automáticamente_ ✨",
                        settings
                    )

                    # Iniciar polling en background
                    background_tasks.add_task(
                        esperar_seleccion_y_procesar,
                        sender,
                        session_id,
                        user_state.get("url_foto_original", ""),
                        user_state.get("url_foto_generada", ""),
                        user_state.get("tipo_trabajo", "remodelación"),
                        settings,
                    )

                # ── ASESOR ────────────────────────────────────────────────
                elif button_id == "btn_asesor":
                    user_state = estado_usuarios.get(sender, {})

                    # Si tiene trabajo previo, incluirlo en el mensaje al asesor
                    if user_state.get("url_foto_generada"):
                        await notificar_asesor(
                            cliente_tel        = sender,
                            tipo_trabajo       = user_state.get("tipo_trabajo", "consulta"),
                            producto_nombre    = "Sin producto elegido — contacto directo",
                            producto_precio    = 0,
                            producto_categoria = "",
                            url_foto_original  = user_state.get("url_foto_original", ""),
                            url_foto_generada  = user_state.get("url_foto_generada", ""),
                            settings           = settings,
                        )

                    await enviar_mensaje_whatsapp(
                        sender,
                        f"👨‍💼 ¡Con gusto! Nuestro asesor te atenderá personalmente.\n\n"
                        f"📱 *Escríbele directamente:*\n"
                        f"https://wa.me/{ASESOR_NUMERO}\n\n"
                        f"⏰ Respuesta en menos de 24 horas 🕐\n\n"
                        f"_También puedes enviarnos fotos de tu espacio\n"
                        f"para que prepare tu propuesta_ 🏠",
                        settings
                    )

        # ── MENSAJES DE TEXTO ─────────────────────────────────────────────
        elif msg_type == "text":
            text = message["text"]["body"].strip()
            logger.info(f"Texto de {sender}: {text[:50]}")

            analisis_seguridad = analizar_mensaje_texto(text)

            if analisis_seguridad.get("es_injection"):
                logger.warning(f"Prompt injection bloqueado de {sender}")
                await enviar_mensaje_whatsapp(
                    sender,
                    analisis_seguridad.get(
                        "respuesta_segura",
                        "Por favor envíame una foto de tu espacio 🏠"
                    ),
                    settings
                )
                return {"status": "ok"}

            text_lower = analisis_seguridad.get("mensaje_limpio", text).lower()

            if any(s in text_lower for s in [
                "hola", "buenas", "buenos", "hi", "hello",
                "inicio", "start", "menu", "menú", "comenzar"
            ]):
                await enviar_menu_principal(sender, settings)
            else:
                await enviar_menu_principal(sender, settings)

        # ── IMÁGENES ──────────────────────────────────────────────────────
        elif msg_type == "image":
            image_id = message["image"]["id"]
            logger.info(f"Imagen de {sender}")
            background_tasks.add_task(
                procesar_imagen_background,
                sender, image_id, settings
            )

    except Exception as e:
        logger.error(f"Error procesando mensaje: {type(e).__name__} - {e}")

    return {"status": "ok"}