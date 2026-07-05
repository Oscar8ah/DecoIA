import logging
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)


async def tiene_fotos_disponibles(empresa_id: str) -> bool:
    """
    Revisa si la empresa todavía tiene fotos/renders disponibles este mes.
    Se debe llamar ANTES de generar cualquier render con IA (que cuesta dinero
    real), para no gastar de más si ya se acabó el cupo del plan.
    """
    if not empresa_id:
        return False
    try:
        supabase = get_supabase()
        r = supabase.table("empresas").select("fotos_disponibles").eq("id", empresa_id).maybeSingle().execute()
        if not r.data:
            return False
        return (r.data.get("fotos_disponibles") or 0) > 0
    except Exception as e:
        logger.error(f"Error revisando fotos disponibles para {empresa_id}: {e}")
        return False  # ante la duda, no dejar generar más (protege el saldo, no al revés)


async def descontar_foto(empresa_id: str) -> None:
    """
    Descuenta 1 foto disponible y suma 1 a fotos_usadas.
    Se debe llamar DESPUÉS de generar el render con éxito.
    No lanza excepción si falla — no queremos que un error aquí tumbe la
    respuesta del render ya generado, solo lo dejamos loggeado.
    """
    if not empresa_id:
        return
    try:
        supabase = get_supabase()
        r = supabase.table("empresas").select("fotos_disponibles, fotos_usadas").eq("id", empresa_id).maybeSingle().execute()
        if not r.data:
            return
        disponibles = max(0, (r.data.get("fotos_disponibles") or 0) - 1)
        usadas      = (r.data.get("fotos_usadas") or 0) + 1
        supabase.table("empresas").update({
            "fotos_disponibles": disponibles,
            "fotos_usadas":      usadas,
        }).eq("id", empresa_id).execute()
    except Exception as e:
        logger.error(f"Error descontando foto para {empresa_id}: {e}")