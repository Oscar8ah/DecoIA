-- ═══════════════════════════════════════════════════════════════════════
--  RLS DE LECTURAS + DATOS LEGALES PRIVADOS (AUD-SEC-001, mitad de lectura)
--  1 oct 2026 · Correr UNA vez en Supabase → SQL Editor, DESPUÉS de
--  sql/seguridad_escrituras.sql. Se puede correr dos veces sin daño.
--
--  Quién puede LEER qué, desde el navegador (el servidor usa la llave de
--  servicio y no lo afecta nada de esto):
--    empresas       → cada cuenta solo la suya · Super Admin todas
--    tiendas        → cualquiera las ACTIVAS (marketplace) · dueño la suya · admin todas
--    tiendas_privado→ (cédula/NIT, representante, correo legal, RUT…) solo dueño y admin
--    notificaciones → solo las de su empresa · admin todas
--    pedidos        → el comprador los suyos (tiempo real) · admin todos
--                     (la tienda los lee por el servidor, que oculta la dirección
--                      y el celular del comprador hasta que el pedido se paga)
-- ═══════════════════════════════════════════════════════════════════════

-- 0. Funciones de identidad (las mismas de seguridad_escrituras.sql)
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

-- Empresa(s) de la sesión, sin pasar por RLS (para usarla dentro de las políticas)
create or replace function public.mis_empresas() returns setof uuid
language sql stable security definer set search_path = public as $$
  select id from empresas where lower(email) = public.correo_sesion()
$$;
-- La usan las políticas de tiendas, que también lee el VISITANTE sin sesión
-- (marketplace): para él no devuelve nada, pero debe poder llamarse.
revoke execute on function public.mis_empresas() from public;
grant execute on function public.mis_empresas() to anon, authenticated;

-- 1. DATOS LEGALES → tabla privada
create table if not exists public.tiendas_privado (
  -- La relación se verifica al FINAL de la operación: al crear una tienda, su
  -- fila privada se crea en el mismo instante (antes de que la tienda exista).
  tienda_id                 uuid primary key references public.tiendas(id) on delete cascade deferrable initially deferred,
  nombre_representante      text,
  cedula_nit                text,
  correo_legal              text,
  direccion_correspondencia text,
  telefono_negocio          text,
  url_rut                   text,
  url_camara_comercio       text,
  actualizado_at            timestamptz default now()
);

-- Si la tabla ya existía de una corrida anterior, su relación también queda diferida
do $$
declare c text;
begin
  select conname into c from pg_constraint where conrelid = 'public.tiendas_privado'::regclass and contype = 'f';
  if c is not null then
    execute format('alter table public.tiendas_privado drop constraint %I', c);
  end if;
  alter table public.tiendas_privado add constraint tiendas_privado_tienda_id_fkey
    foreign key (tienda_id) references public.tiendas(id) on delete cascade deferrable initially deferred;
end $$;

-- Copiar lo que ya existe en tiendas (solo las columnas que de verdad existan)
do $$
declare c text; cols text[] := array['nombre_representante','cedula_nit','correo_legal','direccion_correspondencia',
                                      'telefono_negocio','url_rut','url_camara_comercio'];
