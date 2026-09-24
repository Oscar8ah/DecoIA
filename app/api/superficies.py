"""
Refinado de superficies con IA.

IDEA CENTRAL — leer antes de tocar nada
────────────────────────────────────────
La IA NO produce la imagen final. El compuesto que hace el navegador con
cálculos (perspectiva, escala real de pieza, transferencia de luz, sombras de
contacto) es y sigue siendo la imagen que ve el cliente.

Lo único que se le pide a la IA es que diga cómo DEBERÍA estar iluminada esa
zona. El navegador toma esa respuesta, le extrae únicamente la luminancia
suavizada, y la aplica como un multiplicador sobre su propio compuesto.

Por qué así y no dejando que la IA entregue la imagen directa:

1. La baldosa tiene que ser LA baldosa. El cliente va a comprar ese producto y
   pagar por él. Un modelo generativo le cambia el tono, el veteado o el
   formato sin avisar, y eso pasa de ser un problema estético a un problema
   legal el día que alguien reciba material que no se parece al render.
2. Los muebles y el cuarto son del cliente. Un modelo de edición mueve una
   lámpara, endereza un cuadro o inventa un rodapié, y el cliente deja de
   reconocer su propia casa.
3. No es determinista. La misma baldosa daría un resultado distinto cada vez,
   y eso en una herramienta de venta destruye la confianza.

La restricción no se le pide al modelo por prompt: se impone después, en el
navegador, descartando todo menos la iluminación. Un prompt se puede ignorar;
un multiplicador escalar suavizado, no.
"""

import base64
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase
from app.services.limites_service import tiene_fotos_disponibles, descontar_foto
from app.services.imagen_service import editar_objeto

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/superficies", tags=["superficies"])

TIEMPO_LIMITE = 120.0


class RefinarRequest(BaseModel):
    # Compuesto que ya armó el navegador, PNG en base64 (sin el encabezado data:)
    imagen_base64: str
    # PNG del mismo tamaño: transparente donde la IA puede trabajar,
    # opaco donde NO debe tocar. Sale de la máscara de SAM.
    mascara_base64: str
    # Foto del producto, para que el modelo entienda de qué material se trata
    producto_url: str | None = None
    producto_nombre: str | None = None
    # 'piso' | 'pared' | 'techo'
    superficie: str = "piso"
    # Empresa que usa la herramienta. Sin ella se rechaza: cada llamada a la
    # IA cuesta, y el botón no puede quedar abierto a cualquier visitante.
    empresa_id: str | None = None


class ObjetoRequest(BaseModel):
    # Escena actual (PNG base64) y máscara del mismo tamaño: alfa 0 = objeto
    imagen_base64: str
    mascara_base64: str
    accion: str                         # 'quitar' | 'cambiar'
    empresa_id: str
    producto_url: str | None = None     # solo para 'cambiar'
    producto_nombre: str | None = None


# Lo que usa IA en el editor es del plan Profesional para arriba. El cálculo
# de pisos y paredes sigue gratis para todos: corre en el aparato de cada
# quien y no le cuesta nada a nadie.
PLANES_CON_IA_EN_EDITOR = ("profesional", "premium", "corporativo")


async def _verificar_empresa_para_ia(empresa_id: str | None):
    """Plan, pago y cupo leídos de la base — nunca de lo que diga el navegador."""
    if not empresa_id:
        raise HTTPException(status_code=403,
            detail="Esta herramienta es del plan Profesional. Inicia sesión con tu cuenta de empresa.")
    try:
        r = get_supabase().table("empresas").select("estado, planes(nombre)") \
            .eq("id", empresa_id).maybe_single().execute()
    except Exception as e:
        logger.error(f"No se pudo verificar la empresa {empresa_id}: {e}")
        raise HTTPException(status_code=503, detail="No se pudo verificar tu plan, intenta de nuevo")
    if not r or not r.data:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    plan = ((r.data.get("planes") or {}).get("nombre") or "").lower().replace("á", "a")
    if plan not in PLANES_CON_IA_EN_EDITOR:
        raise HTTPException(status_code=403,
            detail="Quitar y cambiar objetos, y mejorar con IA, son del plan Profesional.")
    if (r.data.get("estado") or "") != "activo":
        raise HTTPException(status_code=402, detail="Tu plan está pendiente de pago.")
    if not await tiene_fotos_disponibles(empresa_id):
        raise HTTPException(status_code=402,
            detail="Se acabaron tus generaciones con IA. Recarga desde tu cuenta.")


