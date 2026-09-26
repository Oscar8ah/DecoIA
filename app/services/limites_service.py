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
        r = supabase.table("empresas").select("fotos_disponibles").eq("id", empresa_id).maybe_single().execute()
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

    Cupo atómico: la base resta en UN solo paso con la función SQL
    descontar_foto_atomico (cupo_atomico.sql). Antes se leía el número y
    después se escribía: dos generaciones al mismo tiempo leían lo mismo y
    una foto salía gratis.
    """
    if not empresa_id:
        return
    supabase = get_supabase()
    try:
        r = supabase.rpc("descontar_foto_atomico", {"p_empresa": empresa_id}).execute()
        quedan = r.data[0] if isinstance(r.data, list) and r.data else r.data
        if quedan is None or quedan == []:
            # Llegaron a la vez más generaciones que fotos: esta ya se entregó y no
            # había cupo que descontar. Queda anotado para verlo en los logs de Render.
            logger.warning(f"Empresa {empresa_id}: se entregó una foto sin cupo (pedidos simultáneos)")
        return
    except Exception as e:
        logger.error(f"Cupo atómico no disponible ({e}); se usa el descuento anterior. ¿Se corrió cupo_atomico.sql?")
    # Respaldo: el método de antes, para no dejar de cobrar si la función aún no existe
    try:
        r = supabase.table("empresas").select("fotos_disponibles, fotos_usadas").eq("id", empresa_id).maybe_single().execute()
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