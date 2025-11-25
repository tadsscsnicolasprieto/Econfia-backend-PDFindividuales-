import io
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from PyPDF2 import PdfMerger
from PIL import Image as PILImage
from django.conf import settings
import os

def generar_pdf_consolidado(resultados, consulta_id):
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()

    # 🔹 PDF resumen
    resumen_buffer = io.BytesIO()
    doc = SimpleDocTemplate(resumen_buffer, pagesize=A4)
    elements = [Paragraph(f"Reporte Consolidado - Consulta {consulta_id}", styles["Title"]), Spacer(1, 20)]
    data = [["Fuente", "Tipo", "Estado", "Score", "Mensaje"]]
    for r in resultados:
        data.append([r["fuente"], r["tipo_fuente"], r["estado"], str(r["score"]), r["mensaje"]])
    table = Table(data, colWidths=[100, 80, 100, 50, 200])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.grey),
        ("TEXTCOLOR", (0,0), (-1,0), colors.whitesmoke),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("GRID", (0,0), (-1,-1), 0.5, colors.black),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
    ]))
    elements.append(table)
    doc.build(elements)
    resumen_buffer.seek(0)

    # 🔹 Merge PDFs
    pdf_merger = PdfMerger()
    pdf_merger.append(resumen_buffer)

    for r in resultados:
        archivo = r.get("archivo")
        if not archivo:
            continue
        ext = os.path.splitext(archivo)[1].lower()
        try:
            if ext in [".png", ".jpg", ".jpeg"]:
                # Convertir la imagen directamente a PDF en memoria (más rápido que crear
                # un documento ReportLab por cada imagen). Pillow permite guardar como PDF.
                try:
                    img = PILImage.open(archivo).convert("RGB")
                    img_pdf_buffer = io.BytesIO()
                    img.save(img_pdf_buffer, format="PDF", resolution=150)
                    img_pdf_buffer.seek(0)
                    pdf_merger.append(img_pdf_buffer)
                except Exception:
                    # Fallback: intentar abrir y anexar como imagen embebida con ReportLab
                    img_buffer = io.BytesIO()
                    img = PILImage.open(archivo)
                    img.thumbnail((400, 400))
                    img.save(img_buffer, format="PNG")
                    img_buffer.seek(0)
                    img_pdf_buffer = io.BytesIO()
                    img_doc = SimpleDocTemplate(img_pdf_buffer, pagesize=A4)
                    img_elements = [Paragraph(f"Fuente: {r.get('fuente')}", styles.get("Heading3", styles["Normal"])),
                                    Spacer(1,10),
                                    Image(img_buffer),
                                    Spacer(1,20)]
                    img_doc.build(img_elements)
                    img_pdf_buffer.seek(0)
                    pdf_merger.append(img_pdf_buffer)

            elif ext == ".pdf":
                # Si ya es PDF, anexar directamente
                pdf_merger.append(archivo)
        except Exception:
            # ignorar archivos problemáticos y continuar
            continue

    final_buffer = io.BytesIO()
    pdf_merger.write(final_buffer)
    pdf_merger.close()
    final_buffer.seek(0)
    return final_buffer
