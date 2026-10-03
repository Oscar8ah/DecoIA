-- ═══════════════════════════════════════════════════════════════════════
--  SEGURIDAD: las funciones del centro de actividad NO se pueden llamar desde
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
