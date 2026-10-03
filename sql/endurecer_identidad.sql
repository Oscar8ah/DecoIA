-- ═══════════════════════════════════════════════════════════════════════
--  ENDURECER IDENTIDAD Y FUNCIONES                               3 oct 2026
--  Correr UNA vez en Supabase → SQL Editor, DESPUÉS de rls_lecturas.sql.
--  Se puede correr dos veces sin daño. No cambia ningún flujo de las tiendas.
--
--  1. Un visitante SIN sesión ya no se confunde con una cuenta de correo vacío.
--     Antes, "sin sesión" se leía como el texto vacío '': un visitante podía crear
--     una empresa con correo vacío y, con ella, tiendas y productos falsos
--     visibles en el marketplace. Ahora "sin sesión" es un valor que ningún
--     correo real puede tener.
--  2. Un visitante sin sesión nunca escribe en empresas, tiendas, productos,
--     datos legales ni avisos (hoy ninguna pantalla pública lo necesita).
--  3. Las funciones de seguridad fijan su search_path (aviso del asesor de Supabase).
-- ═══════════════════════════════════════════════════════════════════════

-- 1. Identidad: sin sesión = valor imposible (los demás SQL comparan contra esta función)
create or replace function public.correo_sesion() returns text
language sql stable set search_path = public as $$
  select coalesce(nullif(lower(coalesce(auth.jwt() ->> 'email', '')), ''), '#sin-sesion#')
$$;

-- 3. search_path fijo en las funciones de seguridad
alter function public.es_admin_o_servidor() set search_path = public;
alter function public.guardia_empresas()    set search_path = public;
alter function public.guardia_tiendas()     set search_path = public;
alter function public.guardia_productos()   set search_path = public;

-- 2. Un visitante sin sesión no escribe en estas tablas
revoke insert, update, delete on public.empresas        from anon;
revoke insert, update, delete on public.tiendas         from anon;
revoke insert, update, delete on public.tiendas_privado from anon;
revoke insert, update, delete on public.productos       from anon;
revoke insert, update, delete on public.notificaciones  from anon;

-- Comprobar 1: debe salir 0 (empresas con correo vacío, creadas por el hueco anterior)
select count(*) as empresas_con_correo_vacio from public.empresas where coalesce(trim(email), '') = '';

-- Comprobar 2: las 5 funciones deben decir search_path=public
select p.proname as funcion, p.proconfig as configuracion
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('es_admin_o_servidor','correo_sesion','guardia_empresas','guardia_tiendas','guardia_productos')
order by 1;
