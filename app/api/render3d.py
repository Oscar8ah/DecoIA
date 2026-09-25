import logging
import base64
import io
import time
import httpx
from collections import defaultdict, deque
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from openai import OpenAI
from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase
from app.services.limites_service import tiene_fotos_disponibles, descontar_foto
from app.api.compras import usuario_de_sesion, guardar_para_venta
from app.utils.auth import exigir_duenio, ADMIN_EMAIL

logger = logging.getLogger(__name__)
router = APIRouter(tags=["render3d"])

# ── Límite de uso por IP ──────────────────────────────────────────────────
# Este endpoint llama a gpt-image-1, que cuesta dinero real por cada llamada.
# Antes no tenía ningún límite: cualquiera podía llamarlo en bucle.
_peticiones_por_ip: dict = defaultdict(deque)
LIMITE_PETICIONES = 20
VENTANA_SEGUNDOS  = 3600


def _verificar_limite_ip(request: Request):
    ip = request.client.host if request.client else "desconocido"
    ahora = time.time()
    historial = _peticiones_por_ip[ip]
    while historial and ahora - historial[0] > VENTANA_SEGUNDOS:
        historial.popleft()
    if len(historial) >= LIMITE_PETICIONES:
        raise HTTPException(status_code=429, detail="Demasiados renders desde esta conexión. Intenta de nuevo más tarde.")
    historial.append(ahora)


class RenderRequest(BaseModel):
    imagen_base64:        str
    prompt:               str
    empresa_id:            Optional[str] = None
    producto_imagen_url:   Optional[str] = None   # foto real del producto elegido (piso, mueble, etc.)
    categoria_producto:    Optional[str] = None    # "muebles", "pisos", "enchapes", "pintura", ...
    producto_nombre:       Optional[str] = None
    # Tienda dueña del producto que se está probando. La usa un cliente
    # que no es empresa (visitante o comprador) para que la generación se
    # le cargue a esa tienda: para ella es un cliente potencial, que es
    # exactamente lo que compra con el plan Básico.
    tienda_id:             Optional[str] = None


