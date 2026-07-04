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

app.include_router(whatsapp_router)
app.include_router(notificaciones_router)
app.include_router(render3d_router)
app.include_router(fondo_router)
app.include_router(wompi_router)   # ✅ NUEVO
app.include_router(catalogo_router)