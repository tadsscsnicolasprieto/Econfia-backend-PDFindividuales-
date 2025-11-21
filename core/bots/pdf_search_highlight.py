# core/bots/pdf_search_highlight.py
import os
import re
import unicodedata
from datetime import datetime
import fitz  # PyMuPDF
from django.conf import settings

NOMBRE_SITIO = "pdf_search_highlight"

def _normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower().strip()

def _tokenize(q: str):
    # tokens de 3+ letras (quita signos y múltiple espacio)
    return [t for t in re.split(r"\W+", _normalize(q)) if len(t) >= 3]

def _buscar_en_pdf_y_resaltar_core(
    pdf_path: str,
    query: str,
    out_folder: str,
    export_first_if_none: bool = True,
    dpi: int = 150,
    stop_on_first: bool = False,   # corta en la primera página con match
    page_limit: int | None = None  # limitar páginas para pruebas / performance
):
    """
    IMPLEMENTACIÓN ORIGINAL (sin cambios de lógica):
    Busca 'query' en pdf_path de forma tolerante (sin acentos, case-insensitive y
    permitiendo variaciones de espacios). Si no hay match exacto del nombre completo,
    busca por tokens (nombres/apellidos) y resalta lo encontrado.
    Exporta PNGs con annots=True. Si no hay matches, exporta la página 1 como preview.
    Retorna lista de PNGs generados.
    """
    os.makedirs(out_folder, exist_ok=True)
    resultados = []

    q_norm = _normalize(query)
    tokens = _tokenize(query)
    full_pat = re.compile(r"\b" + re.sub(r"\s+", r"[\s\-]+", re.escape(q_norm)) + r"\b", re.IGNORECASE)

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"[PDF] Error abriendo PDF: {e}")
        return resultados

    total_pages = len(doc)
    print(f"[PDF] Abierto {pdf_path} con {total_pages} páginas")

    flags = 0
    for name in ("TEXT_IGNORECASE", "TEXT_DEHYPHENATE", "TEXT_PRESERVELIGATURES"):
        val = getattr(fitz, name, None)
        if isinstance(val, int):
            flags |= val

    last_page = total_pages if page_limit is None else min(page_limit, total_pages)
    hits_total = 0

    for i in range(last_page):
        page = doc[i]
        page_text = page.get_text("text") or ""
        page_text_norm = _normalize(page_text)

        full_match_found = False
        if q_norm:
            if full_pat.search(page_text_norm):
                full_match_found = True

        rects = []

        if full_match_found:
            r_full = page.search_for(query, hit_max=200, quads=False, flags=flags) if query else []
            if not r_full:
                q_simple = re.sub(r"\s+", " ", (query or "").strip())
                r_full = page.search_for(q_simple, hit_max=200, quads=False, flags=flags)
            rects.extend(r_full)

        if not rects and tokens:
            for tok in tokens:
                r_tok = page.search_for(tok, hit_max=200, quads=False, flags=flags)
                if not r_tok and tok != tok.title():
                    r_tok = page.search_for(tok.title(), hit_max=200, quads=False, flags=flags)
                if r_tok:
                    rects.extend(r_tok)

        if rects:
            print(f"[PDF] Match en página {i+1}: {len(rects)} hits")
            hits_total += len(rects)
            for r in rects:
                try:
                    page.add_highlight_annot(r)
                except Exception:
                    pass

            pix = page.get_pixmap(dpi=dpi, annots=True)
            base = os.path.splitext(os.path.basename(pdf_path))[0]
            out_png = os.path.join(out_folder, f"{base}_match_p{i+1}.png")
            pix.save(out_png)
            resultados.append(out_png)

            if stop_on_first:
                break
        else:
            if (i + 1) % 50 == 0 or i in (0, 1):
                print(f"[PDF] Página {i+1} sin match...")

    if hits_total == 0 and export_first_if_none and total_pages > 0:
        print("[PDF] Sin coincidencias, exportando preview de la p.1")
        pix0 = doc[0].get_pixmap(dpi=dpi, annots=True)
        base = os.path.splitext(os.path.basename(pdf_path))[0]
        out_png0 = os.path.join(out_folder, f"{base}_p1_preview.png")
        pix0.save(out_png0)
        resultados.append(out_png0)

    doc.close()
    return resultados


