import io
import logging
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT

from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

MORADO   = HexColor("#7C3AED")
GRIS     = HexColor("#6B7280")
GRIS_CLARO = HexColor("#F3F4F6")


def _formatear_cop(valor) -> str:
    return f"${valor:,.0f}".replace(",", ".")


def generar_pdf_recibo(
    referencia: str, monto_cop: float, metodo: str,
    tienda_nombre: str, cliente_email: str = "",
    concepto: str = "Compra en marketplace DecoIArte",
    fecha: datetime = None,
) -> bytes:
    """
    Genera el PDF del comprobante de pago (no es factura electrónica DIAN,
    es un recibo/comprobante interno de DecoIArte con los datos reales
    de la transacción confirmada por Wompi).
    """
    fecha = fecha or datetime.now()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=2*cm, bottomMargin=2*cm, leftMargin=2*cm, rightMargin=2*cm)
    styles = getSampleStyleSheet()

    estilo_titulo = ParagraphStyle('TituloDeco', parent=styles['Title'], textColor=MORADO, fontSize=22, spaceAfter=2)
    estilo_sub    = ParagraphStyle('SubDeco', parent=styles['Normal'], textColor=GRIS, fontSize=11, spaceAfter=20)
    estilo_label  = ParagraphStyle('LabelDeco', parent=styles['Normal'], textColor=GRIS, fontSize=9)
    estilo_valor  = ParagraphStyle('ValorDeco', parent=styles['Normal'], textColor=HexColor("#111827"), fontSize=12)
    estilo_monto  = ParagraphStyle('MontoDeco', parent=styles['Title'], textColor=MORADO, fontSize=28, alignment=TA_CENTER)
    estilo_footer = ParagraphStyle('FooterDeco', parent=styles['Normal'], textColor=GRIS, fontSize=8, alignment=TA_CENTER)

    story = []
    story.append(Paragraph("DecolArte", estilo_titulo))
    story.append(Paragraph("Comprobante de pago", estilo_sub))

    story.append(Spacer(1, 10))
    story.append(Paragraph(_formatear_cop(monto_cop), estilo_monto))
    story.append(Spacer(1, 4))
    story.append(Paragraph("COP", ParagraphStyle('cop', parent=estilo_label, alignment=TA_CENTER)))
    story.append(Spacer(1, 24))

    filas = [
        ["Referencia", referencia],
        ["Fecha", fecha.strftime("%d/%m/%Y %I:%M %p")],
        ["Concepto", concepto],
        ["Tienda", tienda_nombre],
        ["Método de pago", metodo or "—"],
    ]
    if cliente_email:
        filas.append(["Cliente", cliente_email])

    tabla = Table([[Paragraph(k, estilo_label), Paragraph(str(v), estilo_valor)] for k, v in filas], colWidths=[5*cm, 10*cm])
    tabla.setStyle(TableStyle([
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('LINEBELOW', (0,0), (-1,-2), 0.5, GRIS_CLARO),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(tabla)

    story.append(Spacer(1, 40))
    story.append(Paragraph(
        "Este es un comprobante interno de pago generado automáticamente por DecolArte. "
        "No constituye factura electrónica con validez fiscal ante la DIAN.",
        estilo_footer
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"decoiarte.com · Generado el {datetime.now().strftime('%d/%m/%Y %I:%M %p')}", estilo_footer))

    doc.build(story)
    return buffer.getvalue()


async def guardar_recibo(pdf_bytes: bytes, referencia: str) -> str | None:
    """Sube el PDF del recibo a Supabase Storage y devuelve su URL pública."""
    try:
        supabase = get_supabase()
        nombre_archivo = f"recibos/{referencia}.pdf"
        supabase.storage.from_("recibos").upload(
            nombre_archivo, pdf_bytes,
            {"content-type": "application/pdf", "upsert": "true"}
        )
        return supabase.storage.from_("recibos").get_public_url(nombre_archivo)
    except Exception as e:
        logger.error(f"Error subiendo recibo {referencia} a Storage: {e}")
        return None