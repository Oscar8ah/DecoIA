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
from app.services.openai_service import (
    analizar_espacio_foto,
    analizar_plano,
    analizar_plano_completo,
    analizar_mensaje_texto,
)
from app.utils.supabase_client import get_supabase
from datetime import datetime
import json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["whatsapp"])

mensajes_procesados_cache = set()
estado_usuarios = {}

TIENDA_DEMO_SLUG = "pisos-demo"
BASE_URL         = "https://decoiarte.com"
ASESOR_NUMERO    = "573116280351"


# ── UTILIDADES ────────────────────────────────────────────────────────────
def get_saludo() -> str:
    hora = datetime.now().hour
    if 5 <= hora < 12:    return "¡Buenos días"
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
async def enviar_mensaje_whatsapp(telefono: str, mensaje: str, settings: Settings, phone_number_id: str = None):
    pid = phone_number_id or settings.whatsapp_phone_number_id
    url = f"https://graph.facebook.com/v25.0/{pid}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": telefono, "type": "text", "text": {"body": mensaje}}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=data, headers=headers)
        logger.info(f"Texto enviado a {telefono}: {r.status_code}")


async def enviar_botones_whatsapp(telefono: str, mensaje: str, botones: list, settings: Settings, phone_number_id: str = None):
    pid = phone_number_id or settings.whatsapp_phone_number_id
    url = f"https://graph.facebook.com/v25.0/{pid}/messages"
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


async def enviar_imagen_whatsapp(telefono: str, url_imagen: str, caption: str, settings: Settings, phone_number_id: str = None):
    pid = phone_number_id or settings.whatsapp_phone_number_id
    url = f"https://graph.facebook.com/v25.0/{pid}/messages"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp", "to": telefono, "type": "image",
        "image": {"link": url_imagen, "caption": caption}
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=data, headers=headers)
        logger.info(f"Imagen enviada a {telefono}: {r.status_code}")


# ── MENÚ PRINCIPAL ────────────────────────────────────────────────────────
async def enviar_menu_principal(telefono: str, settings: Settings, phone_number_id: str = None):
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
        settings,
        phone_number_id,
    )


async def obtener_slug_tienda(empresa_id: str = None) -> str:
    if not empresa_id:
        return TIENDA_DEMO_SLUG
    try:
        supabase = get_supabase()
        r = supabase.table("tiendas").select("slug").eq("empresa_id", empresa_id).eq("activa", True).maybeSingle().execute()
        return r.data["slug"] if r.data else TIENDA_DEMO_SLUG
    except Exception:
        return TIENDA_DEMO_SLUG


# ── MULTIASESOR ───────────────────────────────────────────────────────────
async def resolver_empresa_por_phone_id(phone_number_id: str):
    """
    Busca a qué empresa pertenece el número de WhatsApp que recibió el mensaje
    (metadata.phone_number_id del webhook). Si no coincide con ninguna empresa
    registrada (ej: es el número demo/de pruebas), devuelve None y todo el
    flujo sigue funcionando igual que hoy, con el comportamiento genérico.
    """
    if not phone_number_id:
        return None
    try:
        supabase = get_supabase()
        r = supabase.table("empresas") \
            .select("id, nombre, whatsapp_phone_number_id, whatsapp_numero_solicitado, tiendas(id, slug, nombre)") \
            .eq("whatsapp_phone_number_id", phone_number_id).maybeSingle().execute()
        return r.data
    except Exception as e:
        logger.error(f"Error resolviendo empresa por phone_number_id {phone_number_id}: {e}")
        return None


# ── NOTIFICAR ASESOR ──────────────────────────────────────────────────────
async def notificar_asesor(
    cliente_tel: str, tipo_trabajo: str,
    producto_nombre: str, producto_precio: int,
    producto_categoria: str, url_foto_original: str,
    url_foto_generada: str, settings: Settings,
    asesor_numero: str = None, phone_number_id: str = None,
):
    destino = asesor_numero or ASESOR_NUMERO
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
    await enviar_mensaje_whatsapp(destino, mensaje, settings, phone_number_id)
    logger.info(f"Asesor {destino} notificado sobre selección de {cliente_tel}")