begin
  insert into public.tiendas_privado (tienda_id) select id from public.tiendas on conflict do nothing;
  foreach c in array cols loop
    if exists (select 1 from information_schema.columns where table_schema='public' and table_name='tiendas' and column_name=c) then
      execute format('update public.tiendas_privado p set %1$I = t.%1$I from public.tiendas t
                      where t.id = p.tienda_id and t.%1$I is not null and p.%1$I is null', c);
      execute format('update public.tiendas set %1$I = null where %1$I is not null', c);   -- ya no quedan públicos
    end if;
  end loop;
end $$;

-- Si algo (el dashboard actual, o código viejo) guarda datos legales en tiendas,
-- se mueven solos a la tabla privada y en tiendas quedan vacíos.
create or replace function public.tienda_datos_privados() returns trigger
language plpgsql security definer set search_path = public as $$
declare j jsonb := to_jsonb(new); c text; vacios jsonb := '{}';
        cols text[] := array['nombre_representante','cedula_nit','correo_legal','direccion_correspondencia',
                             'telefono_negocio','url_rut','url_camara_comercio'];
begin
  insert into tiendas_privado (tienda_id) values (new.id) on conflict do nothing;
  foreach c in array cols loop
    if j ? c and j ->> c is not null then
      execute format('update tiendas_privado set %1$I = $1, actualizado_at = now() where tienda_id = $2', c) using j ->> c, new.id;
      vacios := vacios || jsonb_build_object(c, null);
    end if;
  end loop;
  if vacios <> '{}' then new := jsonb_populate_record(new, vacios); end if;
  return new;
end $$;
drop trigger if exists tr_tienda_datos_privados on public.tiendas;
create trigger tr_tienda_datos_privados before insert or update on public.tiendas
  for each row execute function public.tienda_datos_privados();
revoke execute on function public.tienda_datos_privados() from public, anon, authenticated;

-- Las filas que se acaban de copiar a tiendas_privado dejan verificaciones de
-- relación pendientes hasta el final de la operación. Como Supabase corre todo el
-- script en UNA sola transacción, Postgres no deja activar la seguridad en esa tabla
-- mientras haya verificaciones pendientes: se piden ahora, ya.
set constraints all immediate;

-- 2. RLS: se quitan las políticas anteriores de estas tablas y se dejan solo estas
do $$
declare p record;
begin
  for p in select tablename, policyname from pg_policies
           where schemaname = 'public' and tablename in ('empresas','tiendas','tiendas_privado','notificaciones','pedidos') loop
    execute format('drop policy %I on public.%I', p.policyname, p.tablename);
  end loop;
end $$;

alter table public.empresas        enable row level security;
alter table public.tiendas         enable row level security;
alter table public.tiendas_privado enable row level security;
alter table public.notificaciones  enable row level security;
alter table public.pedidos         enable row level security;

-- empresas: la propia (por correo de la sesión) o el admin
create policy empresas_leer       on public.empresas for select using (lower(email) = public.correo_sesion() or public.es_admin_o_servidor());
create policy empresas_crear      on public.empresas for insert with check (lower(email) = public.correo_sesion() or public.es_admin_o_servidor());
create policy empresas_editar     on public.empresas for update using (lower(email) = public.correo_sesion() or public.es_admin_o_servidor());
create policy empresas_borrar     on public.empresas for delete using (public.es_admin_o_servidor());

-- tiendas: las activas son públicas (marketplace); el dueño ve la suya aunque esté inactiva
create policy tiendas_leer        on public.tiendas for select using (activa = true or empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());
create policy tiendas_crear       on public.tiendas for insert with check (empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());
create policy tiendas_editar      on public.tiendas for update using (empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());
create policy tiendas_borrar      on public.tiendas for delete using (public.es_admin_o_servidor());

-- tiendas_privado: solo el dueño de la tienda y el admin
create policy privado_dueno       on public.tiendas_privado for all
  using (tienda_id in (select id from public.tiendas where empresa_id in (select public.mis_empresas())) or public.es_admin_o_servidor())
  with check (tienda_id in (select id from public.tiendas where empresa_id in (select public.mis_empresas())) or public.es_admin_o_servidor());

-- notificaciones: solo las de su empresa (leer, marcar leída, borrar); las crea el servidor
create policy notif_leer          on public.notificaciones for select using (empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());
create policy notif_editar        on public.notificaciones for update using (empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());
create policy notif_borrar        on public.notificaciones for delete using (empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());
create policy notif_crear         on public.notificaciones for insert with check (public.es_admin_o_servidor());

-- pedidos: el comprador los suyos (también para el tiempo real); todo lo demás, el servidor
create policy pedidos_leer        on public.pedidos for select using (user_id = auth.uid() or public.es_admin_o_servidor());

-- Comprobar
select c.relname as tabla, c.relrowsecurity as rls_activo,
       (select count(*) from pg_policies p where p.schemaname='public' and p.tablename=c.relname) as politicas
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname='public' and c.relname in ('empresas','tiendas','tiendas_privado','notificaciones','pedidos') order by 1;
