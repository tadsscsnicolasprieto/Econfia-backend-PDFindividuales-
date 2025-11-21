# core/utils/pdf_preview.py
import os
from typing import Tuple, Optional
from django.conf import settings

try:
    import fitz  # PyMuPDF
except Exception as e:
    raise RuntimeError("PyMuPDF (fitz) es requerido. Instala con: pip install pymupdf") from e


def pdf_first_page_to_png(
    pdf_abs_path: str,
    out_abs_path: Optional[str] = None,
    max_width: int = 1400
) -> Tuple[str, str]:
    """
    Convierte la primera página de un PDF a PNG.
    Devuelve (png_abs_path, png_rel_path) para guardar en BD, etc.

    - pdf_abs_path: ruta absoluta del PDF ya guardado en disco
    - out_abs_path: ruta absoluta destino del PNG; si no se pasa, se crea junto al PDF
    - max_width: ancho máximo en px (escala proporcional)
    """
    if not os.path.isfile(pdf_abs_path):
        raise FileNotFoundError(f"PDF no encontrado: {pdf_abs_path}")

    # si no dan ruta de salida, crear junto al PDF
    if not out_abs_path:
        base, _ = os.path.splitext(pdf_abs_path)
        out_abs_path = base + "_preview.png"

    # Render con PyMuPDF
    doc = fitz.open(pdf_abs_path)
    if doc.page_count == 0:
        doc.close()
        raise ValueError("El PDF no tiene páginas.")

    page = doc.load_page(0)  # primera página
    # Calcular zoom para respetar max_width
    rect = page.rect
    zoom = max_width / rect.width if rect.width > 0 else 1.5
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)  # PNG sin alpha para menor peso
    os.makedirs(os.path.dirname(out_abs_path), exist_ok=True)
    pix.save(out_abs_path)
    doc.close()

    # Construir ruta relativa (respecto a MEDIA_ROOT) para BD
    media_root = os.path.abspath(settings.MEDIA_ROOT)
    png_abs_real = os.path.abspath(out_abs_path)
    if png_abs_real.startswith(media_root):
        png_rel = os.path.relpath(png_abs_real, media_root)
    else:
        # fallback: si no cuelga de MEDIA_ROOT, devuelve solo el nombre
        png_rel = os.path.basename(png_abs_real)

    return png_abs_real, png_rel