# ── POLLING SELECCIÓN ─────────────────────────────────────────────────────
async def esperar_seleccion_y_procesar(
    sender: str, session_id: str,
    url_foto_original: str, url_foto_generada: str,
    tipo_trabajo: str, settings: Settings,
    pid_envio: str = None, asesor_numero: str = None,
    timeout_seg: int = 300
):
    supabase  = get_supabase()
    intervalo = 5
    intentos  = timeout_seg // intervalo

    logger.info(f"Esperando selección para session_id={session_id}")

    for _ in range(intentos):
        await asyncio.sleep(intervalo)
        try:
            r = supabase.table("selecciones_producto") \
                .select("*").eq("session_id", session_id) \
                .eq("procesada", False) \
                .order("created_at", desc=True).limit(1).execute()

            if not r.data:
                continue

            seleccion = r.data[0]
            logger.info(f"Selección encontrada: {seleccion['nombre']}")

            supabase.table("selecciones_producto") \
                .update({"procesada": True}).eq("id", seleccion["id"]).execute()

            await enviar_mensaje_whatsapp(
                sender,
                f"✅ ¡Perfecto! Elegiste *{seleccion['nombre']}*\n\n"
                f"🤖 Aplicando el producto a tu foto...\nEsto toma unos segundos ⏳",
                settings, pid_envio
            )

            url_resultado = url_foto_generada
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    foto_r     = await client.get(url_foto_original)
                    foto_bytes = foto_r.content

                if seleccion.get("imagen_url"):
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        prod_r     = await client.get(seleccion["imagen_url"])
                        prod_bytes = prod_r.content
                    url_resultado = await generar_imagen_con_producto(
                        foto_bytes, prod_bytes,
                        seleccion["nombre"], seleccion.get("categoria", "material"),
                    )
            except Exception as e:
                logger.error(f"Error aplicando producto: {e}")

            await enviar_imagen_whatsapp(
                sender, url_resultado,
                f"✨ ¡Así quedaría tu espacio con *{seleccion['nombre']}*!\n\n"
                f"💰 Precio: ${seleccion.get('precio', '')} / m²\n"
                f"👨‍💼 Nuestro asesor te contactará pronto 🏠",
                settings, pid_envio
            )

            await enviar_botones_whatsapp(
                sender, "¿Qué deseas hacer ahora?",
                [
                    {"id": "btn_remodelar", "title": "🔄 Nuevo espacio"},
                    {"id": "btn_asesor",    "title": "👨‍💼 Hablar asesor"},
                ],
                settings, pid_envio
            )

            await notificar_asesor(
                cliente_tel=sender, tipo_trabajo=tipo_trabajo,
                producto_nombre=seleccion["nombre"],
                producto_precio=seleccion.get("precio", 0),
                producto_categoria=seleccion.get("categoria", ""),
                url_foto_original=url_foto_original,
                url_foto_generada=url_resultado,
                settings=settings,
                asesor_numero=asesor_numero,
                phone_number_id=pid_envio,
            )

            if sender in estado_usuarios:
                del estado_usuarios[sender]
            return

        except Exception as e:
            logger.error(f"Error en polling selección: {e}")
            continue

    # ✅ Timeout — limpiar estado y NO mandar menú de bienvenida
    logger.info(f"Timeout esperando selección para {sender}")
    if sender in estado_usuarios:
        del estado_usuarios[sender]
    await enviar_mensaje_whatsapp(
        sender,
        "⏰ El tiempo para elegir el producto expiró.\n\n"
        "Puedes volver a enviar una foto cuando quieras 🏠\n"
        "o escribe *menú* para ver las opciones.",
        settings, pid_envio
    )


