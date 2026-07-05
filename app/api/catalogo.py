import json
import logging
import base64
import io
import csv as csv_module

import fitz  # PyMuPDF
import httpx
import openpyxl
from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from app.utils.config import get_settings
from app.services.imagen_service import subir_imagen_a_imgbb

logger = logging.getLogger(__name__)
router = APIRouter(tags=["catalogo"])

# Modelo 3D que se usa por defecto para llamadas de extracción de catálogo.
# Se pasa explícitamente (no hardcodeado dentro del prompt) para poder
# actualizarlo en un solo lugar cuando cambie la versión recomendada.
MODELO_IA = "claude-sonnet-5"

# IDs reales del catálogo de mobiliario del Visor 3D (frontend/visor3d.html,
# constante CATALOGO_MUEBLES). Si se agregan muebles nuevos allá, agregarlos
# también aquí para que la IA los pueda usar como mapeo.
MODELOS_3D_DISPONIBLES = [
    "sofa", "mesa", "cama", "mesita", "escritorio", "sofa2", "mesa_cafe",
    "armario", "planta", "lampara_pie", "ventana_blanca", "ventana_negra",
    "ventana_balcon", "puerta", "hueco", "estufa", "cocina_integral",
    "inodoro", "ducha", "lavamanos", "toallero",
]

CATEGORIAS_VALIDAS = [
    "pisos", "enchapes", "cocinas", "baños", "materiales", "estructuras",
    "ferreteria", "electricos", "cables", "plomeria", "pintura", "puertas",
    "muebles", "electrodomesticos", "electronica", "jardineria", "seguridad", "otros",
]

MAX_PAGINAS_PDF   = 20   # límite de páginas a leer de un PDF (catálogos muy largos se truncan)
MAX_IMAGENES_PDF  = 25   # límite de imágenes a extraer y subir por catálogo
MAX_CHARS_TEXTO   = 12000  # límite de texto a mandar a la IA (antes eran solo 3000)


def _extraer_pdf(contenido: bytes):
    """Extrae texto por página e imágenes embebidas de un PDF real usando PyMuPDF."""
    doc = fitz.open(stream=contenido, filetype="pdf")
    texto_partes = []
    imagenes = []  # lista de bytes de imagen, en orden de aparición

    for num_pagina, pagina in enumerate(doc):
        if num_pagina >= MAX_PAGINAS_PDF:
            break
        texto_pagina = pagina.get_text().strip()

        # Extraer imágenes de la página (hasta el límite global)
        marcadores_pagina = []
        for img_info in pagina.get_images(full=True):
            if len(imagenes) >= MAX_IMAGENES_PDF:
                break
            try:
                xref = img_info[0]
                base_img = doc.extract_image(xref)
                imagenes.append(base_img["image"])  # bytes crudos (jpg/png)
                marcadores_pagina.append(f"[IMG_{len(imagenes) - 1}]")
            except Exception as e:
                logger.warning(f"No se pudo extraer una imagen del PDF: {e}")

        bloque = f"--- Página {num_pagina + 1} {' '.join(marcadores_pagina)} ---\n{texto_pagina}"
        texto_partes.append(bloque)

    doc.close()
    return "\n\n".join(texto_partes), imagenes


def _extraer_xlsx(contenido: bytes) -> str:
    wb = openpyxl.load_workbook(io.BytesIO(contenido), data_only=True)
    filas_texto = []
    for hoja in wb.worksheets:
        for fila in hoja.iter_rows(values_only=True):
            valores = [str(v) for v in fila if v is not None]
            if valores:
                filas_texto.append(" | ".join(valores))
    return "\n".join(filas_texto)


def _extraer_csv(contenido: bytes) -> str:
    texto = contenido.decode("utf-8", errors="ignore")
    lector = csv_module.reader(io.StringIO(texto))
    return "\n".join(" | ".join(fila) for fila in lector)


