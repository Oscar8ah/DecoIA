import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.api.whatsapp import router as whatsapp_router
from app.api.notificaciones import router as notificaciones_router
from app.api.render3d import router as render3d_router
from app.api.fondo import router as fondo_router
from app.api.wompi import router as wompi_router   # ✅ NUEVO
from app.api.catalogo import router as catalogo_router
from app.api.planos import router as planos_router
from app.api.cotizacion import router as cotizacion_router
from app.api.solicitudes import router as solicitudes_router
from app.api.pedidos import router as pedidos_router
from app.api.video_ia import router as video_ia_router
from app.api.modelo3d import router as modelo3d_router
from app.api.superficies import router as superficies_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

app = FastAPI(
    title="DECOIA.COM",
    description="IA para remodelacion y diseno de interiores",
    version="0.1.0"
)

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://decoiarte.com",
        "https://www.decoiarte.com",
        "http://localhost:3000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ── CHEQUEO DE SALUD ──────────────────────────────────────────────────────
# Render entra a "/" para confirmar que el servicio está vivo. Sin esta ruta
# recibía 404, esperaba ~18 minutos y marcaba el deploy como FALLIDO, aunque
# la aplicación hubiera arrancado perfectamente.
# También sirve para el ping que mantiene despierto el servicio (y de paso
# Supabase, porque toca la base de datos).
@app.get("/")
@app.head("/")
async def salud():
    return {"servicio": "DecoIArte API", "estado": "ok"}


@app.get("/salud")
async def salud_detallada():
    """Chequeo que además toca la base de datos — ideal para el cron de uptime."""
    from app.utils.supabase_client import get_supabase
    try:
        get_supabase().table("planes").select("id").limit(1).execute()
        return {"estado": "ok", "base_datos": "conectada"}
    except Exception as e:
        logging.error(f"Chequeo de salud falló: {e}")
        return {"estado": "degradado", "base_datos": "sin conexión"}


app.include_router(whatsapp_router)
app.include_router(notificaciones_router)
app.include_router(render3d_router)
app.include_router(fondo_router)
app.include_router(wompi_router)   # ✅ NUEVO
app.include_router(catalogo_router)
app.include_router(planos_router)
app.include_router(cotizacion_router)
app.include_router(solicitudes_router)
app.include_router(pedidos_router)
app.include_router(video_ia_router)
app.include_router(modelo3d_router)
app.include_router(superficies_router)