"""OAuth de la app de Marketplace: un solo token de agencia para todas las subcuentas.

Uso:
  python ghl_oauth.py instalar               # muestra el link, espera el callback y guarda los tokens
  python ghl_oauth.py subcuentas             # lista las subcuentas donde la app está instalada
  python ghl_oauth.py probar <locationId>    # pide el token de esa subcuenta y hace una lectura de prueba

Desde otro script:
  from ghl_oauth import token_subcuenta
  token = token_subcuenta("<locationId>")

Lee GHL_CLIENT_ID y GHL_CLIENT_SECRET del .env de la carpeta principal (o de variables de entorno).
Guarda los tokens en .ghl_oauth.json (solo lectura para tu usuario). No lo subas al repositorio.
"""
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

BASE = "https://services.leadconnectorhq.com"
AQUI = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_TOKENS = os.path.join(AQUI, ".ghl_oauth.json")
REDIRECT_URI = os.getenv("GHL_REDIRECT_URI", "http://localhost:3000/callback")
PUERTO = urllib.parse.urlparse(REDIRECT_URI).port or 3000

# Tienen que estar marcados en la app (Advanced Settings > Auth). Se pueden cambiar con GHL_SCOPES.
SCOPES = os.getenv("GHL_SCOPES", " ".join([
    "oauth.readonly", "oauth.write",
    "locations.readonly", "locations.write",
    "users.readonly", "users.write",
    "calendars.readonly", "calendars.write",
    "calendars/events.readonly", "calendars/events.write",
    "locations/customValues.readonly", "locations/customValues.write",
    "opportunities.readonly", "opportunities.write",
    "contacts.readonly", "contacts.write",
    "conversations/message.write",
]))


def cargar_env(requeridas=("GHL_CLIENT_ID", "GHL_CLIENT_SECRET")):
    """Lee el .env de la carpeta principal (CLAVE=valor) sin pisar variables ya definidas."""
    ruta = os.path.join(AQUI, "..", ".env")
    if os.path.exists(ruta):
        with open(ruta) as f:
            for linea in f:
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    k, v = linea.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    faltan = [k for k in requeridas if not os.getenv(k)]
    if faltan:
        sys.exit(f"Faltan en el .env: {', '.join(faltan)}")


