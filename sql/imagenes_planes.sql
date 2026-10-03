-- ═══════════════════════════════════════════════════════════════════════
--  IMÁGENES POR PLAN Y CONSUMO SEGURO                            2 oct 2026
--  Correr en Supabase → SQL Editor DESPUÉS de sql/vigencia_planes.sql.
--  Se puede correr dos veces sin daño.
--
--  Cada plan trae una cantidad de imágenes con IA para su año de vigencia
--  (aprobado el 2 oct 2026): Básico 100 · Profesional 200 · Premium 300 ·
--  Corporativo 900. Gratis 0.
--
--  Cada transformación sigue tres pasos, todos en la base:
--    reservar_imagen  → resta 1 en UN paso atómico, solo si el plan está activo,
--                        vigente y con saldo (dos pedidos a la vez no generan de más)
--    confirmar_imagen → la imagen se entregó: cuenta como usada
--    devolver_imagen  → falló: la imagen vuelve al saldo (un error nunca cuesta)
--  Confirmar y devolver solo actúan sobre una reserva abierta, así que repetirlos
--  (reintentos) no descuenta ni devuelve dos veces.
-- ═══════════════════════════════════════════════════════════════════════

-- 1. Cantidades aprobadas
update public.planes set fotos_incluidas = 100 where nombre = 'basico';
update public.planes set fotos_incluidas = 200 where nombre = 'profesional';
update public.planes set fotos_incluidas = 300 where nombre = 'premium';
-- Corporativo pasa de 500 a 900. Quien ya tiene Corporativo activo recibe la
-- diferencia una sola vez, para que su plan cumpla lo que ahora se ofrece.
do $$
declare viejas int;
begin
  select fotos_incluidas into viejas from public.planes where nombre = 'corporativo';
  if coalesce(viejas, 0) < 900 then
    update public.empresas e set fotos_disponibles = coalesce(e.fotos_disponibles, 0) + (900 - coalesce(viejas, 0))
      from public.planes p where p.id = e.plan_id and p.nombre = 'corporativo' and e.estado = 'activo';
    update public.planes set fotos_incluidas = 900 where nombre = 'corporativo';
  end if;
end $$;
update public.planes set fotos_incluidas = 0 where nombre = 'gratis';

-- 2. Registro de cada consumo
create table if not exists public.consumos_imagenes (
  id            uuid primary key default gen_random_uuid(),
  created_at    timestamptz not null default now(),
  empresa_id    uuid not null references public.empresas(id) on delete cascade,
  origen        text not null,                       -- foto_ia, foto_ia_comprador, editor, bot_whatsapp
  estado        text not null default 'reservada' check (estado in ('reservada','confirmada','devuelta')),
  confirmada_at timestamptz,
  devuelta_at   timestamptz
);
create index if not exists idx_consumos_empresa on public.consumos_imagenes (empresa_id, created_at desc);
create index if not exists idx_consumos_abiertos on public.consumos_imagenes (created_at) where estado = 'reservada';

alter table public.consumos_imagenes enable row level security;
drop policy if exists consumos_leer on public.consumos_imagenes;
create policy consumos_leer on public.consumos_imagenes for select
  using (empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());

-- 3. Devolver (reserva abierta → devuelta; el saldo vuelve)
create or replace function public.devolver_imagen(p_consumo uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare e uuid;
begin
  update consumos_imagenes set estado = 'devuelta', devuelta_at = now()
   where id = p_consumo and estado = 'reservada' returning empresa_id into e;
  if e is null then return false; end if;
  update empresas set fotos_disponibles = coalesce(fotos_disponibles, 0) + 1 where id = e;
  return true;
end $$;

-- 4. Reservar (atómico; devuelve el id de la reserva, o null si no hay saldo o el plan no está vigente)
create or replace function public.reservar_imagen(p_empresa uuid, p_origen text) returns uuid
language plpgsql security definer set search_path = public as $$
declare ok uuid; c uuid; r record;
begin
  -- Reservas que quedaron abiertas más de 20 minutos (el servidor se cayó a mitad): se devuelven
  for r in select id from consumos_imagenes
            where empresa_id = p_empresa and estado = 'reservada' and created_at < now() - interval '20 minutes' loop
    perform public.devolver_imagen(r.id);
  end loop;
  update empresas set fotos_disponibles = fotos_disponibles - 1
   where id = p_empresa and estado = 'activo' and coalesce(fotos_disponibles, 0) > 0
     and (plan_vence is null or plan_vence > now())
  returning id into ok;
  if ok is null then return null; end if;
  insert into consumos_imagenes (empresa_id, origen) values (p_empresa, left(coalesce(p_origen, ''), 40)) returning id into c;
  return c;
end $$;

-- 5. Confirmar (reserva abierta → confirmada; cuenta como usada)
create or replace function public.confirmar_imagen(p_consumo uuid) returns boolean
language plpgsql security definer set search_path = public as $$
declare e uuid;
begin
  update consumos_imagenes set estado = 'confirmada', confirmada_at = now()
   where id = p_consumo and estado = 'reservada' returning empresa_id into e;
  if e is null then return false; end if;
  update empresas set fotos_usadas = coalesce(fotos_usadas, 0) + 1 where id = e;
  return true;
end $$;

-- Solo el servidor (llave de servicio) las usa: nadie las llama desde el navegador
revoke execute on function public.reservar_imagen(uuid, text)  from public, anon, authenticated;
revoke execute on function public.confirmar_imagen(uuid)       from public, anon, authenticated;
revoke execute on function public.devolver_imagen(uuid)        from public, anon, authenticated;
grant  execute on function public.reservar_imagen(uuid, text)  to service_role;
grant  execute on function public.confirmar_imagen(uuid)       to service_role;
grant  execute on function public.devolver_imagen(uuid)        to service_role;

-- Comprobar
select nombre, precio, fotos_incluidas as imagenes_por_anio from public.planes order by precio;