def _prompt(superficie: str, producto: str | None) -> str:
    """
    El prompt insiste en iluminación y prohíbe cambios de material. Es una capa
    de defensa, no la única: aunque el modelo lo desobedezca, el navegador
    solo se va a quedar con la luz.
    """
    que = {"piso": "floor", "pared": "wall", "techo": "ceiling"}.get(superficie, "floor")
    material = f" The {que} material is {producto}." if producto else ""
    return (
        f"Photorealistic interior photo retouch. Only adjust the LIGHTING of the {que} "
        f"so it integrates naturally with the room: realistic contact shadows under "
        f"furniture legs and objects, correct light falloff from the existing light "
        f"sources, subtle reflections consistent with the room."
        f"{material}"
        f" CRITICAL: do not change the material, its color, its pattern, its tile size "
        f"or its layout. Do not move, add or remove any furniture or object. "
        f"Do not alter the walls, ceiling, windows or the camera perspective. "
        f"Keep the exact same composition."
    )


@router.post("/refinar")
async def refinar_superficie(data: RefinarRequest, request: Request):
    """
    Devuelve una imagen de referencia de iluminación. El navegador NO la
    muestra tal cual: le extrae la luz y la aplica sobre su propio compuesto.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="Falta configurar la clave de OpenAI")
    await _verificar_empresa_para_ia(data.empresa_id)

    try:
        imagen = base64.b64decode(data.imagen_base64)
        mascara = base64.b64decode(data.mascara_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="La imagen o la máscara no son base64 válido")

    if len(imagen) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="La imagen supera los 20 MB")

    try:
        async with httpx.AsyncClient(timeout=TIEMPO_LIMITE) as cliente:
            respuesta = await cliente.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                files={
                    "image": ("escena.png", imagen, "image/png"),
                    "mask": ("mascara.png", mascara, "image/png"),
                },
                data={
                    "model": "gpt-image-1",
                    "prompt": _prompt(data.superficie, data.producto_nombre),
                    "n": "1",
                    "size": "1024x1024",
                    # Fidelidad alta: se quiere el mínimo cambio posible
                    "input_fidelity": "high",
                    # En "auto" bloquea fotos de obra normales sin explicar
                    "moderation": "low",
                },
            )

        if respuesta.status_code != 200:
            logger.error("gpt-image-1 falló: %s", respuesta.text[:500])
            raise HTTPException(status_code=502, detail="El servicio de IA no respondió bien")

        salida = respuesta.json()["data"][0]
        await descontar_foto(data.empresa_id)
        return {
            "ok": True,
            "imagen_base64": salida.get("b64_json"),
            "url": salida.get("url"),
            # Se le recuerda al frontend que esto es referencia, no resultado
            "uso": "referencia_de_iluminacion",
        }

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="La IA tardó demasiado, intenta de nuevo")
    except Exception as e:
        logger.exception("Error refinando superficie")
        raise HTTPException(status_code=500, detail=f"Error inesperado: {e}")


# ─────────────────────────────────────────────────────────────────────────
# QUITAR O CAMBIAR UN OBJETO — plan Profesional
# El asesor toca un objeto en la foto, el navegador lo recorta (SAM), y aquí
# la IA lo quita o lo reemplaza por un producto del catálogo.
# ─────────────────────────────────────────────────────────────────────────
@router.post("/objeto")
async def objeto(data: ObjetoRequest):
    if data.accion not in ("quitar", "cambiar"):
        raise HTTPException(status_code=400, detail="Acción no válida")
    if data.accion == "cambiar" and not data.producto_url:
        raise HTTPException(status_code=400, detail="Elige el producto por el que lo quieres cambiar")
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="Falta configurar la clave de OpenAI")
    await _verificar_empresa_para_ia(data.empresa_id)

    try:
        escena = base64.b64decode(data.imagen_base64)
        mascara = base64.b64decode(data.mascara_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="La imagen o la máscara no son válidas")
    if len(escena) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="La imagen supera los 20 MB")

    producto_bytes = None
    if data.accion == "cambiar":
        try:
            async with httpx.AsyncClient(timeout=30.0) as cli:
                pr = await cli.get(data.producto_url)
            if pr.status_code != 200 or not pr.headers.get("content-type", "").startswith("image/"):
                raise ValueError(f"respondió {pr.status_code} {pr.headers.get('content-type')}")
            producto_bytes = pr.content
        except Exception as e:
            logger.warning(f"No se pudo descargar la foto del producto: {e}")
            raise HTTPException(status_code=400, detail="No se pudo cargar la foto de ese producto")

    try:
        resultado = await editar_objeto(escena, mascara, data.accion,
                                        producto_bytes, data.producto_nombre)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="La IA tardó demasiado, intenta de nuevo")

    # Se descuenta solo si se entregó: un fallo del proveedor no cuesta cupo
    await descontar_foto(data.empresa_id)
    return {"ok": True, "imagen_base64": base64.b64encode(resultado).decode()}


# ─────────────────────────────────────────────────────────────────────────
# BANDEJA DEL ASESOR — plan Básico
# El plan Básico no da acceso al dashboard completo (Editor Planta, Entorno
# 3D, etc.), así que esta lista ES su dashboard: aquí ve las remodelaciones
# que la IA le generó a sus clientes por WhatsApp, con el teléfono de cada
# uno para poder contactarlos.
# ─────────────────────────────────────────────────────────────────────────
@router.get("/bandeja/{empresa_id}")
async def bandeja_asesor(empresa_id: str, limite: int = 40, solo_clientes: bool = True):
    """Últimas imágenes generadas para los clientes de esta empresa."""
    if limite < 1 or limite > 200:
        limite = 40
    supabase = get_supabase()

    # El nombre de la columna de fecha no es igual en todas las tablas del
    # proyecto: clientes_finales usa created_at, otras usan creado_en. Ordenar
    # por una que no existe hace fallar la consulta ENTERA, y el asesor ve
    # "no se pudo cargar" sin más pistas. Se prueban las variantes y, si
    # ninguna existe, se devuelve sin ordenar antes que no devolver nada.
    campos = "id, url_generada, url_original, tipo_espacio, estilo, telefono, producto, origen"
    ultimo_error = None

    for col in ("creado_en", "created_at", "fecha", None):
        try:
            sel = f"{campos}, {col}" if col else campos
            q = supabase.table("imagenes").select(sel).eq("empresa_id", empresa_id)
            # La bandeja es lo que le llegó de SUS CLIENTES. Mezclar ahí los
            # renders que la propia tienda hizo en el visor 3D es ruido: el
            # asesor busca a quién llamar, no su propio trabajo.
            if solo_clientes:
                q = q.eq("origen", "whatsapp")
            if col:
                q = q.order(col, desc=True)
            r = q.limit(limite).execute()
            items = r.data or []
            # Se normaliza el nombre para que el frontend no tenga que adivinar
            if col and col != "creado_en":
                for it in items:
                    it["creado_en"] = it.get(col)
            return {"ok": True, "items": items, "orden": col or "sin_orden"}
        except Exception as e:
            ultimo_error = e
            continue

    logger.error(f"Bandeja: ninguna variante de consulta funcionó — {ultimo_error}")
    raise HTTPException(
        status_code=500,
        detail=f"No se pudo leer la bandeja: {ultimo_error}"
    )