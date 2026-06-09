import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from app.api.whatsapp import router as whatsapp_router
from app.api.notificaciones import router as notificaciones_router
app.include_router(notificaciones_router)

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

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

app.include_router(whatsapp_router)


@app.get("/")
def root():
    return {
        "proyecto": "DECOIA.COM",
        "status": "activo",
        "version": "0.1.0"
    }


@app.get("/health")
def health():
    return {"status": "ok"}
from app.services.openai_service import test_conexion_openai

@app.get("/test-openai")
def test_openai():
    """Endpoint temporal para verificar conexion OpenAI - remover en produccion"""
    ok = test_conexion_openai()
    if ok:
        return {"status": "ok", "openai": "conectado"}
    return {"status": "error", "openai": "sin conexion"}