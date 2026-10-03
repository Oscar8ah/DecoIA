-- ═══════════════════════════════════════════════════════════════════════
--  ÍNDICES (PERF-05)                                             1 oct 2026
--  Solo columnas de consultas frecuentes REALES del código (conteo de filtros
--  .eq / .in en app/ y frontend/). Antes de crear cada uno se revisa si YA
--  existe un índice que empiece por esa columna (con cualquier nombre, o por
--  una restricción única): si existe, no se crea nada. Se puede correr dos veces.
-- ═══════════════════════════════════════════════════════════════════════
do $$
declare
  r record; existe boolean; tiene_col boolean;
  -- tabla, columna(s) del índice, por qué
  lista text[][] := array[
    ['pedidos',         'referencia',             'webhook de Wompi y cada acción sobre un pedido (13 consultas)'],
    ['pedidos',         'user_id',                'Mis pedidos del comprador y su política RLS'],
    ['pedidos',         'empresa_id',             'Mis ventas de la tienda'],
    ['productos',       'tienda_id',              'catálogo de cada tienda (14 consultas)'],
    ['tiendas',         'empresa_id',             'tienda de cada empresa y políticas RLS (8)'],
    ['empresas',        'email',                  'cada inicio de sesión busca la empresa por correo (19)'],
    ['consumidores',    'email',                  'perfil del comprador (10)'],
    ['notificaciones',  'empresa_id, created_at', 'campana de la tienda, ordenada por fecha'],
    ['imagenes_compra', 'user_id',                'imágenes del comprador y tope diario'],
    ['imagenes_compra', 'referencia',             'webhook de Wompi de las imágenes IA'],
    ['favoritos',       'user_id',                'favoritos del comprador']
  ];
  i int; tabla text; cols text; primera text;
begin
  for i in 1 .. array_length(lista, 1) loop
    tabla := lista[i][1]; cols := lista[i][2]; primera := trim(split_part(cols, ',', 1));
    select exists (select 1 from information_schema.columns
                   where table_schema = 'public' and table_name = tabla and column_name = primera) into tiene_col;
    if not tiene_col then
      raise notice 'Se omite %.% (la columna no existe)', tabla, primera; continue;
    end if;
    select exists (
      select 1 from pg_index x
      join pg_class t on t.oid = x.indrelid join pg_namespace n on n.oid = t.relnamespace
      join pg_attribute a on a.attrelid = t.oid and a.attnum = x.indkey[0]
      where n.nspname = 'public' and t.relname = tabla and a.attname = primera
    ) into existe;
    if existe then
      raise notice 'Ya existe un índice sobre %.% — no se crea', tabla, primera;
    else
      execute format('create index if not exists %I on public.%I (%s)', 'idx_' || tabla || '_' || replace(replace(cols, ', ', '_'), ' ', ''), tabla, cols);
      raise notice 'Creado: índice sobre %.% (%)', tabla, cols, lista[i][3];
    end if;
  end loop;
  -- Las políticas de seguridad comparan lower(email): índice de expresión
  if not exists (select 1 from pg_indexes where schemaname = 'public' and tablename = 'empresas'
                 and indexdef ilike '%lower(%email%') then
    create index idx_empresas_email_lower on public.empresas (lower(email));
    raise notice 'Creado: índice sobre lower(empresas.email) (RLS y guardias)';
  end if;
end $$;

-- Comprobar: índices de las tablas principales
select tablename as tabla, indexname as indice from pg_indexes
where schemaname = 'public' and tablename in ('pedidos','productos','tiendas','empresas','consumidores','notificaciones','imagenes_compra','favoritos')
order by 1, 2;
