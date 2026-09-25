"""
Compra de imágenes del visitante — $5.000 por imagen.

Antes la imagen se entregaba gratis: render3d la guardaba en una carpeta
PÚBLICA y devolvía su dirección, y el "candado" era solo un dibujo encima en
la pantalla. Cualquiera la descargaba desde la consola del navegador.

Ahora:
  1. La imagen limpia va a un bucket PRIVADO (imagenes-compradas).
  2. Al comprador se le entrega una VISTA PREVIA más pequeña con marca de agua
     puesta en el servidor: no se puede quitar desde el navegador.
  3. Paga con el mismo widget de Wompi que ya usa el sitio (referencia IMG-).
  4. El webhook de Wompi confirma el pago, con firma verificada y monto
     comparado contra la base.
  5. Solo entonces se le da un enlace de descarga temporal a la imagen limpia.

Comprar exige sesión de comprador: nunca un anónimo.
"""
import hashlib
import io
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from PIL import Image, ImageDraw, ImageFont

from app.utils.config import get_settings
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/compras", tags=["compras"])

PRECIO_IMAGEN_COP = 5000
BUCKET_PRIVADO = "imagenes-compradas"
DURACION_ENLACE_S = 7 * 24 * 3600          # el enlace de descarga dura 7 días


# ── Quién pide ──────────────────────────────────────────────────────────
async def usuario_de_sesion(request: Request) -> dict:
    """Identifica al usuario por el token de su sesión de Supabase."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        raise HTTPException(status_code=401, detail="Inicia sesión para comprar.")
    try:
        u = get_supabase().auth.get_user(auth[7:].strip())
        usuario = u.user if u else None
    except Exception:
        usuario = None
    if not usuario or not usuario.id:
        raise HTTPException(status_code=401, detail="Tu sesión expiró. Vuelve a iniciar sesión.")
    return {"id": str(usuario.id), "email": (usuario.email or "").lower()}


# ── Marca de agua ───────────────────────────────────────────────────────
def marca_de_agua(imagen_bytes: bytes) -> bytes:
    """
    Vista previa: más pequeña y con la marca repetida en diagonal. Se dibuja
    con la fuente básica de Pillow ampliada, para no depender de que el
    servidor tenga fuentes instaladas.
    """
    base = Image.open(io.BytesIO(imagen_bytes)).convert("RGB")
    base.thumbnail((900, 900))               # la vista previa no sirve de entregable
    w, h = base.size

    texto = "DecoIArte - vista previa"
    fuente = ImageFont.load_default()
    medidor = ImageDraw.Draw(Image.new("L", (1, 1)))
    x0, y0, x1, y1 = medidor.textbbox((0, 0), texto, font=fuente)
    sello = Image.new("L", (x1 - x0 + 6, y1 - y0 + 6), 0)
    ImageDraw.Draw(sello).text((3 - x0, 3 - y0), texto, fill=255, font=fuente)
    escala = max(2, round(w / (sello.width * 3.0)))
    sello = sello.resize((sello.width * escala, sello.height * escala), Image.NEAREST)
    sello = sello.rotate(28, expand=True)

    capa = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    paso_x, paso_y = int(sello.width * 1.05), int(sello.height * 1.25)
    fila = 0
    for y in range(-sello.height // 2, h, max(1, paso_y)):
        desfase = (paso_x // 2) if fila % 2 else 0
        for x in range(-sello.width + desfase, w, max(1, paso_x)):
            capa.paste((0, 0, 0, 70), (x + 2, y + 2), sello)       # sombra: se ve en paredes claras
            capa.paste((255, 255, 255, 115), (x, y), sello)        # texto: se ve en pisos oscuros
        fila += 1

    salida = Image.alpha_composite(base.convert("RGBA"), capa).convert("RGB")
    buf = io.BytesIO()
    salida.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ── Guardar para vender (lo llama render3d) ─────────────────────────────
async def guardar_para_venta(imagen_bytes: bytes, usuario: dict,
                             empresa_id: str | None, tienda_id: str | None) -> dict:
    sb = get_supabase()
    ts = time.time_ns()
    referencia = f"IMG-{usuario['id'][:8]}-{ts // 1_000_000}"

    ruta_limpia = f"{usuario['id']}/{ts}.png"
    sb.storage.from_(BUCKET_PRIVADO).upload(ruta_limpia, imagen_bytes, {"content-type": "image/png"})

    ruta_previa = f"previas/{ts}.jpg"
    sb.storage.from_("portafolio").upload(ruta_previa, marca_de_agua(imagen_bytes), {"content-type": "image/jpeg"})
    url_previa = sb.storage.from_("portafolio").get_public_url(ruta_previa)

    r = sb.table("imagenes_compra").insert({
        "user_id":     usuario["id"],
        "email":       usuario["email"],
        "empresa_id":  empresa_id,
        "tienda_id":   tienda_id,
        "referencia":  referencia,
        "ruta_limpia": ruta_limpia,
        "url_preview": url_previa,
        "monto":       PRECIO_IMAGEN_COP,
    }).execute()
    fila = (r.data or [{}])[0]
    logger.info(f"Imagen a la venta {referencia} para {usuario['email']}")
    return {"id": fila.get("id"), "referencia": referencia, "url_preview": url_previa,
            "monto": PRECIO_IMAGEN_COP}


def _compra_del_usuario(compra_id: str, usuario: dict) -> dict:
    r = get_supabase().table("imagenes_compra").select("*").eq("id", compra_id).maybe_single().execute()
    if not r or not r.data:
        raise HTTPException(status_code=404, detail="Esa imagen no existe.")
    if r.data.get("user_id") != usuario["id"]:
        logger.warning(f"{usuario['email']} intentó acceder a la compra {compra_id} de otra persona")
        raise HTTPException(status_code=403, detail="Esa imagen no es tuya.")
    return r.data


def _enlace_descarga(ruta: str) -> str | None:
    try:
        r = get_supabase().storage.from_(BUCKET_PRIVADO).create_signed_url(ruta, DURACION_ENLACE_S)
        if isinstance(r, dict):
            return r.get("signedURL") or r.get("signedUrl") or r.get("signed_url")
        return getattr(r, "signed_url", None) or getattr(r, "signedURL", None)
    except Exception as e:
        logger.error(f"No se pudo firmar el enlace de {ruta}: {e}")
        return None


# ── Pagar ───────────────────────────────────────────────────────────────
@router.post("/imagen/{compra_id}/pagar")
async def pagar_imagen(compra_id: str, request: Request):
    """Datos para abrir el widget de Wompi. El monto sale de la base, nunca del navegador."""
    usuario = await usuario_de_sesion(request)
    compra = _compra_del_usuario(compra_id, usuario)
    if compra.get("pagada"):
        raise HTTPException(status_code=409, detail="Esta imagen ya está pagada.")
    settings = get_settings()
    if not settings.wompi_llave_publica or not settings.wompi_secreto_integridad:
        raise HTTPException(status_code=503, detail="La pasarela de pago no está configurada todavía.")

    monto_centavos = int(round(float(compra.get("monto") or PRECIO_IMAGEN_COP) * 100))
    moneda = "COP"
    # Misma fórmula que el resto del sitio: SHA256(referencia + monto + moneda + secreto)
    firma = hashlib.sha256(
        f"{compra['referencia']}{monto_centavos}{moneda}{settings.wompi_secreto_integridad}".encode()
    ).hexdigest()
    return {
        "llave_publica":  settings.wompi_llave_publica,
        "referencia":     compra["referencia"],
        "monto_centavos": monto_centavos,
        "moneda":         moneda,
        "firma":          firma,
    }


@router.get("/imagen/{compra_id}")
async def estado_imagen(compra_id: str, request: Request):
    """Si ya se pagó, entrega el enlace temporal a la imagen limpia."""
    usuario = await usuario_de_sesion(request)
    compra = _compra_del_usuario(compra_id, usuario)
    respuesta = {"pagada": bool(compra.get("pagada")), "url_preview": compra.get("url_preview"),
                 "referencia": compra.get("referencia")}
    if compra.get("pagada"):
        respuesta["url_descarga"] = _enlace_descarga(compra["ruta_limpia"])
    return respuesta


# ── Confirmación (la llama el webhook de Wompi, ya con la firma verificada) ─
async def confirmar_pago_imagen(referencia: str, monto_cop: float, tx_id: str) -> dict:
    sb = get_supabase()
    r = sb.table("imagenes_compra").select("id, monto, pagada").eq("referencia", referencia).maybe_single().execute()
    if not r or not r.data:
        logger.error(f"Pago aprobado de {referencia} pero esa imagen no existe")
        return {"status": "ok", "mensaje": "referencia desconocida"}
    if r.data.get("pagada"):
        return {"status": "ok", "mensaje": "ya estaba pagada"}
    esperado = float(r.data.get("monto") or PRECIO_IMAGEN_COP)
    if abs(esperado - float(monto_cop)) > 1:
        logger.error(f"⚠️ MONTO NO COINCIDE en {referencia}: esperado {esperado}, cobrado {monto_cop}")
        return {"status": "ok", "mensaje": "monto no coincide, queda para revisión"}
    sb.table("imagenes_compra").update({
        "pagada": True, "wompi_tx_id": tx_id,
        "pagada_en": datetime.now(timezone.utc).isoformat(),
    }).eq("id", r.data["id"]).execute()
    logger.info(f"✅ Imagen {referencia} pagada — tx {tx_id}")
    return {"status": "ok", "mensaje": "imagen desbloqueada"}