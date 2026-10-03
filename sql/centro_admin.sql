-- ═══════════════════════════════════════════════════════════════════════════
--  CENTRO DE ACTIVIDAD DEL SUPER ADMIN + AUDITORÍA  (30 sep 2026)
--  Correr UNA vez en Supabase → SQL Editor, ANTES de subir el código.
--
--  Idea: cada cosa importante que pasa en la base queda anotada en "eventos"
--  por DISPARADORES de la propia base. Así se registra venga de donde venga
--  (servidor, navegador o panel del admin) y sirve para CUALQUIER tienda o
--  comprador, no solo para las cuentas de prueba.
--
--  Seguridad: si un disparador falla por cualquier motivo, NUNCA bloquea la
--  compra, el registro o el pago (el error se ignora y la operación sigue).
-- ═══════════════════════════════════════════════════════════════════════════

-- 1. Tabla de eventos (solo la lee el servidor con su llave; el navegador no)
create table if not exists public.eventos (
  id          bigserial primary key,
  created_at  timestamptz not null default now(),
  tipo        text not null,
  nivel       text not null default 'info',     -- info | dinero | alerta
  titulo      text not null,
  detalle     jsonb not null default '{}'::jsonb,
  empresa_id  uuid,
  tienda_id   uuid,
  email       text,
  monto       bigint,
  referencia  text,
  leido       boolean not null default false
);
create index if not exists eventos_fecha_idx on public.eventos (created_at desc);
create index if not exists eventos_tipo_idx  on public.eventos (tipo);
alter table public.eventos enable row level security;   -- sin políticas: el navegador no la ve

-- 2. Pagos: guardar el número de transacción y el detalle (qué plan, desde cuál)
alter table public.pagos add column if not exists transaccion_id text;
alter table public.pagos add column if not exists detalle        jsonb;

-- 3. Función para anotar un evento (lee columnas con to_jsonb: no falla si alguna no existe)
create or replace function public.anotar_evento(p_tipo text, p_nivel text, p_titulo text, p_detalle jsonb,
  p_empresa uuid, p_tienda uuid, p_email text, p_monto bigint, p_ref text)
returns void language plpgsql security definer set search_path = public as $$
begin
  insert into public.eventos (tipo, nivel, titulo, detalle, empresa_id, tienda_id, email, monto, referencia)
  values (p_tipo, p_nivel, p_titulo, coalesce(p_detalle, '{}'::jsonb), p_empresa, p_tienda, p_email, p_monto, p_ref);
exception when others then null;   -- nunca bloquear la operación de negocio
end $$;

-- 4. Tiendas: registro nueva y cambios de plan/estado (del webhook o del panel)
create or replace function public.ev_empresas() returns trigger language plpgsql security definer set search_path = public as $$
declare n jsonb := to_jsonb(new); o jsonb; plan_a text; plan_b text;
begin
  begin
    if tg_op = 'INSERT' then
      perform anotar_evento('nueva_tienda', 'info', '🏪 Nueva tienda registrada: ' || coalesce(n->>'nombre','(sin nombre)'),
        jsonb_build_object('nombre', n->>'nombre', 'email', n->>'email', 'estado', n->>'estado'),
        (n->>'id')::uuid, null, n->>'email', null, null);
    else
      o := to_jsonb(old);
      if (n->>'plan_id') is distinct from (o->>'plan_id') then
        select nombre into plan_a from planes where id::text = o->>'plan_id';
        select nombre into plan_b from planes where id::text = n->>'plan_id';
        perform anotar_evento('plan_cambiado', 'dinero', '💎 ' || coalesce(n->>'nombre','Tienda') || ': plan ' || coalesce(plan_a,'ninguno') || ' → ' || coalesce(plan_b,'?'),
          jsonb_build_object('de', plan_a, 'a', plan_b, 'por', coalesce(auth.jwt()->>'email', 'servidor')),
          (n->>'id')::uuid, null, n->>'email', null, null);
      end if;
      if (n->>'estado') is distinct from (o->>'estado') then
        perform anotar_evento('estado_tienda', case when n->>'estado' = 'activo' then 'info' else 'alerta' end,
          '🏪 ' || coalesce(n->>'nombre','Tienda') || ': ' || coalesce(o->>'estado','?') || ' → ' || coalesce(n->>'estado','?'),
          jsonb_build_object('de', o->>'estado', 'a', n->>'estado', 'por', coalesce(auth.jwt()->>'email', 'servidor')),
          (n->>'id')::uuid, null, n->>'email', null, null);
      end if;
    end if;
  exception when others then null;
  end;
  return new;
end $$;
drop trigger if exists tr_ev_empresas on public.empresas;
create trigger tr_ev_empresas after insert or update on public.empresas for each row execute function public.ev_empresas();