@router.post("/generar-render-3d")
async def generar_render_3d(data: RenderRequest, request: Request):
    """
    Recibe captura del visor 3D + prompt.
    Genera render fotorrealista con gpt-image-1.
    Guarda en Supabase Storage y retorna URL pública.
    """
    _verificar_limite_ip(request)
    settings = get_settings()

    # ── Revisar cupo de fotos del plan ANTES de gastar en la IA ───────────
    # OJO: antes, si la petición llegaba SIN empresa_id, este bloque se
    # saltaba entero y el render se generaba gratis sin descontarle a nadie.
    # Ahora la empresa es obligatoria: sin ella no se gasta dinero de la IA.
    # Un visitante o comprador no tiene empresa. Antes eso rechazaba la
    # petición entera y NINGÚN visitante podía generar en /remodelar — ni el
    # que llegaba del home, ni el cliente de WhatsApp que tocaba "probar
    # otros materiales". Ahora se le carga a la tienda del producto. La
    # empresa se busca en la base con la clave de servicio: nunca se confía
    # en una empresa que mande el navegador para este caso.
    # Es un comprador (no una empresa) generando con productos de una tienda.
    # Comprar exige sesión: se identifica ANTES de gastar en la IA.
    es_comprador = not data.empresa_id and bool(data.tienda_id)
    comprador = await usuario_de_sesion(request) if es_comprador else None

    # Es una empresa usando SU cupo (visor 3D, /remodelar del asesor). Antes se
    # creía el empresa_id del navegador, y ese id es público: cualquiera podía
    # gastarle las fotos a otra tienda. Ahora: sesión + dueño + plan pagado.
    if data.empresa_id:
        email = await exigir_duenio(request, data.empresa_id)
        if email != ADMIN_EMAIL:
            try:
                re_ = get_supabase().table("empresas").select("estado") \
                    .eq("id", data.empresa_id).maybe_single().execute()
                estado = ((re_.data or {}).get("estado") if re_ else None) or ""
            except Exception as e:
                logger.error(f"No se pudo leer el estado de {data.empresa_id}: {e}")
                raise HTTPException(status_code=503, detail="No se pudo verificar tu plan, intenta de nuevo")
            if estado != "activo":
                return {"status": "error", "error": "plan_inactivo",
                        "mensaje": "Tu plan no está activo. Completa el pago en el dashboard para generar imágenes."}

    if not data.empresa_id and data.tienda_id:
        try:
            rt = get_supabase().table("tiendas").select("empresa_id") \
                .eq("id", data.tienda_id).eq("activa", True).maybe_single().execute()
            if rt and rt.data and rt.data.get("empresa_id"):
                data.empresa_id = rt.data["empresa_id"]
                logger.info(f"Render de visitante cargado a la empresa de la tienda {data.tienda_id}")
        except Exception as e:
            logger.warning(f"No se pudo resolver la empresa de la tienda {data.tienda_id}: {e}")

    if not data.empresa_id:
        logger.warning("Render 3D rechazado: petición sin empresa_id")
        return {"status": "error", "error": "empresa_requerida",
                "mensaje": "No se pudo identificar tu empresa. Vuelve a iniciar sesión e inténtalo de nuevo."}

    if not await tiene_fotos_disponibles(data.empresa_id):
        logger.warning(f"Empresa {data.empresa_id} sin fotos disponibles — render bloqueado")
        return {"status": "error", "error": "sin_fotos_disponibles",
                "mensaje": "Ya usaste todas las fotos incluidas en tu plan este mes. Actualiza tu plan para seguir generando renders."}

    try:
        client = OpenAI(api_key=settings.openai_api_key)

        # Decodificar imagen base64
        imagen_bytes = base64.b64decode(data.imagen_base64)

        logger.info(f"Generando render 3D para empresa: {data.empresa_id}")

        # ── FIX: images.edit requiere un objeto tipo file con nombre y tipo MIME ──
        # Envolver bytes en BytesIO con nombre para que la librería lo procese bien
        imagen_file = io.BytesIO(imagen_bytes)
        imagen_file.name = "render_3d.png"          # atributo name necesario

        prompt_final   = data.prompt
        imagenes_envio = imagen_file

        # ── Si viene una foto real de producto, mandarla también a la IA ──
        # (antes solo se describía el producto en texto, la IA nunca lo veía)
        if data.producto_imagen_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as http_client:
                    resp_prod = await http_client.get(data.producto_imagen_url)
                    resp_prod.raise_for_status()
                producto_file = io.BytesIO(resp_prod.content)
                producto_file.name = "producto_referencia.png"

                if data.categoria_producto == "muebles":
                    nombre_prod = data.producto_nombre or "el mueble de referencia"
                    prompt_final = (
                        f"Interior design photo edit. This is a photo of a room. "
                        f"STEP 1: Remove ALL existing furniture and decor objects currently in the room "
                        f"(sofas, chairs, tables, beds, shelves, lamps, rugs, curtains, decorative objects) — "
                        f"leave the room completely empty of furniture. "
                        f"STEP 2: Add this exact furniture piece, matching its design, color, material and "
                        f"proportions EXACTLY as shown in the second reference image: \"{nombre_prod}\". "
                        f"Place it in a natural, realistic position appropriate for the room's scale and use. "
                        f"Keep the room's architecture EXACTLY unchanged: same walls, same wall color, same "
                        f"floor material, same windows, same doors, same ceiling, same camera angle and lighting. "
                        f"Photorealistic result, professional real estate photography, no text, no watermarks."
                    )
                else:
                    # Materiales (piso/enchape/pintura): usar el prompt que ya arma el frontend,
                    # pero con la foto real del producto como segunda referencia.
                    prompt_final = data.prompt + " Match the exact material/color shown in the second reference image."

                imagenes_envio = [imagen_file, producto_file]
            except Exception as e:
                logger.warning(f"No se pudo descargar la imagen del producto ({data.producto_imagen_url}): {e} — se sigue solo con texto")

        response = client.images.edit(
            model  = "gpt-image-1",
            image  = imagenes_envio,                # BytesIO único, o lista [cuarto, producto]
            prompt = prompt_final,
            size   = "1024x1024",
        )

        # Obtener imagen generada (b64_json)
        imagen_generada_b64   = response.data[0].b64_json
        imagen_generada_bytes = base64.b64decode(imagen_generada_b64)

        # ── Comprador: la imagen limpia NO se publica ──
        # Antes se subía a una carpeta pública y se devolvía su dirección: el
        # candado era solo un dibujo en la pantalla. Ahora va a un bucket
        # privado y se entrega una vista previa con marca de agua; la limpia
        # se descarga solo cuando Wompi confirma el pago.
        if es_comprador:
            venta = await guardar_para_venta(imagen_generada_bytes, comprador,
                                             data.empresa_id, data.tienda_id)
            try:
                get_supabase().table("imagenes").insert({
                    "empresa_id":   data.empresa_id,
                    "url_generada": venta["url_preview"],
                    "tipo_espacio": "remodelar_web",
                    "estilo":       "render_ia",
                    "origen":       "web",
                }).execute()
            except Exception as e:
                logger.warning(f"No se pudo registrar la imagen en la tienda: {e}")
            await descontar_foto(data.empresa_id)
            logger.info(f"Render de comprador {comprador['email']} → vista previa {venta['referencia']}")
            return {"url_imagen": venta["url_preview"], "status": "ok",
                    "compra": {"id": venta["id"], "referencia": venta["referencia"], "monto": venta["monto"]}}

        # ── Subir a Supabase Storage ──
        supabase   = get_supabase()
        timestamp  = time.time_ns()
        empresa_id = data.empresa_id or "sin_empresa"
        ruta       = f"{empresa_id}/render_{timestamp}.png"

        supabase.storage.from_("portafolio").upload(
            ruta,
            imagen_generada_bytes,
            {"content-type": "image/png", "upsert": "true"}
        )

        # URL pública
        url_publica = supabase.storage.from_("portafolio").get_public_url(ruta)

        # Guardar en tabla imagenes
        if data.empresa_id:
            supabase.table("imagenes").insert({
                "empresa_id":   data.empresa_id,
                "url_generada": url_publica,
                "tipo_espacio": "visor_3d",
                "estilo":       "render_ia",
            }).execute()
            await descontar_foto(data.empresa_id)

        logger.info(f"Render generado: {url_publica}")
        return {"url_imagen": url_publica, "status": "ok"}

    except Exception as e:
        logger.error(f"Error generando render 3D: {type(e).__name__} — {e}")
        return {"error": str(e), "status": "error"}