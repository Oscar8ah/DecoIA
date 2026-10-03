-- ═══════════════════════════════════════════════════════════════════════
--  VIGENCIA ANUAL DE LOS PLANES                                  2 oct 2026
--  Correr UNA vez en Supabase → SQL Editor, ANTES de subir el código nuevo.
--  Se puede correr dos veces sin daño.
--
--  Regla: cada plan de pago es un pago único que cubre UN año.
--    · Al activarse (pago confirmado o activación del Super Admin): un año desde hoy.
--    · Renovar el mismo plan: suma un año desde el vencimiento (lo hace el servidor).
--    · Subir de plan con el plan vigente: conserva el vencimiento (lo hace el servidor).
--    · Al vencer: estado 'vencido'. La tienda SIGUE VENDIENDO (pedidos no depende
--      del plan) y conserva productos, pedidos e historial; solo se bloquean las
--      herramientas del plan, que exigen estado 'activo', hasta que renueve.
--    · El plan Gratis no vence.
-- ═══════════════════════════════════════════════════════════════════════

-- 1. Fechas del plan
alter table public.empresas add column if not exists plan_inicio timestamptz;
alter table public.empresas add column if not exists plan_vence timestamptz;
alter table public.empresas add column if not exists plan_aviso_vence boolean not null default false;   -- ya se avisó que está por vencer
create index if not exists idx_empresas_vence on public.empresas (plan_vence) where estado = 'activo';

-- 2. Quienes ya tienen un plan de pago activo: un año desde HOY (nadie pierde tiempo pagado)
update public.empresas e
   set plan_inicio = coalesce(e.plan_inicio, now()),
       plan_vence  = now() + interval '1 year'
  from public.planes p
 where p.id = e.plan_id and coalesce(p.precio, 0) > 0
   and e.estado = 'activo' and e.plan_vence is null;

-- 3. Fechas automáticas en CUALQUIER activación (también la manual del Super Admin)
create or replace function public.fechas_del_plan() returns trigger
language plpgsql security definer set search_path = public as $$
declare precio numeric; incluidas int;
begin
  select p.precio, p.fotos_incluidas into precio, incluidas from planes p where p.id = new.plan_id;
  if coalesce(precio, 0) = 0 then
    new.plan_vence := null;                -- Gratis no vence
  elsif new.estado = 'activo'
        and (tg_op = 'INSERT' or old.estado is distinct from 'activo')
        and (new.plan_vence is null or new.plan_vence <= now()) then
    new.plan_inicio := now();              -- se activa (o reactiva tras vencer) sin fecha vigente
    new.plan_vence  := now() + interval '1 year';
    -- Año nuevo = imágenes completas del plan (también al activar a mano desde el panel)
    new.fotos_disponibles := coalesce(incluidas, 0);
    new.fotos_usadas      := 0;
  end if;
  if tg_op = 'UPDATE' and new.plan_vence is distinct from old.plan_vence then
    new.plan_aviso_vence := false;         -- nueva vigencia: se podrá volver a avisar
  end if;
  return new;
end $$;
drop trigger if exists tr_fechas_del_plan on public.empresas;
create trigger tr_fechas_del_plan before insert or update on public.empresas
  for each row execute function public.fechas_del_plan();
revoke execute on function public.fechas_del_plan() from public, anon, authenticated;