# ── PROCESAR IMAGEN (background task) ────────────────────────────────────
async def procesar_imagen_background(
    sender: str, image_id: str, settings: Settings,
    pid_envio: str = None, asesor_numero: str = None, url_selector_base: str = None,
    empresa_id: str = None,
):
    try:
        imagen_bytes = await descargar_imagen_whatsapp(image_id, settings.whatsapp_token)
        modo         = estado_usuarios.get(sender, {}).get("modo", "remodelar")

        # ── MODO PLANO ────────────────────────────────────────────────────
        if modo == "plano":
            await enviar_mensaje_whatsapp(
                sender,
                "📐 ¡Recibí tu plano! Analizando distribución con IA\n"
                "y normas NTC colombianas... ⏳🤖",
                settings, pid_envio
            )

            resultado_completo = analizar_plano_completo(imagen_bytes)
            resultado          = resultado_completo.get("info", {})
            modelo_3d          = resultado_completo.get("modelo_3d")

            if not resultado.get("es_plano", True):
                await enviar_mensaje_whatsapp(
                    sender,
                    "🤔 La imagen no parece un plano arquitectónico.\n\n"
                    "Puedes enviar:\n"
                    "• Foto del plano impreso 📄\n"
                    "• Foto de tu boceto ✏️\n"
                    "• Captura de pantalla del plano 📱",
                    settings, pid_envio
                )
                if sender in estado_usuarios:
                    del estado_usuarios[sender]
                await enviar_menu_principal(sender, settings, pid_envio)
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
                settings, pid_envio
            )

            if modelo_3d and modelo_3d.get("modulos"):
                num_modulos    = len(modelo_3d["modulos"])
                nombres_modulos = ", ".join(m["nombre"] for m in modelo_3d["modulos"])
                await enviar_mensaje_whatsapp(
                    sender,
                    f"🏗️ *Modelo 3D generado:* {num_modulos} espacios\n"
                    f"📦 {nombres_modulos}\n\n"
                    f"Generando vista isométrica... ✨",
                    settings, pid_envio
                )
            else:
                await enviar_mensaje_whatsapp(
                    sender,
                    "🎨 Generando vista 3D isométrica...\nEsto toma unos segundos ⏳✨",
                    settings, pid_envio
                )

            url_isometrica = await generar_vista_isometrica(imagen_bytes, resultado)

            await enviar_imagen_whatsapp(
                sender, url_isometrica,
                f"🏠 *Vista 3D de tu {tipo}* ✨\n"
                f"📏 Área: {area} · Acabados modernos aplicados",
                settings, pid_envio
            )

            session_id = f"{sender}_{int(datetime.now().timestamp())}"
            estado_usuarios[sender] = {
                "modo":              "plano_analizado",
                "session_id":        session_id,
                "url_foto_original": url_isometrica,
                "url_foto_generada": url_isometrica,
                "tipo_trabajo":      f"análisis de plano — {tipo}",
                "plano_info":        resultado,
                "modelo_3d":         modelo_3d,
                "pid_envio":         pid_envio,
                "asesor":            asesor_numero,
            }

            # Guardar modelo en Supabase y generar link al visor
            url_visor_3d = None
            if modelo_3d:
                try:
                    supabase  = get_supabase()
                    datos_modelo = {
                        "session_id":  session_id,
                        "telefono":    sender,
                        "modelo_json": json.dumps(modelo_3d),
                        "plano_info":  json.dumps(resultado),
                        "created_at":  datetime.now().isoformat(),
                    }
                    if empresa_id:
                        datos_modelo["empresa_id"] = empresa_id
                    r = supabase.table("modelos_3d_plano").insert(datos_modelo).execute()
                    if r.data:
                        modelo_id    = r.data[0]["id"]
                        url_visor_3d = f"{BASE_URL}/visor3d?plano={modelo_id}"
                        logger.info(f"Modelo 3D guardado: {modelo_id}")
                except Exception as e:
                    logger.error(f"Error guardando modelo 3D: {e}")

            estado_usuarios[sender]["url_selector"] = url_selector_base or f"{BASE_URL}/remodelar"

            if url_visor_3d:
                await enviar_mensaje_whatsapp(
                    sender,
                    f"🏗️ *¡Tu modelo 3D está listo!*\n\n"
                    f"👉 *Ver en visor 3D:*\n{url_visor_3d}\n\n"
                    f"Desde el visor puedes:\n"
                    f"• Rotar y explorar tu plano en 3D 🔄\n"
                    f"• Cambiar materiales y colores 🎨\n"
                    f"• Generar render con IA 🤖",
                    settings, pid_envio
                )

            await enviar_botones_whatsapp(
                sender,
                "💡 *¿Qué quieres hacer con tu plano?*",
                [
                    {"id": "btn_ver_productos", "title": "🛍️ Ver productos"},
                    {"id": "btn_asesor",        "title": "👨‍💼 Hablar asesor"},
                ],
                settings, pid_envio
            )

        # ── MODO REMODELAR ────────────────────────────────────────────────
        else:
            await enviar_mensaje_whatsapp(
                sender,
                "📸 ¡Recibí tu foto! Analizando tu espacio con IA...\n"
                "Esto toma unos segundos ⏳🤖",
                settings, pid_envio
            )

            analisis     = analizar_espacio_foto(imagen_bytes)
            tipo_espacio = analisis.get("tipo_espacio", "espacio")

            await enviar_mensaje_whatsapp(
                sender,
                f"🔍 *Espacio detectado:* {tipo_espacio}\n\n"
                f"✨ Generando remodelación con IA...",
                settings, pid_envio
            )

            url_generada = await generar_imagen_remodelada(imagen_bytes, "moderno")

            from app.services.imagen_service import subir_imagen_a_imgbb
            settings_obj = get_settings()
            url_original = await subir_imagen_a_imgbb(imagen_bytes, settings_obj.imgbb_api_key)

            await enviar_imagen_whatsapp(
                sender, url_generada,
                f"✨ ¡Así podría quedar tu {tipo_espacio}!\n"
                f"Diseño moderno con acabados premium 🏠",
                settings, pid_envio
            )

            session_id   = f"{sender}_{int(datetime.now().timestamp())}"
            url_selector = url_selector_base or f"{BASE_URL}/remodelar"

            estado_usuarios[sender] = {
                "modo":              "remodelado",
                "session_id":        session_id,
                "url_foto_original": url_original,
                "url_foto_generada": url_generada,
                "tipo_trabajo":      f"remodelación de {tipo_espacio}",
                "url_selector":      url_selector,
                "pid_envio":         pid_envio,
                "asesor":            asesor_numero,
            }

            await enviar_botones_whatsapp(
                sender,
                "🎉 ¡Tu espacio transformado!\n\n"
                "¿Qué quieres hacer ahora?\n\n"
                f"🛍️ *Ver productos* — elige materiales reales\n"
                f"👨‍💼 *Hablar con asesor* — cotización personalizada",
                [
                    {"id": "btn_ver_productos", "title": "🛍️ Ver productos"},
                    {"id": "btn_asesor",        "title": "👨‍💼 Hablar asesor"},
                ],
                settings, pid_envio
            )

    except Exception as e:
        logger.error(f"Error procesando imagen: {type(e).__name__} - {e}")
        if sender in estado_usuarios:
            del estado_usuarios[sender]
        await enviar_mensaje_whatsapp(
            sender,
            "😅 Hubo un error procesando tu imagen.\n"
            "Por favor intenta de nuevo 🙏",
            settings, pid_envio
        )


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

        # ── MULTIASESOR: resolver a qué tienda pertenece este número ───────
        phone_number_id_recibido = value.get("metadata", {}).get("phone_number_id")
        empresa_ctx = await resolver_empresa_por_phone_id(phone_number_id_recibido)

        if empresa_ctx:
            tiendas_ctx  = empresa_ctx.get("tiendas")
            tienda_ctx   = (tiendas_ctx[0] if isinstance(tiendas_ctx, list) and tiendas_ctx else tiendas_ctx) or None
            pid_envio    = empresa_ctx.get("whatsapp_phone_number_id") or None
            asesor_ctx   = empresa_ctx.get("whatsapp_numero_solicitado") or ASESOR_NUMERO
            url_selector_base = f"{BASE_URL}/remodelar?tienda={tienda_ctx['id']}" if tienda_ctx else f"{BASE_URL}/remodelar"
            logger.info(f"Mensaje de {sender} identificado como cliente de la empresa '{empresa_ctx.get('nombre')}'")
        else:
            pid_envio    = None  # usa el número global/demo de settings
            asesor_ctx   = ASESOR_NUMERO
            url_selector_base = f"{BASE_URL}/remodelar"

        # ── BOTONES INTERACTIVOS ──────────────────────────────────────────
        if msg_type == "interactive":
            interactive = message["interactive"]
            if interactive["type"] == "button_reply":
                button_id = interactive["button_reply"]["id"]
                logger.info(f"Botón: {button_id} de {sender}")

                if button_id == "btn_remodelar":
                    estado_usuarios[sender] = {"modo": "remodelar", "pid_envio": pid_envio, "asesor": asesor_ctx, "url_selector_base": url_selector_base}
                    await enviar_mensaje_whatsapp(
                        sender,
                        "🏠 ¡Perfecto! Estoy listo para transformar tu espacio.\n\n"
                        "📸 *Envíame una foto* del espacio que deseas remodelar\n"
                        "(sala, habitación, cocina, baño, etc.)\n\n"
                        "La IA analizará tu espacio y te mostrará cómo podría quedar ✨",
                        settings, pid_envio
                    )

                elif button_id == "btn_plano":
                    estado_usuarios[sender] = {"modo": "plano", "pid_envio": pid_envio, "asesor": asesor_ctx, "url_selector_base": url_selector_base}
                    await enviar_mensaje_whatsapp(
                        sender,
                        "📐 ¡Perfecto! Voy a analizar tu plano con IA.\n\n"
                        "Puedes enviarme:\n"
                        "• 📄 Foto del plano impreso\n"
                        "• ✏️ Foto de tu boceto\n"
                        "• 📱 Captura de pantalla\n\n"
                        "La IA detectará habitaciones, áreas y distribución\n"
                        "con normas NTC colombianas 🇨🇴\n"
                        "y generará un *modelo 3D automático* ✨",
                        settings, pid_envio
                    )

                elif button_id == "btn_ver_productos":
                    user_state   = estado_usuarios.get(sender, {})
                    url_selector = user_state.get("url_selector")
                    session_id   = user_state.get("session_id")
                    pid_conv     = user_state.get("pid_envio", pid_envio)

                    if not url_selector:
                        await enviar_mensaje_whatsapp(
                            sender,
                            "📸 Primero envíame una foto de tu espacio\n"
                            "para poder mostrarte los productos aplicados 🏠",
                            settings, pid_conv
                        )
                        return {"status": "ok"}

                    await enviar_mensaje_whatsapp(
                        sender,
                        f"🛍️ *Elige el producto que más te gusta*\n\n"
                        f"Toca el link para ver el catálogo completo:\n\n"
                        f"👉 {url_selector}\n\n"
                        f"_Una vez elijas, la IA lo aplicará automáticamente_ ✨",
                        settings, pid_conv
                    )

                    background_tasks.add_task(
                        esperar_seleccion_y_procesar,
                        sender, session_id,
                        user_state.get("url_foto_original", ""),
                        user_state.get("url_foto_generada", ""),
                        user_state.get("tipo_trabajo", "remodelación"),
                        settings,
                        pid_conv,
                        user_state.get("asesor", asesor_ctx),
                    )

                elif button_id == "btn_asesor":
                    user_state = estado_usuarios.get(sender, {})
                    pid_conv   = user_state.get("pid_envio", pid_envio)
                    asesor_conv = user_state.get("asesor", asesor_ctx)
                    if user_state.get("url_foto_generada"):
                        await notificar_asesor(
                            cliente_tel=sender,
                            tipo_trabajo=user_state.get("tipo_trabajo", "consulta"),
                            producto_nombre="Sin producto elegido — contacto directo",
                            producto_precio=0,
                            producto_categoria="",
                            url_foto_original=user_state.get("url_foto_original", ""),
                            url_foto_generada=user_state.get("url_foto_generada", ""),
                            settings=settings,
                            asesor_numero=asesor_conv,
                            phone_number_id=pid_conv,
                        )

                    # Limpiar estado al ir con asesor
                    if sender in estado_usuarios:
                        del estado_usuarios[sender]

                    await enviar_mensaje_whatsapp(
                        sender,
                        f"👨‍💼 ¡Con gusto! Nuestro asesor te atenderá personalmente.\n\n"
                        f"📱 *Escríbele directamente:*\n"
                        f"https://wa.me/{asesor_conv}\n\n"
                        f"⏰ Respuesta en menos de 24 horas 🕐\n\n"
                        f"_También puedes enviarnos fotos de tu espacio\n"
                        f"para que prepare tu propuesta_ 🏠",
                        settings, pid_conv
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
                    analisis_seguridad.get("respuesta_segura", "Por favor envíame una foto de tu espacio 🏠"),
                    settings, pid_envio
                )
                return {"status": "ok"}

            text_lower = analisis_seguridad.get("mensaje_limpio", text).lower()

            # Palabras de saludo o menú — siempre mostrar menú
            if any(s in text_lower for s in [
                "hola", "buenas", "buenos", "hi", "hello",
                "inicio", "start", "menu", "menú", "comenzar"
            ]):
                # Limpiar estado al saludar de nuevo
                if sender in estado_usuarios:
                    del estado_usuarios[sender]
                await enviar_menu_principal(sender, settings, pid_envio)

            else:
                # ✅ FIX: solo mostrar menú si NO hay sesión activa
                user_state = estado_usuarios.get(sender, {})
                pid_conv   = user_state.get("pid_envio", pid_envio)
                if sender not in estado_usuarios:
                    await enviar_menu_principal(sender, settings, pid_envio)
                else:
                    await enviar_mensaje_whatsapp(
                        sender,
                        "¿En qué más te puedo ayudar? 😊\n"
                        "Envíame una foto o elige una opción.",
                        settings, pid_conv
                    )

        # ── IMÁGENES ──────────────────────────────────────────────────────
        elif msg_type == "image":
            image_id = message["image"]["id"]
            logger.info(f"Imagen de {sender}")
            user_state = estado_usuarios.get(sender, {})
            background_tasks.add_task(
                procesar_imagen_background,
                sender, image_id, settings,
                user_state.get("pid_envio", pid_envio),
                user_state.get("asesor", asesor_ctx),
                user_state.get("url_selector_base", url_selector_base),
                empresa_ctx.get("id") if empresa_ctx else None,
            )

    except Exception as e:
        logger.error(f"Error procesando mensaje: {type(e).__name__} - {e}")

    return {"status": "ok"}