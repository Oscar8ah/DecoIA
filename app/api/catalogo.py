import json
import logging
import base64
import io
import csv as csv_module
from urllib.parse import urljoin, urlparse

import fitz  # PyMuPDF
import httpx
import openpyxl
from bs4 import BeautifulSoup
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

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
    "tv", "nevera", "lavadora", "microondas", "tapete", "luz_techo",
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


MAX_IMAGENES_URL   = 20
TAMANO_MIN_IMAGEN_URL = 150  # px — filtra iconos/logos chiquitos declarados en el HTML

async def _extraer_url(url: str):
    """
    Extrae texto e imágenes de una página de tienda existente (ej: su web en
    Shopify, WooCommerce, o una página armada a mano), en el mismo formato
    que usa el PDF: texto con marcadores [IMG_n] cerca de cada imagen, en el
    orden en que aparecen en la página.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"No se pudo abrir ese link (código {resp.status_code}). ¿Es público?")
        html = resp.text

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        tag.decompose()

    imagenes_bytes = []
    partes_texto = []

    async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
        for el in soup.body.find_all(True) if soup.body else []:
            if el.name == "img":
                if len(imagenes_bytes) >= MAX_IMAGENES_URL:
                    continue
                src = el.get("src") or el.get("data-src") or (el.get("srcset") or "").split(" ")[0]
                if not src:
                    continue
                try:
                    ancho = int(el.get("width", 0) or 0)
                    alto  = int(el.get("height", 0) or 0)
                    if 0 < ancho < TAMANO_MIN_IMAGEN_URL or 0 < alto < TAMANO_MIN_IMAGEN_URL:
                        continue  # probablemente un ícono/logo, no foto de producto
                except ValueError:
                    pass
                url_img = urljoin(url, src)
                if urlparse(url_img).scheme not in ("http", "https"):
                    continue
                try:
                    r = await client.get(url_img)
                    if r.status_code == 200 and len(r.content) > 2000:  # descarta pixeles de tracking
                        imagenes_bytes.append(r.content)
                        alt = (el.get("alt") or "").strip()
                        partes_texto.append(f"[IMG_{len(imagenes_bytes) - 1}{': ' + alt if alt else ''}]")
                except Exception as e:
                    logger.warning(f"No se pudo descargar imagen {url_img}: {e}")
            else:
                texto_el = el.get_text(" ", strip=True) if el.name in ("p","span","div","h1","h2","h3","h4","li","td","a","strong","b") else ""
                if texto_el and (not el.find(True)):  # solo nodos "hoja" para no repetir texto anidado
                    partes_texto.append(texto_el)

    texto_final = "\n".join(partes_texto)
    return texto_final, imagenes_bytes


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
- modelo_3d_tipo: SOLO si el producto tiene sentido VERLO ubicado dentro de un cuarto en un render 3D (muebles, electrodomésticos grandes como nevera/lavadora/tv/microondas, ventanas, puertas, tapetes, luces). Si aplica, usa exactamente uno de estos IDs: {', '.join(MODELOS_3D_DISPONIBLES)}. Para TODO lo demás (tornillos, cables, herramientas, materiales sueltos, pisos, enchapes, pintura, perfiles metálicos, varillas, accesorios pequeños) usa null — la mayoría de productos de un catálogo de ferretería NO necesitan modelo 3D, solo se venden con su foto real en el marketplace. Si es un mueble/objeto pero ninguno de la lista se parece razonablemente, usa null — NUNCA inventes un id que no esté en la lista.
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


async def _post_procesar_productos(productos: list, imagenes: list, settings) -> dict:
    """Sube a imgbb solo las fotos que la IA sí asoció a un producto, y valida categoría/modelo_3d_tipo."""
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
    return await _post_procesar_productos(productos, imagenes, settings)


class CatalogoUrlRequest(BaseModel):
    url: str
    tienda_id: str


@router.post("/procesar-catalogo-url")
async def procesar_catalogo_url(data: CatalogoUrlRequest):
    """
    Recibe el link de una tienda existente (Shopify, WooCommerce, página propia),
    lee su contenido público y extrae productos con IA, igual que con un PDF.
    """
    settings = get_settings()
    if not data.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="El link debe empezar con http:// o https://")

    try:
        texto, imagenes = await _extraer_url(data.url)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error leyendo la URL {data.url}: {e}")
        raise HTTPException(status_code=400, detail="No se pudo leer esa página. Verifica que el link sea público y correcto.")

    if not texto.strip():
        raise HTTPException(status_code=400, detail="No se encontró texto de productos en esa página.")

    parsed = await _llamar_ia_extraccion(texto, tiene_imagenes=bool(imagenes), settings=settings)
    productos = parsed.get("productos", [])
    return await _post_procesar_productos(productos, imagenes, settings)