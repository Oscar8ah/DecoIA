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

from app.utils.config import OPENAI_API_KEY

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
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="Falta configurar OPENAI_API_KEY")

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
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
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
                },
            )

        if respuesta.status_code != 200:
            logger.error("gpt-image-1 falló: %s", respuesta.text[:500])
            raise HTTPException(status_code=502, detail="El servicio de IA no respondió bien")

        salida = respuesta.json()["data"][0]
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