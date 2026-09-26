"""
Quién hace la petición y si lo que pide es suyo.

Regla: el servidor NUNCA cree el empresa_id ni el tienda_id que manda el
navegador. Esos ids son públicos (la tabla de tiendas se lee sin sesión y los
trae), así que cualquiera podría usarlos para gastarle la IA a otra tienda o
ver sus datos. Se comprueba con el token de la sesión de Supabase y la base.

Nació en app/api/superficies.py como _exigir_duenio; vive aquí para que todos
los endpoints usen la misma puerta.
"""
import logging

from fastapi import HTTPException, Request

from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

ADMIN_EMAIL = "oscar8a.cds@gmail.com"   # el mismo que usan las políticas RLS y el trigger


def email_de_sesion(request: Request) -> str:
    """Correo de la sesión que viene en 'Authorization: Bearer <token>'. 401 si no hay o expiró."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        raise HTTPException(status_code=401, detail="Inicia sesión para usar esta herramienta.")
    try:
        u = get_supabase().auth.get_user(auth[7:].strip())
        email = ((u.user.email if u and u.user else "") or "").lower()
    except Exception:
        raise HTTPException(status_code=401, detail="Tu sesión expiró. Vuelve a iniciar sesión.")
    if not email:
        raise HTTPException(status_code=401, detail="Tu sesión expiró. Vuelve a iniciar sesión.")
    return email


async def exigir_duenio(request: Request, empresa_id: str | None) -> str:
    """Sesión válida y la empresa es de esa cuenta (o es el administrador). Devuelve el correo."""
    email = email_de_sesion(request)
    if email == ADMIN_EMAIL:
        return email
    if not empresa_id:
        raise HTTPException(status_code=403, detail="Esta herramienta es para cuentas de empresa.")
    try:
        r = get_supabase().table("empresas").select("id") \
            .eq("id", empresa_id).eq("email", email).maybe_single().execute()
    except Exception as e:
        logger.error(f"No se pudo verificar el dueño de {empresa_id}: {e}")
        raise HTTPException(status_code=503, detail="No se pudo verificar tu cuenta, intenta de nuevo")
    if not r or not r.data:
        logger.warning(f"Intento de usar la empresa {empresa_id} desde la cuenta {email}")
        raise HTTPException(status_code=403, detail="Esa empresa no pertenece a tu cuenta.")
    return email


async def exigir_duenio_tienda(request: Request, tienda_id: str | None, pagado: bool = False) -> str:
    """
    Igual que exigir_duenio, pero partiendo de una tienda: busca a qué empresa
    pertenece y comprueba que esa empresa sea de la sesión. Devuelve el empresa_id
    leído de la base (no el que diga el navegador).
    Con pagado=True además exige el plan activo: regla de /estructura, sin pago
    no se usa la IA (la tienda sí se puede armar a mano).
    """
    email = email_de_sesion(request)
    if not tienda_id:
        raise HTTPException(status_code=400, detail="Falta la tienda.")
    try:
        r = get_supabase().table("tiendas").select("empresa_id") \
            .eq("id", tienda_id).maybe_single().execute()
    except Exception as e:
        logger.error(f"No se pudo leer la tienda {tienda_id}: {e}")
        raise HTTPException(status_code=503, detail="No se pudo verificar tu tienda, intenta de nuevo")
    empresa_id = (r.data or {}).get("empresa_id") if r else None
    if not empresa_id:
        # Mismo mensaje que "no es tuya": no se le confirma a nadie qué ids existen
        logger.warning(f"Tienda inexistente {tienda_id} pedida desde {email}")
        raise HTTPException(status_code=403, detail="Esa tienda no pertenece a tu cuenta.")
    if email != ADMIN_EMAIL:
        try:
            e = get_supabase().table("empresas").select("id, estado") \
                .eq("id", empresa_id).eq("email", email).maybe_single().execute()
        except Exception as ex:
            logger.error(f"No se pudo verificar el dueño de la tienda {tienda_id}: {ex}")
            raise HTTPException(status_code=503, detail="No se pudo verificar tu cuenta, intenta de nuevo")
        if not e or not e.data:
            logger.warning(f"Intento de usar la tienda {tienda_id} desde la cuenta {email}")
            raise HTTPException(status_code=403, detail="Esa tienda no pertenece a tu cuenta.")
        if pagado and (e.data.get("estado") or "") != "activo":
            raise HTTPException(status_code=403, detail="Importar con IA se habilita cuando tu plan esté activo. "
                                                        "Mientras tanto puedes agregar tus productos a mano.")
    return empresa_id