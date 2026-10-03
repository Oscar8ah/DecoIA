-- ═══════════════════════════════════════════════════════════════════════
--  TIEMPO REAL EN PEDIDOS  (29 sep 2026)
--  Para que al comprador le llegue al instante la cotización del domicilio,
--  sin recargar la página. Correr UNA vez en Supabase → SQL Editor.
--  (Si falla o no se corre, la página igual se refresca sola cada 25 s.)
-- ═══════════════════════════════════════════════════════════════════════

-- 1. Publicar la tabla pedidos en tiempo real
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'pedidos'
  ) then
    alter publication supabase_realtime add table public.pedidos;
  end if;
end $$;

-- 2. Mandar la fila completa en cada cambio (si no, llega sin el total)
alter table public.pedidos replica identity full;

-- Comprobar: debe aparecer una fila con pedidos
select schemaname, tablename from pg_publication_tables
where pubname = 'supabase_realtime' and tablename = 'pedidos';