def guardar(datos):
    fd = os.open(ARCHIVO_TOKENS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(datos, f, indent=2)


def leer():
    if not os.path.exists(ARCHIVO_TOKENS):
        sys.exit("No hay tokens guardados. Corré primero: python ghl_oauth.py instalar")
    with open(ARCHIVO_TOKENS) as f:
        return json.load(f)


def pedir_token(**campos):
    """POST /oauth/token (form-urlencoded). Muestra el error de GHL tal cual si falla."""
    r = requests.post(f"{BASE}/oauth/token", timeout=30, headers={"Accept": "application/json"}, data={
        "client_id": os.environ["GHL_CLIENT_ID"],
        "client_secret": os.environ["GHL_CLIENT_SECRET"],
        **campos,
    })
    if r.status_code >= 400:
        raise RuntimeError(f"POST /oauth/token -> HTTP {r.status_code}: {r.text[:500]}")
    t = r.json()
    t["expira_en"] = int(time.time()) + int(t.get("expires_in", 0)) - 60  # margen de 1 minuto
    return t


# ---------- instalación ----------

def instalar():
    # Las apps nuevas exigen version_id en el link (si falta: "error.noAppVersionIdFound").
    # Por defecto es la parte del Client ID antes del guion; se puede fijar con GHL_APP_VERSION_ID.
    link = "https://marketplace.gohighlevel.com/v2/oauth/chooselocation?" + urllib.parse.urlencode({
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "client_id": os.environ["GHL_CLIENT_ID"],
        "scope": SCOPES,
        "version_id": os.getenv("GHL_APP_VERSION_ID", os.environ["GHL_CLIENT_ID"].split("-")[0]),
    })
    print("Abrí este link en el navegador, elegí la AGENCIA, marcá todas las subcuentas")
    print("(y que se instale en las futuras) y aceptá:\n")
    print(link + "\n")
    print(f"Esperando la respuesta de GHL en {REDIRECT_URI} ...", flush=True)

    resultado = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            resultado["code"] = (q.get("code") or [None])[0]
            resultado["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Listo, ya podés cerrar esta pestaña." if resultado["code"] else "No llegó el código."
            self.wfile.write(f"<p>{msg}</p>".encode())

        def log_message(self, *a):
            pass

    servidor = HTTPServer(("127.0.0.1", PUERTO), Callback)
    while "code" not in resultado:
        servidor.handle_request()
    servidor.server_close()

    if not resultado["code"]:
        sys.exit(f"GHL no devolvió el código (error: {resultado.get('error')})")

    t = pedir_token(grant_type="authorization_code", code=resultado["code"],
                    user_type="Company", redirect_uri=REDIRECT_URI)
    guardar({"agencia": t, "subcuentas": {}})
    print("\nInstalación OK.")
    print(f"  userType: {t.get('userType')}  companyId: {t.get('companyId')}")
    print(f"  scopes concedidos: {t.get('scope')}")
    print(f"  tokens guardados en {ARCHIVO_TOKENS}")


# ---------- tokens ----------

def token_agencia():
    datos = leer()
    t = datos["agencia"]
    if time.time() >= t["expira_en"]:
        # El refresh token se rota: hay que guardar el nuevo cada vez
        t = pedir_token(grant_type="refresh_token", refresh_token=t["refresh_token"], user_type="Company")
        datos["agencia"] = t
        guardar(datos)
    return t


def token_subcuenta(location_id):
    """Devuelve un access token válido para esa subcuenta, pidiéndolo o renovándolo si hace falta."""
    datos = leer()
    t = datos["subcuentas"].get(location_id)
    if t and time.time() < t["expira_en"]:
        return t["access_token"]
    if t and t.get("refresh_token"):
        try:
            t = pedir_token(grant_type="refresh_token", refresh_token=t["refresh_token"], user_type="Location")
        except RuntimeError:
            t = None  # si falla, se pide uno nuevo desde el token de agencia
    if not t or time.time() >= t["expira_en"]:
        t = _cambiar_por_token_subcuenta(location_id)
    datos = leer()
    datos["subcuentas"][location_id] = t
    guardar(datos)
    return t["access_token"]


def _cambiar_por_token_subcuenta(location_id):
    ag = token_agencia()
    cuerpo = {"companyId": ag["companyId"], "locationId": location_id}
    # La doc de GHL muestra dos variantes según la versión; se prueba la clásica y después la v3
    intentos = [("/oauth/locationToken", "2021-07-28"), ("/oauth/location-token", "v3")]
    errores = []
    for ruta, version in intentos:
        r = requests.post(f"{BASE}{ruta}", data=cuerpo, timeout=30, headers={
            "Authorization": f"Bearer {ag['access_token']}", "Version": version,
            "Accept": "application/json"})
        if r.status_code < 400:
            t = r.json()
            t["expira_en"] = int(time.time()) + int(t.get("expires_in", 0)) - 60
            return t
        errores.append(f"POST {ruta} (Version {version}) -> HTTP {r.status_code}: {r.text[:300]}")
    raise RuntimeError("No se pudo obtener el token de la subcuenta:\n  " + "\n  ".join(errores))


# ---------- comandos de prueba ----------

def subcuentas():
    ag = token_agencia()
    r = requests.get(f"{BASE}/oauth/installedLocations", timeout=30, params={
        "companyId": ag["companyId"], "appId": ag.get("appId") or os.environ["GHL_CLIENT_ID"].split("-")[0],
        "isInstalled": "true", "limit": 100,
    }, headers={"Authorization": f"Bearer {ag['access_token']}", "Version": "2021-07-28",
                "Accept": "application/json"})
    if r.status_code >= 400:
        sys.exit(f"GET /oauth/installedLocations -> HTTP {r.status_code}: {r.text[:500]}")
    for loc in r.json().get("locations", []):
        print(f"  {loc.get('_id') or loc.get('id')}  {loc.get('name')}  instalada={loc.get('isInstalled')}")


def probar(location_id):
    token = token_subcuenta(location_id)
    r = requests.get(f"{BASE}/locations/{location_id}", timeout=30, headers={
        "Authorization": f"Bearer {token}", "Version": "2021-07-28", "Accept": "application/json"})
    print(f"GET /locations/{location_id} -> HTTP {r.status_code}")
    if r.ok:
        print(f"  nombre: {r.json()['location']['name']}")
    else:
        print(f"  {r.text[:300]}")


if __name__ == "__main__":
    cargar_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "instalar":
        instalar()
    elif cmd == "subcuentas":
        subcuentas()
    elif cmd == "probar" and len(sys.argv) > 2:
        probar(sys.argv[2])
    else:
        sys.exit(__doc__)
