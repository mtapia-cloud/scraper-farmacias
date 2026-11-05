"""
Scraper Farmacias Ahumada - v1

Corre sobre Salesforce Commerce Cloud (SFRA - Storefront Reference
Architecture), pero a diferencia de Cruz Verde, no expone una API JSON
propia: los resultados de búsqueda se renderizan como HTML del lado del
servidor. Por eso acá renderizamos con Playwright y parseamos el HTML
resultante con BeautifulSoup, apoyándonos en los mismos patrones que ya
vimos en el HTML de autocompletado:
    - [data-pid]          -> SKU del producto
    - [content]            -> precio numérico limpio (atributo content)
    - a[aria-label]        -> nombre del producto

Requisitos:
    pip install playwright beautifulsoup4 --break-system-packages
    playwright install chromium

"""

import json
import re
import sys
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


def _parse_precio_texto(texto: str):
    """Extrae el valor numérico de un texto tipo '$1.309' -> 1309.0
    (el formato chileno usa '.' como separador de miles, sin decimales).

    IMPORTANTE: se busca solo el PRIMER número que aparece justo después
    de un '$' - no se puede simplemente eliminar todo lo que no sea
    dígito del texto completo, porque el bloque de precio a veces incluye
    texto de cuotas/financiamiento (ej. "hasta 12 cuotas de $58.291 sin
    interés"), y hacerlo así concatenaría el precio real con esos otros
    números, generando valores absurdamente grandes."""
    if not texto:
        return None

    match = re.search(r"\$\s*([\d.]+)", texto)
    if not match:
        return None

    limpio = re.sub(r"[^0-9]", "", match.group(1))
    return float(limpio) if limpio else None


def _cerrar_popups(page):
    """
    Intenta cerrar banners de cookies/newsletter comunes en sitios SFCC.
    No falla el flujo si no encuentra nada.
    """
    selectores_comunes = [
        "#onetrust-accept-btn-handler",   # OneTrust (muy común)
        ".accept-cookies",
        "[aria-label*='cerrar' i]",
        "[aria-label*='close' i]",
        ".modal.show .close",
    ]
    for sel in selectores_comunes:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=2000)
                page.wait_for_timeout(500)
        except Exception:
            pass

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _parsear_html(html: str, query: str) -> list:
    """Parsea el HTML de una página de resultados y devuelve productos
    normalizados, etiquetados con el término de búsqueda que los encontró."""
    soup = BeautifulSoup(html, "html.parser")
    resultados = []

    tiles = soup.select("[data-pid]")
    vistos = set()

    for tile in tiles:
        pid = tile.get("data-pid")
        if not pid or pid in vistos:
            continue

        contenedor = tile
        nombre_el = None
        for _ in range(6):
            if contenedor is None:
                break
            nombre_el = (contenedor.select_one("img.tile-image[alt]")
                         or contenedor.select_one("a[aria-label]"))
            tiene_precio = contenedor.select_one(".sales") is not None
            if nombre_el and tiene_precio:
                break
            contenedor = contenedor.parent

        if not nombre_el:
            continue

        vistos.add(pid)
        nombre = (nombre_el.get("alt") or nombre_el.get("aria-label", "")).strip()
        # nombre_el es directamente el tag <img> cuando viene de la grilla
        # principal (tiene atributo src); si vino del fallback de
        # autocompletado (a[aria-label]) no tiene imagen, y queda None
        imagen_url = nombre_el.get("src")

        sales_el = contenedor.select_one(".sales")
        precio_venta = _parse_precio_texto(sales_el.get_text()) if sales_el else None

        list_el = contenedor.select_one("del [content], .strike-through [content]")
        try:
            precio_lista = float(list_el.get("content")) if list_el else precio_venta
        except (TypeError, ValueError):
            precio_lista = precio_venta

        if precio_venta is None:
            continue

        tiene_promocion = contenedor.select_one(".promotion-badge-container") is not None

        resultados.append({
            "farmacia": "ahumada",
            "producto_id": pid,
            "producto": nombre,
            "precio_lista": precio_lista,
            "precio_venta": precio_venta,
            "tiene_promocion": tiene_promocion,
            "imagen_url": imagen_url,
            "query_busqueda": query,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    return resultados


def buscar_multiples(queries: list, headless: bool = True,
                      timeout_ms: int = 20000, pausa_entre_terminos: float = 1.5):
    """
    Busca múltiples términos reutilizando el MISMO navegador - evita el
    costo de abrir/cerrar Chromium por cada búsqueda.
    """
    todos_resultados = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="es-CL",
        )
        page = context.new_page()

        for i, query in enumerate(queries):
            print(f"[INFO] ({i+1}/{len(queries)}) buscando '{query}'...")
            try:
                url_busqueda = f"https://www.farmaciasahumada.cl/search?q={query}"
                page.goto(url_busqueda, wait_until="domcontentloaded", timeout=timeout_ms)
                time.sleep(1.5)

                # El popup de cookies/newsletter suele aparecer solo la
                # primera vez - lo intentamos cerrar en cada iteración
                # igual, es barato y no falla si ya no está.
                _cerrar_popups(page)

                try:
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                except Exception:
                    pass

                html = page.content()
                resultados_termino = _parsear_html(html, query)

                if not resultados_termino:
                    debug_path = f"/app/output/ahumada_debug_{i}.html"
                    try:
                        with open(debug_path, "w", encoding="utf-8") as f:
                            f.write(html)
                        print(f"[WARN] sin resultados para '{query}' - "
                              f"HTML guardado en {debug_path}")
                    except Exception as e:
                        print(f"[WARN] sin resultados para '{query}' "
                              f"(no se pudo guardar debug: {e})")
                else:
                    todos_resultados.extend(resultados_termino)

            except Exception as e:
                print(f"[WARN] falló la búsqueda de '{query}', se omite: {e}")

            time.sleep(pausa_entre_terminos)

        browser.close()

    return todos_resultados


def buscar_ahumada(query: str, headless: bool = True, timeout_ms: int = 20000):
    """Compatibilidad con el modo de un solo término (uso manual/CLI)."""
    return buscar_multiples([query], headless=headless, timeout_ms=timeout_ms)


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "paracetamol"

    resultados = buscar_ahumada(query, headless=True)

    print(f"\nEncontrados {len(resultados)} productos:\n")
    print(json.dumps(resultados, indent=2, ensure_ascii=False))

    if not resultados:
        print("\n[INFO] No se encontraron productos. Revisa el archivo "
              "ahumada_debug.html generado en esta carpeta para ver qué "
              "cargó realmente la página, y compárteme un fragmento del "
              "HTML de un producto en la grilla de resultados.")