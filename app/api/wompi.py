import hashlib
import logging
import json
import httpx
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from app.utils.config import get_settings, Settings
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wompi", tags=["wompi"])


class FirmaRequest(BaseModel):
    referencia: str
    monto: int      # en centavos
    moneda: str = "COP"


class FirmaResponse(BaseModel):
    firma: str


@router.post("/firma", response_model=FirmaResponse)
async def generar_firma(
    body: FirmaRequest,
    settings: Settings = Depends(get_settings)
):
    """
    Genera la firma de integridad para Wompi.
    El secreto de integridad NUNCA sale del backend.
    SHA256(referencia + monto + moneda + secreto_integridad)
    """
    try:
        cadena = f"{body.referencia}{body.monto}{body.moneda}{settings.wompi_secreto_integridad}"
        firma  = hashlib.sha256(cadena.encode()).hexdigest()
        logger.info(f"Firma Wompi generada para referencia: {body.referencia}")
        return FirmaResponse(firma=firma)
    except Exception as e:
        logger.error(f"Error generando firma Wompi: {e}")
        raise


@router.get("/estado/{referencia}")
async def consultar_estado_pago(
    referencia: str,
    settings: Settings = Depends(get_settings)
):
    """Consulta el estado de una transacción en Wompi."""
    try:
        url = f"https://sandbox.wompi.co/v1/transactions?reference={referencia}"
        headers = {"Authorization": f"Bearer {settings.wompi_llave_privada}"}
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            data = r.json()
            transacciones = data.get("data", [])
            if not transacciones:
                return {"estado": "no_encontrado", "referencia": referencia}
            ultima = transacciones[-1]
            return {
                "estado":     ultima.get("status"),
                "referencia": referencia,
                "monto":      ultima.get("amount_in_cents", 0) / 100,
                "moneda":     ultima.get("currency"),
                "metodo":     ultima.get("payment_method_type"),
            }
    except Exception as e:
        logger.error(f"Error consultando estado Wompi: {e}")
        return {"estado": "error", "detalle": str(e)}


# ── WEBHOOK WOMPI ─────────────────────────────────────────────────────────
@router.post("/webhook")
async def webhook_wompi(
    request: Request,
    settings: Settings = Depends(get_settings)
):
    """
    Wompi llama este endpoint cuando hay un evento de pago.
    Verificamos la firma, y si el pago es APPROVED:
    - Guardamos la notificación en Supabase
    - El dashboard del asesor la recibe en tiempo real
    """
    try:
        body = await request.json()
        logger.info(f"Webhook Wompi recibido: {body.get('event', 'unknown')}")

        # ── Verificar firma del evento (algoritmo real de Wompi) ──────────
        # Wompi firma: SHA256( concat(valores de signature.properties) + timestamp + secreto_de_EVENTOS )
        # Ojo: signature.properties son RUTAS dentro de "data" (ej: "transaction.id",
        # "transaction.status"), no un id de evento fijo — y el secreto de eventos
        # es DIFERENTE al secreto de integridad que se usa para firmar pagos.
        firma_info   = body.get("signature", {}) or {}
        propiedades  = firma_info.get("properties", []) or []
        checksum     = (firma_info.get("checksum") or "").lower()
        timestamp    = body.get("timestamp", "")
        data_evento  = body.get("data", {}) or {}

        def _valor_por_ruta(data: dict, ruta: str):
            actual = data
            for parte in ruta.split("."):
                if not isinstance(actual, dict) or parte not in actual:
                    return ""
                actual = actual[parte]
            return actual

        valores_concatenados = "".join(str(_valor_por_ruta(data_evento, p)) for p in propiedades)
        cadena_verificacion = f"{valores_concatenados}{timestamp}{settings.wompi_secreto_eventos}"
        firma_esperada = hashlib.sha256(cadena_verificacion.encode()).hexdigest().lower()

        if not settings.wompi_secreto_eventos:
            logger.error("WOMPI_SECRETO_EVENTOS no está configurado — rechazando webhook por seguridad")
            raise HTTPException(status_code=500, detail="Webhook mal configurado")

        if not checksum or checksum != firma_esperada:
            logger.warning("Firma Wompi inválida — posible intento de fraude. Webhook rechazado.")
            raise HTTPException(status_code=401, detail="Firma inválida")

        # ── Procesar solo eventos de transacción ──────────────────────────
        evento = body.get("event", "")
        if evento != "transaction.updated":
            return {"status": "ok", "mensaje": "evento ignorado"}

        transaccion = body.get("data", {}).get("transaction", {})
        estado      = transaccion.get("status", "")
        referencia  = transaccion.get("reference", "")
        monto_cents = transaccion.get("amount_in_cents", 0)
        monto_cop   = monto_cents / 100
        metodo      = transaccion.get("payment_method_type", "")
        tx_id       = transaccion.get("id", "")
        cliente_email = transaccion.get("customer_email", "")

        logger.info(f"Transacción {tx_id} — Estado: {estado} — Ref: {referencia}")

        if estado != "APPROVED":
            return {"status": "ok", "mensaje": f"transacción {estado} ignorada"}

        # ── Pago aprobado — buscar la tienda por la referencia ────────────
        # La referencia tiene formato: DECO-{tienda_id_8chars}-{timestamp}
        empresa_id = None
        tienda_nombre = "DecoIArte"

        try:
            # Extraer tienda_id de la referencia: DECO-f4c1517e-1781842211218
            partes = referencia.split("-")
            if len(partes) >= 2:
                tienda_id_partial = partes[1]  # primeros 8 chars del tienda_id
                supabase = get_supabase()

                # Buscar tienda que empiece con ese ID
                r = supabase.table("tiendas").select(
                    "id, nombre, empresa_id"
                ).ilike("id", f"{tienda_id_partial}%").maybeSingle().execute()

                if r.data:
                    empresa_id    = r.data.get("empresa_id")
                    tienda_nombre = r.data.get("nombre", "Tu tienda")
                    logger.info(f"Tienda encontrada: {tienda_nombre} — empresa: {empresa_id}")

        except Exception as e:
            logger.error(f"Error buscando tienda: {e}")

        # ── Insertar notificación en Supabase ─────────────────────────────
        if empresa_id:
            try:
                supabase = get_supabase()
                supabase.table("notificaciones").insert({
                    "empresa_id": empresa_id,
                    "tipo":       "pago",
                    "titulo":     f"💰 ¡Nuevo pago recibido!",
                    "mensaje":    f"${monto_cop:,.0f} COP · Ref: {referencia} · {metodo}",
                    "leida":      False,
                    "datos": {
                        "referencia":       referencia,
                        "monto":            monto_cop,
                        "metodo":           metodo,
                        "tx_id":            tx_id,
                        "cliente_email":    cliente_email,
                        "tienda_nombre":    tienda_nombre,
                    }
                }).execute()
                logger.info(f"Notificación insertada para empresa {empresa_id}")

            except Exception as e:
                logger.error(f"Error insertando notificación: {e}")

        return {"status": "ok", "mensaje": "pago procesado correctamente"}

    except HTTPException:
        raise  # firma inválida u otro error de seguridad — debe rechazarse de verdad

    except Exception as e:
        logger.error(f"Error en webhook Wompi: {e}")
        # Siempre devolver 200 a Wompi para que no reintente (errores no relacionados con seguridad)
        return {"status": "ok", "error": str(e)}