async def _llamar_ia_extraccion(texto: str, tiene_imagenes: bool, settings) -> dict:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=500,
            detail="Falta configurar ANTHROPIC_API_KEY en el backend (variables de entorno)."
        )

    instrucciones_imagen = (
        "Cada bloque de texto puede tener marcadores como [IMG_3] indicando que "
        "esa imagen (que verás más abajo, numerada igual) pertenece a ese producto. "
        "Si detectas la imagen de un producto, incluye su número en \"imagen_index\" "
        "(ej: 3). Si no hay imagen clara para ese producto, usa null."
        if tiene_imagenes else
        "Este catálogo no tiene imágenes adjuntas; deja \"imagen_index\" en null siempre."
    )

    prompt = f"""Eres un asistente que extrae productos de catálogos de tiendas de materiales de construcción, acabados y muebles para un marketplace de remodelación con IA.

Para cada producto que encuentres, extrae:
- nombre: nombre comercial del producto
- referencia: código o referencia del fabricante si aparece (si no hay, usa "")
- precio: número, sin símbolos de moneda ni puntos de miles (0 si no aparece)
- descripcion: 1-2 frases describiendo material, color, acabado, medidas si las hay
- categoria: EXACTAMENTE una de estas opciones: {', '.join(CATEGORIAS_VALIDAS)}
- unidad: m2, unidad, kg, litro o caja
- rendimiento_m2: si el catálogo menciona cuántos m² cubre una caja/unidad (ej: "1.44 m²/caja"), pon ese número. Si no aparece esa información, usa null — NO inventes un número.
- modelo_3d_tipo: a cuál de estos modelos 3D predeterminados corresponde este producto (para que se pueda dibujar en el visor 3D): {', '.join(MODELOS_3D_DISPONIBLES)}. Si el producto es un piso, enchape o pintura (no un mueble/objeto 3D), usa null. Si es un mueble/objeto pero ninguno de la lista se parece razonablemente, usa null — NUNCA inventes un id que no esté en la lista.
- imagen_index: {instrucciones_imagen}

Responde SOLO con JSON válido, sin texto antes ni después, con esta forma exacta:
{{"productos": [{{"nombre":"", "referencia":"", "precio":0, "descripcion":"", "categoria":"", "unidad":"", "rendimiento_m2": null, "modelo_3d_tipo": null, "imagen_index": null}}]}}

Catálogo a analizar:
{texto[:MAX_CHARS_TEXTO]}"""

    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODELO_IA,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    if response.status_code != 200:
        logger.error(f"Error llamando a Claude: {response.status_code} {response.text[:500]}")
        raise HTTPException(status_code=502, detail="Error consultando la IA de extracción.")

    data = response.json()
    texto_resp = (data.get("content") or [{}])[0].get("text", "{}")
    try:
        parsed = json.loads(texto_resp)
    except json.JSONDecodeError:
        inicio = texto_resp.find("{")
        fin = texto_resp.rfind("}")
        parsed = json.loads(texto_resp[inicio:fin + 1]) if inicio != -1 and fin != -1 else {"productos": []}
    return parsed


@router.post("/procesar-catalogo")
async def procesar_catalogo(archivo: UploadFile = File(...), tienda_id: str = Form(...)):
    """
    Recibe un catálogo (PDF, XLSX o CSV), extrae productos con IA (texto + fotos
    reales si es PDF), sube las fotos detectadas a imgbb, y mapea cada producto
    a un modelo 3D predeterminado del Visor 3D cuando aplica.
    Devuelve la lista de productos para previsualización — no los publica todavía.
    """
    settings = get_settings()
    contenido = await archivo.read()
    nombre_archivo = (archivo.filename or "").lower()

    try:
        imagenes: list[bytes] = []
        if nombre_archivo.endswith(".pdf"):
            texto, imagenes = _extraer_pdf(contenido)
        elif nombre_archivo.endswith(".xlsx") or nombre_archivo.endswith(".xls"):
            texto = _extraer_xlsx(contenido)
        elif nombre_archivo.endswith(".csv"):
            texto = _extraer_csv(contenido)
        else:
            raise HTTPException(status_code=400, detail="Formato no soportado. Usa PDF, XLSX o CSV.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error extrayendo contenido del archivo: {e}")
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo. ¿Está corrupto o protegido?")

    if not texto.strip():
        raise HTTPException(status_code=400, detail="No se encontró texto en el archivo.")

    parsed = await _llamar_ia_extraccion(texto, tiene_imagenes=bool(imagenes), settings=settings)
    productos = parsed.get("productos", [])

    # Subir a imgbb solo las imágenes que la IA sí asoció a un producto (ahorra llamadas)
    indices_usados = {p.get("imagen_index") for p in productos if p.get("imagen_index") is not None}
    urls_por_indice = {}
    for idx in indices_usados:
        if isinstance(idx, int) and 0 <= idx < len(imagenes):
            try:
                urls_por_indice[idx] = await subir_imagen_a_imgbb(imagenes[idx], settings.imgbb_api_key)
            except Exception as e:
                logger.warning(f"No se pudo subir imagen {idx} a imgbb: {e}")

    for p in productos:
        idx = p.get("imagen_index")
        p["imagen_url"] = urls_por_indice.get(idx) if isinstance(idx, int) else None
        if p.get("modelo_3d_tipo") not in MODELOS_3D_DISPONIBLES:
            p["modelo_3d_tipo"] = None
        if p.get("categoria") not in CATEGORIAS_VALIDAS:
            p["categoria"] = "otros"

    return {"productos": productos, "total_imagenes_detectadas": len(imagenes)}