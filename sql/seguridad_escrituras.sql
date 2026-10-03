-- ═══════════════════════════════════════════════════════════════════════
--  SEGURIDAD DE ESCRITURAS (AUD-SEC-001)               30 sep 2026
--  Correr UNA vez en Supabase → SQL Editor.
--
--  El navegador escribe en empresas, tiendas y productos con la llave
--  pública. Si las políticas RLS no lo impiden, una cuenta podría, desde la
--  consola del navegador, activarse un plan sin pagar, bajarse la comisión o
--  cambiar los precios de otra tienda. Estos disparadores lo impiden DENTRO
--  de la base, existan o no políticas RLS, y no rompen ningún flujo actual:
--    · El servidor (llave de servicio) y el Super Admin pueden todo.
--    · Cada cuenta solo toca SU empresa, SUS tiendas y SUS productos.
--    · Nadie más que el servidor o el admin cambia plan, estado, cupo de
--      fotos, comisión, activación de la tienda o el bot de WhatsApp.
-- ═══════════════════════════════════════════════════════════════════════

-- 0. DIAGNÓSTICO (solo lectura): RLS y políticas actuales de cada tabla
select c.relname as tabla, c.relrowsecurity as rls_activo,
       (select count(*) from pg_policies p where p.schemaname = 'public' and p.tablename = c.relname) as politicas
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r'
  and c.relname in ('empresas','tiendas','productos','pedidos','notificaciones','consumidores','pagos','solicitudes_plan','imagenes_compra','eventos','resenas')
order by 1;

-- 1. ¿Quién está escribiendo?
create or replace function public.es_admin_o_servidor() returns boolean
language sql stable set search_path = public as $$
  select current_user in ('postgres', 'service_role', 'supabase_admin')
      or coalesce(auth.jwt() ->> 'role', '') = 'service_role'
      or lower(coalesce(auth.jwt() ->> 'email', '')) = 'oscar8a.cds@gmail.com'
$$;

create or replace function public.correo_sesion() returns text
language sql stable set search_path = public as $$
  select coalesce(nullif(lower(coalesce(auth.jwt() ->> 'email', '')), ''), '#sin-sesion#')
$$;

-- 2. EMPRESAS
create or replace function public.guardia_empresas() returns trigger language plpgsql set search_path = public as $$
declare precio numeric;
begin
  if public.es_admin_o_servidor() then return coalesce(new, old); end if;
  if tg_op = 'DELETE' then raise exception 'No autorizado: no se puede borrar una empresa desde aquí'; end if;
  if tg_op = 'INSERT' then
    if lower(coalesce(new.email, '')) <> public.correo_sesion() then
      raise exception 'No autorizado: solo puedes registrar la empresa de tu propio correo';
    end if;
    -- Un plan de pago nace SIEMPRE pendiente: se activa solo cuando Wompi confirma el pago
    select p.precio into precio from planes p where p.id = new.plan_id;
    if coalesce(precio, 0) > 0 then new.estado := 'pendiente_pago'; end if;
    -- Nadie se inventa una vigencia al registrarse (las columnas de fecha las crea
    -- sql/vigencia_planes.sql; jsonb_populate_record ignora las que aún no existan)
    new := jsonb_populate_record(new, jsonb_build_object('plan_inicio', null, 'plan_vence', null, 'plan_aviso_vence', false));
    return new;
  end if;
  -- UPDATE: solo la empresa propia, y nunca las columnas de plata
  if lower(coalesce(old.email, '')) <> public.correo_sesion() then
    raise exception 'No autorizado: no puedes modificar otra empresa';
  end if;
  if new.plan_id is distinct from old.plan_id or new.estado is distinct from old.estado
     or new.fotos_disponibles is distinct from old.fotos_disponibles or new.fotos_usadas is distinct from old.fotos_usadas
     or new.email is distinct from old.email or new.whatsapp_phone_number_id is distinct from old.whatsapp_phone_number_id
     or (new.whatsapp_estado is distinct from old.whatsapp_estado and new.whatsapp_estado <> 'pendiente')
     -- Las fechas del plan: se comparan por jsonb para que esta función sirva
     -- exista o no la columna, y corras este archivo antes o después de vigencia_planes.sql
     or (to_jsonb(new) ->> 'plan_inicio')      is distinct from (to_jsonb(old) ->> 'plan_inicio')
     or (to_jsonb(new) ->> 'plan_vence')       is distinct from (to_jsonb(old) ->> 'plan_vence')
     or (to_jsonb(new) ->> 'plan_aviso_vence') is distinct from (to_jsonb(old) ->> 'plan_aviso_vence') then
    raise exception 'No autorizado: el plan, su vigencia, el estado, el cupo de fotos y la activación de WhatsApp solo los cambia DecoIArte';
  end if;
  return new;
