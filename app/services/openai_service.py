import logging
import base64
from openai import OpenAI
from app.utils.config import get_settings

logger = logging.getLogger(__name__)

# Motor central de DecoIArte
MOTOR_CENTRAL = """
Eres el motor central de DECOIARTE — sistema especializado en construcción, 
remodelación, visualización arquitectónica y cotización automática.

Tu objetivo es comprender espacios reales para ayudar a compradores, 
constructoras, arquitectos, remodeladores y proveedores.

ANÁLISIS DEL ESPACIO:
Identifica: pisos, muros, techos, puertas, ventanas, columnas, escaleras,
cocina, baño, sala, habitaciones, balcones, mobiliario, elementos decorativos.

Clasifica cada elemento como:
- EDITABLE: piso, paredes, pintura, enchapes
- NO EDITABLE: estructura, columnas, vigas
- DECORATIVO: cuadros, plantas, accesorios
- MOBILIARIO: muebles, electrodomésticos

CUANDO ANALICES UN PLANO:
- Identifica cada habitación y su función
- Estima áreas aproximadas
- Detecta distribución espacial
- Identifica zonas húmedas (baños, cocina)
- Detecta circulaciones y accesos

CUANDO ANALICES UNA FOTO:
- Detecta el tipo de espacio
- Identifica materiales actuales
- Segmenta zonas editables y no editables
- Sugiere mejoras específicas

Responde SIEMPRE en español.
Sé específico, profesional y útil.
"""


def get_openai_client() -> OpenAI:
    settings = get_settings()
    return OpenAI(api_key=settings.openai_api_key)


def imagen_a_base64(imagen_bytes: bytes) -> str:
    """Convierte imagen bytes a base64"""
    return base64.b64encode(imagen_bytes).decode("utf-8")


def analizar_espacio_foto(imagen_bytes: bytes) -> dict:
    """
    Analiza una foto de espacio con GPT-4o Vision
    Retorna tipo de espacio y elementos detectados
    """
    client = get_openai_client()
    imagen_b64 = imagen_a_base64(imagen_bytes)

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": MOTOR_CENTRAL
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{imagen_b64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": """Analiza esta imagen y responde en este formato exacto:

TIPO_ESPACIO: [sala/cocina/habitación/baño/oficina/local comercial/obra gris/otro]
ELEMENTOS_EDITABLES: [lista de elementos que se pueden remodelar]
ELEMENTOS_PROTEGIDOS: [lista de elementos que NO se deben tocar]
RECOMENDACION: [qué cambios tendrían más impacto visual]
PREGUNTA: [una sola pregunta corta para el cliente sobre qué quiere cambiar]"""
                        }
                    ]
                }
            ],
            max_tokens=500
        )

        respuesta = response.choices[0].message.content
        logger.info(f"Análisis de espacio: {respuesta[:100]}")

        # Parsear la respuesta
        resultado = {
            "tipo_espacio": "espacio",
            "elementos_editables": [],
            "elementos_protegidos": [],
            "recomendacion": "",
            "pregunta": "¿Qué te gustaría cambiar en este espacio?"
        }

        for linea in respuesta.split("\n"):
            if "TIPO_ESPACIO:" in linea:
                resultado["tipo_espacio"] = linea.split(":", 1)[1].strip()
            elif "RECOMENDACION:" in linea:
                resultado["recomendacion"] = linea.split(":", 1)[1].strip()
            elif "PREGUNTA:" in linea:
                resultado["pregunta"] = linea.split(":", 1)[1].strip()

        return resultado

    except Exception as e:
        logger.error(f"Error analizando espacio: {type(e).__name__} - {e}")
        return {
            "tipo_espacio": "espacio",
            "pregunta": "¿Qué te gustaría cambiar? (piso, paredes, iluminación)"
        }


def analizar_plano(imagen_bytes: bytes) -> dict:
    """
    Analiza un plano arquitectónico dibujado o impreso
    Identifica habitaciones, distribución y áreas
    """
    client = get_openai_client()
    imagen_b64 = imagen_a_base64(imagen_bytes)

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": MOTOR_CENTRAL
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{imagen_b64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": """Analiza este plano arquitectónico y responde:

TIPO_PLANO: [apartamento/casa/local/oficina/otro]
HABITACIONES: [lista cada habitación detectada]
AREAS_HUMEDAS: [baños y cocinas detectadas]
DISTRIBUCION: [descripción breve de la distribución]
AREA_ESTIMADA: [metros cuadrados aproximados si es posible]
PREGUNTA: [pregunta al cliente sobre qué espacio quiere visualizar primero]

Si la imagen NO es un plano arquitectónico, responde:
NO_ES_PLANO: true
SUGERENCIA: [indica amablemente que envíe un plano o foto del espacio]"""
                        }
                    ]
                }
            ],
            max_tokens=600
        )

        respuesta = response.choices[0].message.content
        logger.info(f"Análisis de plano: {respuesta[:100]}")

        resultado = {
            "es_plano": True,
            "tipo_plano": "plano",
            "habitaciones": [],
            "area_estimada": "Por determinar",
            "pregunta": "¿Qué espacio te gustaría visualizar primero?"
        }

        if "NO_ES_PLANO: true" in respuesta:
            resultado["es_plano"] = False
            return resultado

        for linea in respuesta.split("\n"):
            if "TIPO_PLANO:" in linea:
                resultado["tipo_plano"] = linea.split(":", 1)[1].strip()
            elif "AREA_ESTIMADA:" in linea:
                resultado["area_estimada"] = linea.split(":", 1)[1].strip()
            elif "PREGUNTA:" in linea:
                resultado["pregunta"] = linea.split(":", 1)[1].strip()
            elif "HABITACIONES:" in linea:
                habitaciones = linea.split(":", 1)[1].strip()
                resultado["habitaciones"] = habitaciones
            elif "DISTRIBUCION:" in linea:
                resultado["distribucion"] = linea.split(":", 1)[1].strip()

        return resultado

    except Exception as e:
        logger.error(f"Error analizando plano: {type(e).__name__} - {e}")
        return {
            "es_plano": True,
            "pregunta": "¿Qué espacio te gustaría visualizar primero?"
        }


def generar_visualizacion(prompt_usuario: str, url_imagen: str) -> str:
    """
    Genera una visualizacion de remodelacion usando OpenAI
    OWASP: validacion de entrada antes de enviar a la API
    """
    if not prompt_usuario or len(prompt_usuario) > 500:
        raise ValueError("Prompt invalido o demasiado largo")

    if not url_imagen.startswith("https://"):
        raise ValueError("URL de imagen invalida")

    client = get_openai_client()
    prompt_seguro = prompt_usuario.strip().replace("\n", " ")

    prompt_final = f"""
    Eres un experto en diseño de interiores y remodelacion.
    El cliente quiere visualizar su espacio con estos cambios: {prompt_seguro}
    Genera una descripcion detallada de como quedaria el espacio remodelado,
    mencionando materiales, colores y acabados especificos.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": MOTOR_CENTRAL
                },
                {
                    "role": "user",
                    "content": prompt_final
                }
            ],
            max_tokens=500,
            temperature=0.7
        )
        return response.choices[0].message.content

    except Exception as e:
        logger.error(f"Error en OpenAI API: {type(e).__name__}")
        raise RuntimeError("Error generando visualizacion")


def test_conexion_openai() -> bool:
    """Verifica que la conexion con OpenAI funciona"""
    try:
        client = get_openai_client()
        client.models.list()
        return True
    except Exception as e:
        logger.error(f"Error conectando OpenAI: {type(e).__name__}")
        return False