-- 4. Vencer planes y avisar (15 días antes y el día que vence)
create or replace function public.vencer_planes() returns integer
language plpgsql security definer set search_path = public as $$
declare n integer := 0; r record;
begin
  -- Aviso previo, una sola vez por vigencia
  for r in update empresas set plan_aviso_vence = true
           where estado = 'activo' and plan_vence is not null and not plan_aviso_vence
             and plan_vence > now() and plan_vence <= now() + interval '15 days'
           returning id, plan_vence loop
    insert into notificaciones (empresa_id, tipo, titulo, mensaje, leida, datos)
    values (r.id, 'plan', '⏳ Tu plan está por vencer',
            'Tu plan vence el ' || to_char(r.plan_vence at time zone 'America/Bogota', 'DD/MM/YYYY')
            || '. Renuévalo desde Mi plan para no perder tus herramientas. Tu tienda sigue vendiendo igual.',
            false, jsonb_build_object('accion', 'renovar_plan'));
  end loop;
  -- Vencidos
  for r in update empresas set estado = 'vencido'
           where estado = 'activo' and plan_vence is not null and plan_vence <= now()
           returning id, nombre, email, plan_vence loop
    n := n + 1;
    insert into notificaciones (empresa_id, tipo, titulo, mensaje, leida, datos)
    values (r.id, 'plan', '⌛ Tu plan venció',
            'Tu tienda sigue vendiendo y conserva todo, pero las herramientas del plan quedan en pausa hasta que lo renueves desde Mi plan.',
            false, jsonb_build_object('accion', 'renovar_plan'));
    begin
      insert into eventos (tipo, nivel, titulo, empresa_id, email)
      values ('plan_vencido', 'info', '⌛ Plan vencido: ' || coalesce(r.nombre, r.email), r.id, r.email);
    exception when others then null;   -- la actividad del admin es opcional
    end;
  end loop;
  return n;
end $$;
revoke execute on function public.vencer_planes() from public, anon;
-- El dashboard la llama al abrir (así el vencimiento aplica al instante aunque
-- la tarea programada no haya corrido). Solo vence lo que ya está vencido.
grant execute on function public.vencer_planes() to authenticated;

-- 5. Tarea programada cada hora (si pg_cron está disponible en el proyecto)
do $$
begin
  begin
    create extension if not exists pg_cron;
  exception when others then
    raise notice 'pg_cron no disponible: actívalo en Database → Extensions. Mientras tanto, el dashboard vence los planes al abrir.';
    return;
  end;
  begin
    perform cron.unschedule('decoiarte-vencer-planes');
  exception when others then null;
  end;
  perform cron.schedule('decoiarte-vencer-planes', '7 * * * *', 'select public.vencer_planes()');
  raise notice 'Tarea programada: vencer planes cada hora';
end $$;

-- 6. El navegador NO puede tocar las fechas del plan (se suman a las guardias)
create or replace function public.guardia_empresas() returns trigger language plpgsql set search_path = public as $$
declare precio numeric;
begin
  if public.es_admin_o_servidor() then return coalesce(new, old); end if;
  if tg_op = 'DELETE' then raise exception 'No autorizado: no se puede borrar una empresa desde aquí'; end if;
  if tg_op = 'INSERT' then
    if lower(coalesce(new.email, '')) <> public.correo_sesion() then
      raise exception 'No autorizado: solo puedes registrar la empresa de tu propio correo';
    end if;
    select p.precio into precio from planes p where p.id = new.plan_id;
    if coalesce(precio, 0) > 0 then new.estado := 'pendiente_pago'; end if;
    new.plan_inicio := null; new.plan_vence := null;
    return new;
  end if;
  if lower(coalesce(old.email, '')) <> public.correo_sesion() then
    raise exception 'No autorizado: no puedes modificar otra empresa';
  end if;
  if new.plan_id is distinct from old.plan_id or new.estado is distinct from old.estado
     or new.fotos_disponibles is distinct from old.fotos_disponibles or new.fotos_usadas is distinct from old.fotos_usadas
     or new.email is distinct from old.email or new.whatsapp_phone_number_id is distinct from old.whatsapp_phone_number_id
     or new.plan_inicio is distinct from old.plan_inicio or new.plan_vence is distinct from old.plan_vence
     or new.plan_aviso_vence is distinct from old.plan_aviso_vence
     or (new.whatsapp_estado is distinct from old.whatsapp_estado and new.whatsapp_estado <> 'pendiente') then
    raise exception 'No autorizado: el plan, su vigencia, el estado, el cupo de fotos y la activación de WhatsApp solo los cambia DecoIArte';
  end if;
  return new;
end $$;

-- 7. Correr ahora una vez (vence lo que ya esté vencido)
select public.vencer_planes() as planes_vencidos_ahora;

-- Comprobar
select e.nombre, p.nombre as plan, e.estado, e.plan_inicio::date as inicio, e.plan_vence::date as vence
from public.empresas e left join public.planes p on p.id = e.plan_id
order by e.plan_vence nulls last limit 20;