-- 5. Compradores nuevos
create or replace function public.ev_consumidores() returns trigger language plpgsql security definer set search_path = public as $$
declare n jsonb := to_jsonb(new);
begin
  begin
    perform anotar_evento('nuevo_comprador', 'info', '🛒 Nuevo comprador: ' || coalesce(n->>'nombre', n->>'email', '(sin nombre)'),
      jsonb_build_object('nombre', n->>'nombre', 'email', n->>'email', 'ciudad', n->>'ciudad'), null, null, n->>'email', null, null);
  exception when others then null;
  end;
  return new;
end $$;
drop trigger if exists tr_ev_consumidores on public.consumidores;
create trigger tr_ev_consumidores after insert on public.consumidores for each row execute function public.ev_consumidores();

-- 6. Pedidos del marketplace: creado y cada cambio de estado
create or replace function public.ev_pedidos() returns trigger language plpgsql security definer set search_path = public as $$
declare n jsonb := to_jsonb(new); e text := n->>'estado'; tienda text; t text; niv text := 'info';
begin
  begin
    select nombre into tienda from tiendas where id::text = n->>'tienda_id';
    if tg_op = 'INSERT' then
      t := '🧾 Nuevo pedido: ' || coalesce(n->>'comprador_nombre','comprador') || ' → ' || coalesce(tienda,'tienda');
    elsif e is distinct from (to_jsonb(old)->>'estado') then
      t := case e
        when 'por_pagar' then '🚚 Domicilio cotizado: ' || coalesce(tienda,'tienda')
        when 'pagado'    then '💰 Venta pagada en ' || coalesce(tienda,'tienda')
        when 'enviado'   then '📦 Pedido despachado por ' || coalesce(tienda,'tienda')
        when 'entregado' then '✅ Pedido recibido (' || coalesce(tienda,'tienda') || ')'
        when 'cancelado' then '✖ Pedido cancelado (' || coalesce(tienda,'tienda') || ')'
        when 'revisar'   then '⚠️ Pedido para revisar (' || coalesce(tienda,'tienda') || ')'
        else '🧾 Pedido ' || coalesce(e,'?') end;
      niv := case when e = 'pagado' then 'dinero' when e in ('cancelado','revisar') then 'alerta' else 'info' end;
    else
      return new;
    end if;
    perform anotar_evento('pedido_' || coalesce(case when tg_op = 'INSERT' then 'nuevo' else e end, '?'), niv, t,
      jsonb_build_object('comprador', n->>'comprador_nombre', 'comprador_email', n->>'comprador_email', 'tienda', tienda,
        'subtotal', n->>'subtotal', 'domicilio', n->>'domicilio', 'total', n->>'total',
        'comision', n->>'comision_monto', 'para_tienda', n->>'monto_tienda', 'transaccion', n->>'transaccion_id', 'metodo', n->>'metodo_pago'),
      nullif(n->>'empresa_id','')::uuid, nullif(n->>'tienda_id','')::uuid, n->>'comprador_email',
      nullif(split_part(coalesce(n->>'total','0'), '.', 1), '')::bigint, n->>'referencia');
  exception when others then null;
  end;
  return new;
end $$;
drop trigger if exists tr_ev_pedidos on public.pedidos;
create trigger tr_ev_pedidos after insert or update on public.pedidos for each row execute function public.ev_pedidos();

-- 7. Imágenes IA vendidas
create or replace function public.ev_imagenes() returns trigger language plpgsql security definer set search_path = public as $$
declare n jsonb := to_jsonb(new);
begin
  begin
    if coalesce((n->>'pagada')::boolean, false) and not coalesce((to_jsonb(old)->>'pagada')::boolean, false) then
      perform anotar_evento('imagen_vendida', 'dinero', '🖼️ Imagen IA vendida', jsonb_build_object('email', n->>'email', 'transaccion', n->>'wompi_tx_id'),
        nullif(n->>'empresa_id','')::uuid, nullif(n->>'tienda_id','')::uuid, n->>'email', nullif(split_part(coalesce(n->>'monto','0'),'.',1),'')::bigint, n->>'referencia');
    end if;
  exception when others then null;
  end;
  return new;
end $$;
drop trigger if exists tr_ev_imagenes on public.imagenes_compra;
create trigger tr_ev_imagenes after update on public.imagenes_compra for each row execute function public.ev_imagenes();

-- 8. Solicitudes de plan con monto inválido (alguien pagó menos de lo debido)
create or replace function public.ev_solicitudes() returns trigger language plpgsql security definer set search_path = public as $$
declare n jsonb := to_jsonb(new);
begin
  begin
    if n->>'estado' = 'monto_invalido' and (to_jsonb(old)->>'estado') is distinct from 'monto_invalido' then
      perform anotar_evento('plan_monto_invalido', 'alerta', '⚠️ Pago de plan por MENOS de lo debido (no se activó)',
        jsonb_build_object('solicitud', n->>'id'), nullif(n->>'empresa_id','')::uuid, null, null, null, null);
    end if;
  exception when others then null;
  end;
  return new;
