import fitz
import os

def pdf_search(pdf_path, texto_busqueda, carpeta_salida):
    os.makedirs(carpeta_salida, exist_ok=True)
    doc = fitz.open(pdf_path)
    coincidencias_encontradas = False

    for num_pagina, pagina in enumerate(doc, start=1):
        # Buscar texto (case-insensitive)
        areas = pagina.search_for(texto_busqueda, quads=True)
        if areas:
            coincidencias_encontradas = True
            for idx, quad in enumerate(areas, start=1):
                pagina.add_highlight_annot(quad)

                rect = fitz.Rect(quad.rect)
                rect = rect + (-20, -20, 20, 20)

                pix = pagina.get_pixmap(clip=rect, dpi=150)
                nombre_img = os.path.join(carpeta_salida, f"pagina_{num_pagina}_match_{idx}.png")
                pix.save(nombre_img)


    if coincidencias_encontradas:
        return "Coincidencia encontrada"
    else:
        return "No se encontraron coincidencias"