# ============================
# NUEVOS NOMBRES "consultar*"
# ============================

def consultar_buscar_en_pdf_y_resaltar_dj(
    cedula: str,
    pdf_path: str,
    query: str,
    export_first_if_none: bool = True,
    dpi: int = 150,
    stop_on_first: bool = False,
    page_limit: int | None = None,
):
    """
    Wrapper estilo plantilla (nombre con 'consultar'):
      - Guarda en MEDIA_ROOT/resultados/<cedula>/
      - Llama al core
      - Devuelve dict estándar con lista de archivos en 'archivos'
    """
    try:
        relative_folder = os.path.join("resultados", str(cedula))
        absolute_folder = os.path.join(settings.MEDIA_ROOT, relative_folder)
        os.makedirs(absolute_folder, exist_ok=True)

        archivos_abs = _buscar_en_pdf_y_resaltar_core(
            pdf_path=pdf_path,
            query=query,
            out_folder=absolute_folder,
            export_first_if_none=export_first_if_none,
            dpi=dpi,
            stop_on_first=stop_on_first,
            page_limit=page_limit,
        )

        # convertir a rutas relativas
        archivos_rel = []
        for ap in archivos_abs:
            if os.path.isabs(ap):
                try:
                    ap_rel = os.path.relpath(ap, settings.MEDIA_ROOT)
                except Exception:
                    ap_rel = ap
                archivos_rel.append(ap_rel)
            else:
                archivos_rel.append(ap)

        return {
            "sitio": NOMBRE_SITIO,
            "estado": "ok",
            "archivo": archivos_rel[0] if archivos_rel else "",
            "archivos": archivos_rel,
            "mensaje": "",
        }

    except Exception as e:
        return {
            "sitio": NOMBRE_SITIO,
            "estado": "error",
            "archivo": "",
            "archivos": [],
            "mensaje": str(e),
        }


def consultar_buscar_en_pdf_y_resaltar(
    pdf_path: str,
    query: str,
    out_folder: str,
    export_first_if_none: bool = True,
    dpi: int = 150,
    stop_on_first: bool = False,
    page_limit: int | None = None,
):
    """
    Versión “cruda” con nombre 'consultar*' para uso directo.
    Mismo comportamiento que la función legacy.
    """
    return _buscar_en_pdf_y_resaltar_core(
        pdf_path=pdf_path,
        query=query,
        out_folder=out_folder,
        export_first_if_none=export_first_if_none,
        dpi=dpi,
        stop_on_first=stop_on_first,
        page_limit=page_limit,
    )


# ====================================================
# ALIAS de compatibilidad (NO rompen el código legado)
# ====================================================

def buscar_en_pdf_y_resaltar_dj(
    cedula: str,
    pdf_path: str,
    query: str,
    export_first_if_none: bool = True,
    dpi: int = 150,
    stop_on_first: bool = False,
    page_limit: int | None = None,
):
    """Alias legacy que llama al nuevo nombre con 'consultar'."""
    return consultar_buscar_en_pdf_y_resaltar_dj(
        cedula=cedula,
        pdf_path=pdf_path,
        query=query,
        export_first_if_none=export_first_if_none,
        dpi=dpi,
        stop_on_first=stop_on_first,
        page_limit=page_limit,
    )


def buscar_en_pdf_y_resaltar(
    pdf_path: str,
    query: str,
    out_folder: str,
    export_first_if_none: bool = True,
    dpi: int = 150,
    stop_on_first: bool = False,
    page_limit: int | None = None,
):
    """Alias legacy que llama al nuevo nombre con 'consultar'."""
    return consultar_buscar_en_pdf_y_resaltar(
        pdf_path=pdf_path,
        query=query,
        out_folder=out_folder,
        export_first_if_none=export_first_if_none,
        dpi=dpi,
        stop_on_first=stop_on_first,
        page_limit=page_limit,
    )
