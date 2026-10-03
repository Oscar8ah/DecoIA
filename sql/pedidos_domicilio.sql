-- ═══════════════════════════════════════════════════════════════════════
--  PEDIDOS CON DOMICILIO, ENVÍO, RECIBIDO Y PAGO A LA TIENDA  (28 sep 2026)
--  Correr UNA vez en Supabase → SQL Editor, ANTES de subir el código.
-- ═══════════════════════════════════════════════════════════════════════

-- 1. Nuevas columnas del pedido (todo en PESOS ENTEROS)
alter table public.pedidos
  add column if not exists domicilio               bigint,       -- lo que cotiza la tienda (0 a 50.000.000)
  add column if not exists domicilio_cotizado_at   timestamptz,
  add column if not exists costo_wompi             bigint,       -- estimado; lo asume DecoIArte
  add column if not exists neto_decoiarte          bigint,       -- comisión − costo Wompi (puede ser negativo)
  add column if not exists enviado_at              timestamptz,  -- desde aquí corren los 5 días hábiles
  add column if not exists recibido_at             timestamptz,
  add column if not exists liberado_at             timestamptz,  -- la tienda ya puede recibir su dinero
  add column if not exists liberado_por            text,         -- comprador | automatico | administrador
  add column if not exists pagado_tienda_at        timestamptz,  -- el administrador ya le transfirió
  add column if not exists comprobante_pago_tienda text;

-- 2. Estados nuevos (cotizando, por_pagar): si la tabla tenía una regla que
--    solo aceptaba los viejos, se quita (sin adivinar su nombre).
do $$
declare c record;
begin
  for c in select conname from pg_constraint
           where conrelid = 'public.pedidos'::regclass and contype = 'c'
             and pg_get_constraintdef(oid) ilike '%estado%'
  loop
    execute format('alter table public.pedidos drop constraint %I', c.conname);
  end loop;
end $$;

-- 3. Pedido mínimo de cada tienda (0 = sin mínimo)
alter table public.tiendas add column if not exists pedido_minimo bigint not null default 0;

-- 4. Producto "Prueba" de $1.000 en la tienda de oscar8a.cds@gmail.com
insert into public.productos (tienda_id, nombre, descripcion, precio, categoria, unidad, activo)
select t.id, 'Prueba', 'Producto de prueba del flujo de compra (domicilio, pago, envío y recibido)', 1000, 'materiales', 'unidad', true
from public.tiendas t
join public.empresas e on e.id = t.empresa_id
where lower(e.email) = 'oscar8a.cds@gmail.com'
  and not exists (select 1 from public.productos p where p.tienda_id = t.id and p.nombre = 'Prueba')
limit 1;

-- Comprobar: debe mostrar la tienda y el producto Prueba a $1.000
select t.nombre as tienda, p.nombre, p.precio, p.activo
from public.productos p join public.tiendas t on t.id = p.tienda_id
where p.nombre = 'Prueba';
