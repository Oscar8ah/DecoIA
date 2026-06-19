import hashlib
import logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from app.utils.config import get_settings, Settings

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

    Fórmula Wompi:
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
    """
    Consulta el estado de una transacción en Wompi.
    Útil para verificar pagos desde el backend.
    """
    import httpx
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