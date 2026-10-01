"""
Matriz única de permisos por plan (AUD-PLAN-003).

Regla comercial: TODOS los planes venden (tienda, productos, carrito, pedidos,
domicilio, notificaciones e historial no dependen del plan; app/api/pedidos.py
no consulta el plan). Los planes solo deciden las HERRAMIENTAS.

Toda validación de plan en el servidor debe salir de aquí, para que una
función nueva no se quede validada solo en el navegador (como pasó con la
Foto IA, AUD-PLAN-001). Las constantes que ya existían en planos.py,
video_ia.py, modelo3d.py y superficies.py coinciden con esta tabla.
"""

# Herramienta → planes que la incluyen (nombres de la tabla `planes`)
# (Copia fiel de lo que ya hacía cada módulo; el único cambio de regla es AR,
#  que pasa de solo Corporativo a "exclusiva de Premium" por decisión comercial.)
PERMISOS = {
    "foto_ia":        {"basico", "profesional", "premium", "corporativo"},   # transformar fotos con IA (render3d.py)
    "bot_whatsapp":   {"basico", "profesional", "premium", "corporativo"},   # bot de la tienda (dashboard)
    "editor_ia":      {"profesional", "premium", "corporativo"},             # quitar/cambiar objetos (superficies.py)
    "crm":            {"profesional", "premium", "corporativo"},
    "visor_3d":       {"premium", "corporativo"},
    "planos_ia":      {"premium", "corporativo"},                            # planos.py
    "modelos_3d":     {"premium", "corporativo"},                            # modelo3d.py (Meshy)
    "cotizador":      {"premium", "corporativo"},
    "multiasesor":    {"premium", "corporativo"},                            # solicitudes.py
    "realidad_aum":   {"premium", "corporativo"},    # AR: exclusiva desde Premium (Corporativo incluye todo Premium)
    "video_ia":       {"corporativo"},                                       # video_ia.py
}

# AR está construida (frontend/medir-ar.html, WebXR) pero NO se anuncia como
# disponible hasta validarla en dispositivos reales: el Home dice "Muy pronto".
AR_DISPONIBLE = False


def normalizar(plan: str | None) -> str:
    return (plan or "").strip().lower().replace("á", "a")


def plan_permite(plan: str | None, herramienta: str) -> bool:
    """¿El plan incluye la herramienta? Herramienta desconocida → no (falla cerrado)."""
    return normalizar(plan) in PERMISOS.get(herramienta, set())