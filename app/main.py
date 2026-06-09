import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from app.api.whatsapp import router as whatsapp_router
from app.api.notificaciones import router as notificaciones_router

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
app.include_router(notificaciones_router)