end $$;
drop trigger if exists tr_guardia_empresas on public.empresas;
create trigger tr_guardia_empresas before insert or update or delete on public.empresas
  for each row execute function public.guardia_empresas();

-- 3. TIENDAS
create or replace function public.guardia_tiendas() returns trigger language plpgsql set search_path = public as $$
begin
  if public.es_admin_o_servidor() then return coalesce(new, old); end if;
  if tg_op = 'DELETE' then raise exception 'No autorizado: no se puede borrar una tienda desde aquí'; end if;
  if not exists (select 1 from empresas e where e.id = new.empresa_id and lower(e.email) = public.correo_sesion()) then
    raise exception 'No autorizado: la tienda debe pertenecer a tu empresa';
  end if;
  if tg_op = 'INSERT' then
    new.comision_porcentaje := null;   -- la comisión la fija DecoIArte (5 % por defecto)
    new.activa := true;
    return new;
  end if;
  if new.empresa_id is distinct from old.empresa_id or new.comision_porcentaje is distinct from old.comision_porcentaje
     or new.activa is distinct from old.activa or new.plan_nombre is distinct from old.plan_nombre then
    raise exception 'No autorizado: la comisión, la activación y el plan de la tienda solo los cambia DecoIArte';
  end if;
  return new;
end $$;
drop trigger if exists tr_guardia_tiendas on public.tiendas;
create trigger tr_guardia_tiendas before insert or update or delete on public.tiendas
  for each row execute function public.guardia_tiendas();

-- 4. PRODUCTOS
create or replace function public.guardia_productos() returns trigger language plpgsql set search_path = public as $$
declare tid uuid;
begin
  if public.es_admin_o_servidor() then return coalesce(new, old); end if;
  tid := case when tg_op = 'DELETE' then old.tienda_id else new.tienda_id end;
  if not exists (select 1 from tiendas t join empresas e on e.id = t.empresa_id
                 where t.id = tid and lower(e.email) = public.correo_sesion()) then
    raise exception 'No autorizado: solo puedes gestionar los productos de tu propia tienda';
  end if;
  if tg_op = 'UPDATE' and new.tienda_id is distinct from old.tienda_id then
    raise exception 'No autorizado: un producto no se puede mover a otra tienda';
  end if;
  return coalesce(new, old);
end $$;
drop trigger if exists tr_guardia_productos on public.productos;
create trigger tr_guardia_productos before insert or update or delete on public.productos
  for each row execute function public.guardia_productos();

-- 5. Estas funciones no se llaman desde afuera (/rest/v1/rpc/...)
revoke execute on function public.guardia_empresas()  from public, anon, authenticated;
revoke execute on function public.guardia_tiendas()   from public, anon, authenticated;
revoke execute on function public.guardia_productos() from public, anon, authenticated;

-- Comprobar: deben salir 3 guardias
select event_object_table as tabla, trigger_name from information_schema.triggers
where trigger_name like 'tr_guardia_%' group by 1, 2 order by 1;

-- ═══════════════════════════════════════════════════════════════════════
-- 6. AVISOS DE LA TIENDA EN TIEMPO REAL (AUD-NOTIF-001)
--    La campana del dashboard se entera al instante de un pedido nuevo.
--    (Si esto no se corre, igual se revisa cada 30 segundos.)
-- ═══════════════════════════════════════════════════════════════════════
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime'
                     and schemaname = 'public' and tablename = 'notificaciones') then
    alter publication supabase_realtime add table public.notificaciones;
  end if;
end $$;

-- Comprobar: debe aparecer notificaciones (y pedidos, de antes)
select tablename from pg_publication_tables
where pubname = 'supabase_realtime' and tablename in ('notificaciones', 'pedidos') order by 1;
