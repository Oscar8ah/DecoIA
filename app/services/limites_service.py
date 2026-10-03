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
        r = supabase.table("empresas").select("fotos_disponibles, estado").eq("id", empresa_id).maybe_single().execute()
        if not r.data:
            return False
        # El cupo solo sirve con el plan VIGENTE: un plan vencido (o sin pagar)
        # no genera imágenes, aunque le queden fotos (cada una cuesta dinero real).
        # Así quedan cubiertos el visor, /remodelar, el editor y el bot de WhatsApp.
        if (r.data.get("estado") or "") != "activo":
            logger.info(f"Empresa {empresa_id} con plan {r.data.get('estado')!r}: sin generación de imágenes")
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

# ═══ RESERVA DE IMÁGENES (sql/imagenes_planes.sql) ═══════════════════════════
# Cada transformación: reservar ANTES de llamar a la IA (resta en un paso
# atómico, solo con plan activo, vigente y con saldo) → confirmar si se entregó
# → devolver si falló. Así dos pedidos simultáneos no generan de más, un error
# nunca cuesta una imagen y un reintento no descuenta dos veces.
import contextvars
import functools

_RESERVA = contextvars.ContextVar("reserva_imagen", default=None)


class Reserva:
    def __init__(self, empresa_id: str, consumo_id=None, legado: bool = False):
        self.empresa_id, self.consumo_id, self.legado = empresa_id, consumo_id, legado
        self.cerrada = False

    async def confirmar(self) -> None:
        """La imagen se entregó: cuenta como usada (una sola vez)."""
        if self.cerrada:
            return
        self.cerrada = True
        if self.legado:
            await descontar_foto(self.empresa_id)
            return
        try:
            get_supabase().rpc("confirmar_imagen", {"p_consumo": self.consumo_id}).execute()
        except Exception as e:
            logger.error(f"No se pudo confirmar la imagen {self.consumo_id} de {self.empresa_id}: {e}")

    async def devolver(self) -> None:
        """Falló: la imagen vuelve al saldo (una sola vez)."""
        if self.cerrada:
            return
        self.cerrada = True
        if self.legado:
            return          # en el método anterior nada se había restado aún
        try:
            get_supabase().rpc("devolver_imagen", {"p_consumo": self.consumo_id}).execute()
        except Exception as e:
            # Si esto falla, la reserva se devuelve sola a los 20 minutos (en la base)
            logger.error(f"No se pudo devolver la imagen {self.consumo_id} de {self.empresa_id}: {e}")


async def reservar_imagen(empresa_id: str, origen: str):
    """Reserva UNA imagen. Devuelve la Reserva, o None si no hay saldo o el plan
    no está activo/vigente. Si la migración aún no está corrida, usa el método
    anterior (revisar ahora, descontar al final) para no frenar las ventas."""
    if not empresa_id:
        return None
    try:
        r = get_supabase().rpc("reservar_imagen", {"p_empresa": empresa_id, "p_origen": origen}).execute()
        consumo = r.data[0] if isinstance(r.data, list) and r.data else r.data
        if isinstance(consumo, dict):
            consumo = next(iter(consumo.values()), None)
        if not consumo:
            logger.info(f"Empresa {empresa_id}: sin imágenes disponibles o plan no vigente ({origen})")
            return None
        return Reserva(empresa_id, consumo)
    except Exception as e:
        logger.error(f"Reserva atómica no disponible ({e}); se usa el método anterior. ¿Se corrió sql/imagenes_planes.sql?")
        return Reserva(empresa_id, legado=True) if await tiene_fotos_disponibles(empresa_id) else None


async def reservar_para_esta_solicitud(empresa_id: str, origen: str):
    """Reserva y la deja asociada a la solicitud en curso, para que el
    decorador @cierra_reserva la confirme o la devuelva al terminar."""
    reserva = await reservar_imagen(empresa_id, origen)
    caja = _RESERVA.get()
    if caja is not None and reserva:
        caja.append(reserva)
    return reserva


async def confirmar_reserva_actual() -> None:
    """Confirma las reservas de esta solicitud (la imagen ya se entregó)."""
    for reserva in (_RESERVA.get() or []):
        await reserva.confirmar()


def cierra_reserva(exito=lambda resultado: True):
    """Decorador: al terminar la función, confirma las reservas abiertas si
    `exito(resultado)` es verdadero y las DEVUELVE si no, o si hubo cualquier error."""
    def decorador(func):
        @functools.wraps(func)
        async def envuelta(*args, **kwargs):
            token = _RESERVA.set([])
            reservas = _RESERVA.get()
            try:
                resultado = await func(*args, **kwargs)
            except BaseException:
                for r in reservas:
                    await r.devolver()
                raise
            finally:
                _RESERVA.reset(token)
            ok = False
            try:
                ok = bool(exito(resultado))
            except Exception:
                ok = False
            for r in reservas:
                await (r.confirmar() if ok else r.devolver())
            return resultado
        return envuelta
    return decorador