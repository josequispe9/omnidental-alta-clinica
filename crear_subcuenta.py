"""Paso 0 del alta: crea la subcuenta de GHL desde el navegador y devuelve su locationId.

La API de GHL no deja crear subcuentas con el plan actual (requiere Agency Pro), así que se automatiza
el formulario del panel. Se conecta a un Brave ya abierto y logueado en GHL, para no cargar credenciales:

  brave-browser --remote-debugging-port=9222

Uso:
  .venv/bin/python crear_subcuenta.py <slug>                 # crea la subcuenta e imprime el locationId
  .venv/bin/python crear_subcuenta.py <slug> --alta          # además corre alta.py con ese locationId
  .venv/bin/python crear_subcuenta.py <slug> --snapshot "Blank Snapshot"

Si ya existe una subcuenta con el nombre de la clínica, no crea otra: devuelve la existente.
"""
import argparse
import os
import re
import subprocess
import sys
import time

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(RAIZ, "ghl-alta"))
import ghl_oauth  # noqa: E402
from progreso import evento, resultado  # noqa: E402
import datos  # noqa: E402

APP = "https://app.gohighlevel.com"
CDP = os.getenv("BRAVE_CDP", "http://127.0.0.1:9222")
API = "https://services.leadconnectorhq.com"
PAISES = {"AR": "Argentina", "ES": "Spain", "MX": "Mexico", "CL": "Chile", "UY": "Uruguay", "CO": "Colombia",
          "PE": "Peru", "US": "United States"}


def leer_clinica(slug):
    c = datos.clinica(slug)
    if not c:
        sys.exit(f"No existe la clínica '{slug}' en la base (cargala desde el panel o con datos.py importar)")
    return c


def subcuenta_existente(nombre):
    """Busca por API (token de agencia) una subcuenta con ese nombre, para no duplicarla."""
    r = requests.get(f"{API}/locations/search", timeout=30, params={
        "companyId": os.environ["GHL_COMPANY_ID"], "limit": 100},
        headers={"Authorization": f"Bearer {os.environ['GHL_AGENCY_TOKEN']}", "Version": "2021-07-28"})
    r.raise_for_status()
    return next((l["id"] for l in r.json().get("locations", [])
                 if l["name"].strip().lower() == nombre.strip().lower()), None)


# ---------- helpers de la UI (Naive UI) ----------

def escribir(page, campo_id, valor):
    campo = page.locator(f"div#{campo_id} input").first
    campo.click()
    campo.fill(str(valor))


def campo_por_etiqueta(page, etiqueta):
    """Contenedor del campo cuya etiqueta es <etiqueta>. Hace falta porque GHL repite ids:
    "Business Niche" y "Time Zone" tienen los dos id="pCategories"."""
    return page.locator("#prospect_location_add_account_detail label").filter(
        has_text=re.compile(rf"^\s*{re.escape(etiqueta)}")).locator("xpath=..")


def elegir_opcion(page, campo, filtro, texto):
    """Dropdowns de Naive UI: abre, escribe para filtrar y elige la opción por su texto. El popup se dibuja
    al final del <body> y los de otros campos pueden seguir en el DOM: solo se miran las opciones visibles."""
    campo.locator(".n-base-selection").first.click()
    campo.locator("input").first.fill(filtro)
    opciones = page.locator(".n-base-select-option").filter(visible=True)
    exacta = opciones.filter(has_text=re.compile(rf"^\s*{re.escape(texto)}\s*$"))
    parcial = opciones.filter(has_text=texto)
    try:
        parcial.first.wait_for(timeout=10000)
    except PWTimeout:
        visibles = [t.strip() for t in opciones.all_inner_texts()[:10]]
        raise RuntimeError(f"No encontré la opción '{texto}'. Opciones visibles: {visibles}")
    (exacta if exacta.count() else parcial).first.click()


