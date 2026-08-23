import io
import logging
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT

from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

MORADO     = HexColor("#7C3AED")
GRIS       = HexColor("#6B7280")
GRIS_CLARO = HexColor("#F3F4F6")
NEGRO      = HexColor("#111827")
VERDE      = HexColor("#059669")


def _formatear_cop(valor) -> str:
    return f"${valor:,.0f}".replace(",", ".")


def generar_pdf_cotizacion(
    numero: str,
    tienda_nombre: str,
    items: list,               # [{"nombre":, "cantidad":, "unidad":, "precio_unitario":, "subtotal":}]
    subtotal: float,
    descuento_pct: float = 0,
    descuento_valor: float = 0,
    total: float = 0,
    cliente_nombre: str = "",
    cliente_telefono: str = "",
    validez_dias: int = 15,
    notas: str = "",
    fecha: datetime = None,
) -> bytes:
    """
    Genera el PDF de una cotización (estimado de precio, SIN validez fiscal —
    no reemplaza factura electrónica DIAN). Reusa el mismo estilo visual que
    el recibo de pago (recibo_service.py) para mantener consistencia de marca.
    """
    fecha = fecha or datetime.now()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=2*cm, bottomMargin=2*cm, leftMargin=2*cm, rightMargin=2*cm)
    styles = getSampleStyleSheet()

    estilo_titulo = ParagraphStyle('TituloDeco', parent=styles['Title'], textColor=MORADO, fontSize=22, spaceAfter=2)
    estilo_sub    = ParagraphStyle('SubDeco', parent=styles['Normal'], textColor=GRIS, fontSize=11, spaceAfter=20)
    estilo_label  = ParagraphStyle('LabelDeco', parent=styles['Normal'], textColor=GRIS, fontSize=9)
    estilo_valor  = ParagraphStyle('ValorDeco', parent=styles['Normal'], textColor=NEGRO, fontSize=11)
    estilo_valor_der = ParagraphStyle('ValorDerDeco', parent=estilo_valor, alignment=TA_RIGHT)
    estilo_header_tabla = ParagraphStyle('HeaderTabla', parent=styles['Normal'], textColor=GRIS, fontSize=9, alignment=TA_RIGHT)
    estilo_header_tabla_izq = ParagraphStyle('HeaderTablaIzq', parent=styles['Normal'], textColor=GRIS, fontSize=9)
    estilo_total  = ParagraphStyle('TotalDeco', parent=styles['Title'], textColor=MORADO, fontSize=20, alignment=TA_RIGHT)
    estilo_footer = ParagraphStyle('FooterDeco', parent=styles['Normal'], textColor=GRIS, fontSize=8, alignment=TA_RIGHT)
    estilo_footer_aviso = ParagraphStyle('FooterAviso', parent=styles['Normal'], textColor=HexColor("#B45309"), fontSize=9)

    story = []
    story.append(Paragraph("DecoIArte", estilo_titulo))
    story.append(Paragraph(f"Cotización {numero}", estilo_sub))

    # ── Datos generales ────────────────────────────────────────────────
    filas_info = [
        ["Tienda", tienda_nombre],
        ["Fecha", fecha.strftime("%d/%m/%Y")],
        ["Válida hasta", f"{validez_dias} días desde la fecha de emisión"],
    ]
    if cliente_nombre:
        filas_info.append(["Cliente", cliente_nombre])
    if cliente_telefono:
        filas_info.append(["Teléfono", cliente_telefono])

    tabla_info = Table([[Paragraph(k, estilo_label), Paragraph(str(v), estilo_valor)] for k, v in filas_info], colWidths=[4*cm, 11*cm])
    tabla_info.setStyle(TableStyle([
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(tabla_info)
    story.append(Spacer(1, 20))

    # ── Tabla de ítems ──────────────────────────────────────────────────
    encabezado = [
        Paragraph("Producto", estilo_header_tabla_izq),
        Paragraph("Cant.", estilo_header_tabla),
        Paragraph("Unidad", estilo_header_tabla),
        Paragraph("Precio unit.", estilo_header_tabla),
        Paragraph("Subtotal", estilo_header_tabla),
    ]
    filas_items = [encabezado]
    for it in items:
        filas_items.append([
            Paragraph(str(it.get("nombre", "")), estilo_valor),
            Paragraph(str(it.get("cantidad", "")), estilo_valor_der),
            Paragraph(str(it.get("unidad", "") or "—"), estilo_valor_der),
            Paragraph(_formatear_cop(it.get("precio_unitario", 0)), estilo_valor_der),
            Paragraph(_formatear_cop(it.get("subtotal", 0)), estilo_valor_der),
        ])

    tabla_items = Table(filas_items, colWidths=[6*cm, 1.8*cm, 2.2*cm, 3*cm, 3*cm])
    tabla_items.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,0), 1, MORADO),
        ('LINEBELOW', (0,1), (-1,-1), 0.5, GRIS_CLARO),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(tabla_items)
    story.append(Spacer(1, 16))

    # ── Totales ─────────────────────────────────────────────────────────
    filas_totales = [["Subtotal", _formatear_cop(subtotal)]]
    if descuento_valor > 0:
        etiqueta_desc = f"Descuento ({descuento_pct:.0f}%)" if descuento_pct > 0 else "Descuento"
        filas_totales.append([etiqueta_desc, f"- {_formatear_cop(descuento_valor)}"])

    tabla_totales = Table(
        [[Paragraph(k, estilo_label), Paragraph(v, estilo_valor_der)] for k, v in filas_totales],
        colWidths=[12*cm, 3*cm]
    )
    tabla_totales.setStyle(TableStyle([('BOTTOMPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4)]))
    story.append(tabla_totales)
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Total: {_formatear_cop(total)}", estilo_total))
    story.append(Spacer(1, 4))
    story.append(Paragraph("COP", ParagraphStyle('cop', parent=estilo_footer)))

    if notas:
        story.append(Spacer(1, 20))
        story.append(Paragraph("Notas", estilo_label))
        story.append(Paragraph(notas, estilo_valor))

    story.append(Spacer(1, 34))
    story.append(Paragraph(
        "⚠ Este documento es una cotización / estimado de precio. NO constituye "
        "factura electrónica con validez fiscal ante la DIAN.",
        estilo_footer_aviso
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"decoiarte.com · Generado el {datetime.now().strftime('%d/%m/%Y %I:%M %p')}",
                            ParagraphStyle('footerCentro', parent=estilo_footer, alignment=1)))

    doc.build(story)
    return buffer.getvalue()


async def guardar_cotizacion_pdf(pdf_bytes: bytes, numero: str) -> str | None:
    """Sube el PDF de la cotización a Supabase Storage y devuelve su URL pública."""
    try:
        supabase = get_supabase()
        nombre_archivo = f"cotizaciones/{numero}.pdf"
        supabase.storage.from_("cotizaciones").upload(
            nombre_archivo, pdf_bytes,
            {"content-type": "application/pdf", "upsert": "true"}
        )
        return supabase.storage.from_("cotizaciones").get_public_url(nombre_archivo)
    except Exception as e:
        logger.error(f"Error subiendo cotización {numero} a Storage: {e}")
        return None