-- ═══════════════════════════════════════════════════════════════════════
--  MOVIMIENTOS DE DINERO DE CADA VENTA                           2 oct 2026
--  Correr UNA vez en Supabase → SQL Editor. Se puede correr dos veces.
--
--  Cada venta ya guarda por separado: total, comisión de DecoIArte, costo de
--  Wompi y lo que le toca a la tienda (comisión + Wompi + tienda = total).
--  Esta tabla registra lo que pasa DESPUÉS, una fila por movimiento:
--    · desembolso_tienda        → lo que se le transfiere a la tienda
--    · devolucion_comprador     → dinero que se le devuelve al comprador
--                                  (a_cargo_de: 'tienda' o 'decoiarte', lo decide el admin)
--    · descuento_administrativo → cualquier otro descuento a la tienda (retención,
--                                  ajuste…), con concepto y responsable. Nunca se
--                                  mezcla con la comisión ni con Wompi.
--  Estados: pendiente (aún no se hace) · realizado (con comprobante) · anulado.
--  Solo el servidor escribe aquí (llave de servicio, endpoints del Super Admin).
-- ═══════════════════════════════════════════════════════════════════════

create table if not exists public.movimientos_dinero (
  id              uuid primary key default gen_random_uuid(),
  created_at      timestamptz not null default now(),
  pedido_id       uuid not null references public.pedidos(id) on delete restrict,
  referencia      text not null,                     -- referencia del pedido (DECO-…)
  empresa_id      uuid references public.empresas(id),
  tipo            text not null check (tipo in ('desembolso_tienda','devolucion_comprador','descuento_administrativo')),
  monto           bigint not null check (monto > 0),  -- pesos enteros
  concepto        text not null check (length(trim(concepto)) >= 3),
  a_cargo_de      text check (a_cargo_de in ('tienda','decoiarte')),
  estado          text not null default 'pendiente' check (estado in ('pendiente','realizado','anulado')),
  comprobante     text,                              -- número de transferencia o de devolución en Wompi
  registrado_por  text not null,                     -- correo de quien lo registró
  realizado_at    timestamptz,
  anulado_motivo  text,
  clave           text unique,                       -- evita duplicados por doble clic o reintentos
  check (tipo <> 'devolucion_comprador' or a_cargo_de is not null)
);
create index if not exists idx_movimientos_pedido  on public.movimientos_dinero (pedido_id);
create index if not exists idx_movimientos_empresa on public.movimientos_dinero (empresa_id, created_at desc);
-- Un pedido tiene a lo sumo UN desembolso vigente (no anulado)
create unique index if not exists idx_un_desembolso_por_pedido on public.movimientos_dinero (pedido_id)
  where tipo = 'desembolso_tienda' and estado <> 'anulado';

-- Quién lee: el Super Admin todo; cada tienda solo los de sus pedidos. Nadie
-- escribe desde el navegador (no hay políticas de escritura).
alter table public.movimientos_dinero enable row level security;
drop policy if exists movimientos_leer on public.movimientos_dinero;
create policy movimientos_leer on public.movimientos_dinero for select
  using (empresa_id in (select public.mis_empresas()) or public.es_admin_o_servidor());

-- Fecha del reembolso total en el pedido (además del estado 'reembolsado')
alter table public.pedidos add column if not exists reembolsado_at timestamptz;

-- Comprobar
select count(*) as movimientos_registrados from public.movimientos_dinero;
