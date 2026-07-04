# fix_visor.py - Ejecutar en C:\DECOIA.COM
with open('frontend/visor3d.html', encoding='utf-8') as f:
    html = f.read()

# 1. Agregar variables zoom/pan/resize si no existen
if 'let zoomPlanta' not in html:
    html = html.replace(
        'let modulosPlanta = [];',
        'let modulosPlanta = [];\nlet zoomPlanta=1,panX=0,panY=0,resizing=false,resizeModulo=null;'
    )
    print('✅ Variables zoom agregadas')
else:
    print('✅ Variables zoom ya existen')

# 2. Fix función cambiarVista - asegurar toolbarPlanta se muestra
old_cambiar = '''function cambiarVista(vista) {
    vistaActual=vista;
    document.getElementById('tab3d').classList.toggle('active',    vista==='3d');
    document.getElementById('tabFoto').classList.toggle('active',  vista==='foto');
    document.getElementById('tabPlanta').classList.toggle('active',vista==='planta');'''

new_cambiar = '''function cambiarVista(vista) {
    vistaActual=vista;
    document.getElementById('tab3d').classList.toggle('active',    vista==='3d');
    document.getElementById('tabFoto').classList.toggle('active',  vista==='foto');
    document.getElementById('tabPlanta').classList.toggle('active',vista==='planta');
    const tp = document.getElementById('toolbarPlanta');
    if(tp) tp.style.display = vista==='planta' ? 'flex' : 'none';'''

if old_cambiar in html:
    html = html.replace(old_cambiar, new_cambiar)
    print('✅ cambiarVista actualizado')
else:
    print('⚠️  cambiarVista no encontrado exacto — buscando alternativa')
    if 'toolbarPlanta' in html and 'cambiarVista' in html:
        print('✅ toolbarPlanta ya está en cambiarVista')

# 3. Fix iniciarEditorPlanta con setTimeout
old_iniciar = 'function iniciarEditorPlanta() {\n    const c = document.getElementById(\'canvasPlanta\');\n    if (!c) return;\n    const cont = c.parentElement;\n    c.width  = cont.offsetWidth  || window.innerWidth - 280;\n    c.height = cont.offsetHeight || window.innerHeight - 150;'

new_iniciar = '''function iniciarEditorPlanta() {
    const c = document.getElementById('canvasPlanta');
    if (!c) return;
    const cont = c.parentElement;
    setTimeout(() => {
        c.width  = cont.offsetWidth  || window.innerWidth - 280;
        c.height = cont.offsetHeight || window.innerHeight - 150;'''

if old_iniciar in html:
    # Buscar el cierre de iniciarEditorPlanta
    idx = html.find(old_iniciar)
    # Encontrar el siguiente bloque de eventos
    end_marker = 'c.ontouchend'
    idx_end = html.find(end_marker, idx)
    if idx_end > 0:
        # Encontrar el ; después de ontouchend
        idx_semi = html.find(';', idx_end)
        old_block = html[idx:idx_semi+1]
        new_block = old_block.replace(
            'function iniciarEditorPlanta() {\n    const c = document.getElementById(\'canvasPlanta\');\n    if (!c) return;\n    const cont = c.parentElement;\n    c.width  = cont.offsetWidth  || window.innerWidth - 280;\n    c.height = cont.offsetHeight || window.innerHeight - 150;',
            '''function iniciarEditorPlanta() {
    const c = document.getElementById('canvasPlanta');
    if (!c) return;
    const cont = c.parentElement;
    setTimeout(() => {
        c.width  = cont.offsetWidth  || (window.innerWidth - 300);
        c.height = cont.offsetHeight || (window.innerHeight - 160);'''
        )
        html = html[:idx] + new_block + ';\n    }, 150);\n}' + html[idx_semi+1:].replace('\n}', '', 1)
        print('✅ iniciarEditorPlanta con setTimeout')
else:
    print('⚠️  iniciarEditorPlanta ya tiene setTimeout o no encontrado')

with open('frontend/visor3d.html', 'w', encoding='utf-8') as f:
    f.write(html)

print('\n✅ Archivo guardado. Haz el push ahora.')