end $$;
drop trigger if exists tr_ev_solicitudes on public.solicitudes_plan;
create trigger tr_ev_solicitudes after update on public.solicitudes_plan for each row execute function public.ev_solicitudes();

-- 9. PLAN DE CADA TIENDA SIEMPRE AL DÍA (tiendas.plan_nombre)
--    Remodelar y el Visor 3D solo muestran tiendas con plan distinto de "gratis",
--    pero NINGÚN código escribía ese campo: las tiendas de prueba lo tenían
--    puesto a mano y una tienda NUEVA habría quedado invisible. Ahora la base lo
--    copia sola del plan de su empresa: al crear la tienda y cada vez que la
--    empresa cambia de plan (por Wompi, por el panel o a mano).
do $$
begin
  if exists (select 1 from information_schema.columns where table_schema='public' and table_name='tiendas'
             and column_name='plan_nombre' and is_generated='ALWAYS') then
    raise notice 'tiendas.plan_nombre es columna generada: no hace falta sincronizarla';
    return;
  end if;
  if not exists (select 1 from information_schema.columns where table_schema='public' and table_name='tiendas' and column_name='plan_nombre') then
    alter table public.tiendas add column plan_nombre text;
  end if;
end $$;

create or replace function public.tienda_toma_plan() returns trigger language plpgsql security definer set search_path = public as $$
begin
  select p.nombre into new.plan_nombre from empresas e join planes p on p.id = e.plan_id where e.id = new.empresa_id;
  return new;
exception when others then return new;
end $$;
drop trigger if exists tr_tienda_toma_plan on public.tiendas;
create trigger tr_tienda_toma_plan before insert or update of empresa_id on public.tiendas
  for each row execute function public.tienda_toma_plan();

create or replace function public.empresa_pasa_plan() returns trigger language plpgsql security definer set search_path = public as $$
begin
  if new.plan_id is distinct from old.plan_id then
    update tiendas t set plan_nombre = p.nombre from planes p where p.id = new.plan_id and t.empresa_id = new.id;
  end if;
  return new;
exception when others then return new;
end $$;
drop trigger if exists tr_empresa_pasa_plan on public.empresas;
create trigger tr_empresa_pasa_plan after update of plan_id on public.empresas
  for each row execute function public.empresa_pasa_plan();

-- Poner al día las tiendas que ya existen
update public.tiendas t set plan_nombre = p.nombre
from public.empresas e join public.planes p on p.id = e.plan_id
where e.id = t.empresa_id and t.plan_nombre is distinct from p.nombre;

-- 10. Las tiendas NUEVAS nacen activas (no se tocan las que ya existen:
--     si desactivaste una, sigue desactivada)
alter table public.tiendas alter column activa set default true;

-- Comprobar: tiendas con su dueño y su plan (dueño vacío = tienda sin cuenta)
select t.nombre as tienda, t.activa, t.plan_nombre, e.email as dueno
from public.tiendas t left join public.empresas e on e.id = t.empresa_id
order by e.email nulls first, t.nombre;

-- Comprobar: deben salir 7 disparadores
select event_object_table as tabla, trigger_name from information_schema.triggers
where trigger_name like 'tr_ev_%' or trigger_name like 'tr_tienda_%' or trigger_name like 'tr_empresa_%' group by 1, 2 order by 1;

-- 11. --  SEGURIDAD: las funciones del centro de actividad NO se pueden llamar desde
--  afuera (/rest/v1/rpc/...). Solo las usan los disparadores de la base.
--  Sin esto, cualquiera podía meter eventos falsos en el centro del admin.
-- ═══════════════════════════════════════════════════════════════════════
revoke execute on function public.anotar_evento(text, text, text, jsonb, uuid, uuid, text, bigint, text) from public, anon, authenticated;
revoke execute on function public.ev_empresas()       from public, anon, authenticated;
revoke execute on function public.ev_consumidores()   from public, anon, authenticated;
revoke execute on function public.ev_pedidos()        from public, anon, authenticated;
revoke execute on function public.ev_imagenes()       from public, anon, authenticated;
revoke execute on function public.ev_solicitudes()    from public, anon, authenticated;
revoke execute on function public.tienda_toma_plan()  from public, anon, authenticated;
revoke execute on function public.empresa_pasa_plan() from public, anon, authenticated;

-- Comprobar: debe salir "false" en las dos columnas para las 8 funciones
select p.proname as funcion,
       has_function_privilege('anon', p.oid, 'execute') as visitante_puede,
       has_function_privilege('authenticated', p.oid, 'execute') as usuario_puede
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname in ('anotar_evento','ev_empresas','ev_consumidores','ev_pedidos','ev_imagenes','ev_solicitudes','tienda_toma_plan','empresa_pasa_plan')
order by 1;
