import hashlib
import logging
import json
import httpx
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from app.utils.config import get_settings, Settings
from app.utils.supabase_client import get_supabase
from app.services.recibo_service import generar_pdf_recibo, guardar_recibo

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


# ── PAGO DE CAMBIO DE PLAN (suscripción) ────────────────────────────────
async def _procesar_pago_cambio_plan(referencia: str, monto_cop: float, metodo: str, tx_id: str, cliente_email: str):
    """
    Referencia: SUSC-{empresa_id_8chars}-{timestamp}
    Al confirmarse el pago:
      1. Busca la empresa por el prefijo de su id.
      2. Busca su solicitud más reciente en estado 'pagando' -> ahí está
         a qué plan se quiere cambiar.
      3. Actualiza empresas: plan_id, estado='activo', fotos_disponibles
         recalculadas con el cupo del plan nuevo menos las ya usadas.
      4. Marca la solicitud como 'aprobada'.
      5. Registra el pago y notifica a la empresa.
    Si algo no calza (empresa o solicitud no encontrada), no truena — solo
    lo deja registrado en logs para revisar a mano, y le confirma 200 a
    Wompi para que no reintente indefinidamente.
    """
    supabase = get_supabase()
    empresa = None
    try:
        partes = referencia.split("-")
        if len(partes) >= 2:
            empresa_id_partial = partes[1]
            r = supabase.table("empresas").select(
                "id, nombre, email, fotos_usadas"
            ).ilike("id", f"{empresa_id_partial}%").maybeSingle().execute()
            empresa = r.data
    except Exception as e:
        logger.error(f"Error buscando empresa para cambio de plan: {e}")

    if not empresa:
        logger.error(f"No se encontró empresa para el pago de cambio de plan {referencia} — requiere revisión manual")
        return {"status": "ok", "mensaje": "empresa no encontrada, requiere revisión manual"}

    empresa_id = empresa["id"]

    solicitud = None
    try:
        r = supabase.table("solicitudes_plan").select(
            "id, plan_solicitado_id"
        ).eq("empresa_id", empresa_id).eq("estado", "pagando") \
         .order("created_at", desc=True).limit(1).maybeSingle().execute()
        solicitud = r.data
    except Exception as e:
        logger.error(f"Error buscando solicitud de cambio de plan: {e}")

    if not solicitud:
        logger.error(f"Pago {referencia} aprobado pero no hay solicitud 'pagando' para empresa {empresa_id} — requiere revisión manual")
        return {"status": "ok", "mensaje": "solicitud no encontrada, requiere revisión manual"}

    plan_nuevo = None
    try:
        r = supabase.table("planes").select("id, nombre, fotos_incluidas").eq("id", solicitud["plan_solicitado_id"]).maybeSingle().execute()
        plan_nuevo = r.data
    except Exception as e:
        logger.error(f"Error buscando plan nuevo: {e}")

    if not plan_nuevo:
        logger.error(f"Plan solicitado {solicitud['plan_solicitado_id']} no existe — requiere revisión manual")
        return {"status": "ok", "mensaje": "plan no encontrado, requiere revisión manual"}

    fotos_usadas = empresa.get("fotos_usadas") or 0
    fotos_nuevas_disponibles = max((plan_nuevo.get("fotos_incluidas") or 0) - fotos_usadas, 0)

    try:
        supabase.table("empresas").update({
            "plan_id":           plan_nuevo["id"],
            "estado":            "activo",
            "pago_metodo":       "wompi",
            "fotos_disponibles": fotos_nuevas_disponibles,
        }).eq("id", empresa_id).execute()

        supabase.table("solicitudes_plan").update({
            "estado":      "aprobada",
            "resuelta_at": datetime.now().isoformat(),
        }).eq("id", solicitud["id"]).execute()

        supabase.table("pagos").insert({
            "empresa_id":  empresa_id,
            "monto":       monto_cop,
            "tipo":        "cambio_plan",
            "metodo":      metodo,
            "estado":      "aprobado",
            "referencia":  referencia,
            "created_at":  datetime.now().isoformat(),
        }).execute()

        supabase.table("notificaciones").insert({
            "empresa_id": empresa_id,
            "tipo":       "plan",
            "titulo":     "🎉 ¡Tu plan fue actualizado!",
            "mensaje":    f"Ahora estás en el plan {plan_nuevo['nombre'].capitalize()}. ${monto_cop:,.0f} COP · Ref: {referencia}",
            "leida":      False,
            "datos": {
                "referencia": referencia,
                "monto":      monto_cop,
                "plan_nuevo": plan_nuevo["nombre"],
            }
        }).execute()

        logger.info(f"Cambio de plan aprobado — empresa {empresa_id} -> {plan_nuevo['nombre']}")
    except Exception as e:
        logger.error(f"Error activando cambio de plan para empresa {empresa_id}: {e}")
        return {"status": "ok", "error": str(e)}

    return {"status": "ok", "mensaje": "cambio de plan procesado correctamente"}


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

        # ── Pago de CAMBIO DE PLAN (suscripción) — referencia SUSC-... ────
        # Se procesa aparte de las compras del marketplace (DECO-...): activa
        # el plan solo, sin que nadie tenga que aprobar nada a mano.
        if referencia.startswith("SUSC-"):
            return await _procesar_pago_cambio_plan(referencia, monto_cop, metodo, tx_id, cliente_email)

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

        # ── Generar y guardar el recibo/comprobante de pago ───────────────
        url_recibo = None
        try:
            pdf_bytes  = generar_pdf_recibo(
                referencia=referencia, monto_cop=monto_cop, metodo=metodo,
                tienda_nombre=tienda_nombre, cliente_email=cliente_email,
            )
            url_recibo = await guardar_recibo(pdf_bytes, referencia)
        except Exception as e:
            logger.error(f"Error generando recibo para {referencia}: {e}")

        # ── Insertar notificación en Supabase ─────────────────────────────
        if empresa_id:
            try:
                supabase = get_supabase()

                # Registro real del pago (antes nada escribía en esta tabla)
                supabase.table("pagos").insert({
                    "empresa_id":  empresa_id,
                    "monto":       monto_cop,
                    "tipo":        "venta_marketplace",
                    "metodo":      metodo,
                    "estado":      "aprobado",
                    "referencia":  referencia,
                    "url_recibo":  url_recibo,
                    "created_at":  datetime.now().isoformat(),
                }).execute()

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
                        "url_recibo":       url_recibo,
                    }
                }).execute()
                logger.info(f"Pago y notificación registrados para empresa {empresa_id}")

            except Exception as e:
                logger.error(f"Error insertando pago/notificación: {e}")

        return {"status": "ok", "mensaje": "pago procesado correctamente"}

    except HTTPException:
        raise  # firma inválida u otro error de seguridad — debe rechazarse de verdad

    except Exception as e:
        logger.error(f"Error en webhook Wompi: {e}")
        # Siempre devolver 200 a Wompi para que no reintente (errores no relacionados con seguridad)
        return {"status": "ok", "error": str(e)}