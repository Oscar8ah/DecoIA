import logging
from openai import OpenAI
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


def get_openai_client() -> OpenAI:
    settings = get_settings()
    return OpenAI(api_key=settings.openai_api_key)


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
                    "content": "Eres un experto en diseno de interiores para DECOIA.COM"
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