def crear_en_navegador(c, snapshot):
    p = c["perfil"]
    pais = PAISES.get(p["pais"], p["pais"])
    with sync_playwright() as pw:
        try:
            navegador = pw.chromium.connect_over_cdp(CDP)
        except Exception:
            sys.exit(f"No pude conectarme a Brave en {CDP}. Abrilo con: brave-browser --remote-debugging-port=9222 "
                     "e iniciá sesión en GHL")
        contexto = navegador.contexts[0] if navegador.contexts else navegador.new_context()
        page = contexto.new_page()
        page.set_default_timeout(30000)
        try:
            evento("subcuenta", 1, "Abrir Sub-Accounts", texto="[1] Abrir Sub-Accounts")
            page.goto(f"{APP}/accounts", wait_until="domcontentloaded")
            try:
                page.locator("button#header-list-btn").wait_for(timeout=30000)
            except PWTimeout:
                sys.exit(f"No veo el botón 'Create Sub-Account' (¿sesión de GHL iniciada en Brave?). URL: {page.url}")
            page.locator("button#header-list-btn").click()

            evento("subcuenta", 2, f"Elegir snapshot: {snapshot}", texto=f"[2] Snapshot: {snapshot}")
            # GHL tiene dos modales con id add-new-variation (uno oculto): se trabaja solo con lo visible
            buscador = page.get_by_placeholder("Snapshot name").filter(visible=True).first
            buscador.wait_for(timeout=30000)
            buscador.fill(snapshot)
            opcion = page.get_by_text(re.compile(rf"^\s*{re.escape(snapshot)}\s*$", re.I)).filter(visible=True).last
            opcion.wait_for(timeout=15000)
            opcion.hover(force=True)  # al pasar el mouse, la tarjeta muestra "Select and continue" (tapa el texto)
            seleccionar = page.get_by_role("button", name="Select and continue").filter(visible=True).first
            seleccionar.wait_for(timeout=10000)
            seleccionar.click()
            continuar = page.get_by_role("button", name="Continue", exact=True).filter(visible=True)
            try:
                page.wait_for_url("**/accounts/search**", timeout=8000)
            except PWTimeout:
                continuar.first.click()  # versión del modal con botón "Continue" aparte

            evento("subcuenta", 3, "Alta manual", texto="[3] Alta manual")
            page.wait_for_url("**/accounts/search**")
            page.locator("div#prospect_location_search a.add-manually").click()
            page.wait_for_url("**/accounts/add**")

            evento("subcuenta", 4, "Completar formulario", texto="[4] Completar formulario")
            page.locator("div#firstName input").wait_for()
            escribir(page, "firstName", p["contacto"]["nombre"])
            escribir(page, "lastName", p["contacto"]["apellido"])
            escribir(page, "email", p["contacto"]["email"])
            escribir(page, "name", c["clinica"])
            escribir(page, "pLocationPhone", p["telefono"])
            escribir(page, "plocationAddress", p["direccion"])
            escribir(page, "plocationCity", p["ciudad"])
            escribir(page, "plocationState", p["provincia"])
            elegir_opcion(page, campo_por_etiqueta(page, "Country"), pais[:4], pais)
            escribir(page, "plocationPostalCode", p["cp"])
            if p.get("web"):
                escribir(page, "plocationWebsite", p["web"])
            elegir_opcion(page, campo_por_etiqueta(page, "Time Zone"), p["timezone"], p["timezone"])

            evento("subcuenta", 5, "Crear subcuenta", texto="[5] Crear subcuenta")
            page.locator("button#submit").click()
            page.wait_for_url(re.compile(r".*/accounts/detail/[A-Za-z0-9]+"), timeout=60000)
            location_id = re.search(r"/accounts/detail/([A-Za-z0-9]+)", page.url).group(1)

            evento("subcuenta", 6, "Cambiar a la subcuenta", texto="[6] Cambiar a la subcuenta")
            page.locator("button#switch-account").click()
            page.wait_for_url(re.compile(r".*/v2/location/[A-Za-z0-9]+/"), timeout=60000)
            location_id = re.search(r"/v2/location/([A-Za-z0-9]+)/", page.url).group(1)
            return location_id
        except Exception:
            captura = os.path.join(RAIZ, "error_crear_subcuenta.png")
            page.screenshot(path=captura, full_page=True)
            evento("subcuenta", "error", f"Falló en {page.url}", nivel="error", captura=captura,
                   texto=f"Falló en {page.url}. Captura: {captura}")
            raise


def esperar_app_instalada(location_id, segundos=90):
    """El alta usa la app de Marketplace: hay que esperar a que GHL la instale en la subcuenta nueva."""
    fin = time.time() + segundos
    while time.time() < fin:
        try:
            ghl_oauth.token_subcuenta(location_id)
            return True
        except (RuntimeError, SystemExit):
            time.sleep(5)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="slug de la clínica en la base (datos.py listar)")
    ap.add_argument("--snapshot", default="Blank Snapshot", help="snapshot a elegir (por defecto Blank Snapshot)")
    ap.add_argument("--alta", action="store_true", help="al terminar, correr alta.py con el locationId")
    args = ap.parse_args()

    ghl_oauth.cargar_env(("GHL_AGENCY_TOKEN", "GHL_COMPANY_ID", "GHL_CLIENT_ID", "GHL_CLIENT_SECRET"))
    c = leer_clinica(args.slug)

    location_id = subcuenta_existente(c["clinica"])
    if location_id:
        evento("subcuenta", 6, f"Ya existe una subcuenta '{c['clinica']}': no se crea otra", nivel="aviso",
               texto=f"Ya existe una subcuenta '{c['clinica']}': no se crea otra.")
    else:
        location_id = crear_en_navegador(c, args.snapshot)
    evento("subcuenta", "id", f"Subcuenta: {location_id}", texto=f"\nlocationId: {location_id}")
    resultado(location_id=location_id)

    evento("subcuenta", "app", "Esperando que la app de Marketplace quede instalada en la subcuenta",
           texto="Esperando que la app de Marketplace quede instalada en la subcuenta...")
    if not esperar_app_instalada(location_id):
        sys.exit("La app no se instaló sola. Instalala en la subcuenta y corré: "
                 f".venv/bin/python alta.py {args.slug} --location {location_id}")
    evento("subcuenta", "app", "App instalada", texto="App instalada.")

    if args.alta:
        print(f"\n=== Corriendo alta.py {args.slug} --location {location_id}\n", flush=True)
        sys.exit(subprocess.call([sys.executable, os.path.join(RAIZ, "alta.py"), args.slug,
                                  "--location", location_id]))
    print(f"Siguiente paso: .venv/bin/python alta.py {args.slug} --location {location_id}")


if __name__ == "__main__":
    main()
