"""Dos colas que procesan de a una clínica: extracción (extraer_web.py) y alta (crear_subcuenta.py --alta).

Cada tarea corre el script de siempre en un proceso aparte con ALTA_EVENTOS=json. Las líneas JSON son eventos de
avance; el resto (avisos, errores, tracebacks) se guarda como detalle. Si el script termina con error, la clínica
queda en error con las últimas líneas de la salida y se puede reintentar.
"""
import json
import os
import subprocess
import sys
import threading
import time

import requests

from . import db

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = sys.executable
BORRADORES = os.path.join(RAIZ, "borradores", "panel")
CDP = os.getenv("BRAVE_CDP", "http://127.0.0.1:9222")

# avance (%) por etapa y paso
PASOS_GHL = ["inicio", "0", "1", "2", "2a", "2b", "3", "4", "4b", "5", "6"]
AVANCE_ALTA = {("subcuenta", "1"): 3, ("subcuenta", "2"): 6, ("subcuenta", "3"): 9, ("subcuenta", "4"): 12,
               ("subcuenta", "5"): 16, ("subcuenta", "6"): 20, ("subcuenta", "id"): 22, ("subcuenta", "app"): 25,
               **{("ghl", p): 30 + i * 4 for i, p in enumerate(PASOS_GHL)},
               ("retell", "inicio"): 76, ("retell", "R0"): 80, ("retell", "R1"): 85, ("retell", "R2"): 90,
               ("retell", "R3"): 95, ("retell", "R4"): 98, ("fin", "ok"): 100}
NOMBRE_ETAPA = {"extraccion": "Extracción de la web", "subcuenta": "Creación de la subcuenta (Brave)",
                "ghl": "Configuración de GHL", "retell": "Agente de Retell", "fin": "Fin"}


def avance_extraccion(ev):
    p, d = ev["paso"], ev.get("datos") or {}
    if p == "pagina" and d.get("n"):
        return 10 + 55 * d["i"] / d["n"]
    return {"sitio": 4, "sitemap": 8, "recorrido": 68, "claude": 72, "borrador": 100}.get(p)


def correr(cid, args, etapa_inicial, calc_avance):
    """Corre un script y vuelca sus eventos en la base. Devuelve (ok, resultado, ultimas_lineas)."""
    env = {**os.environ, "ALTA_EVENTOS": "json", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([PY, *args], cwd=RAIZ, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    resultado, ultimas, etapa = {}, [], etapa_inicial
    for linea in proc.stdout:
        linea = linea.rstrip("\n")
        if not linea.strip():
            continue
        try:
            ev = json.loads(linea) if linea.startswith("{") else None
        except json.JSONDecodeError:
            ev = None
        if ev and ev.get("tipo") == "resultado":
            resultado.update(ev.get("datos") or {})
            continue
        if ev and ev.get("tipo") == "evento":
            etapa = ev["etapa"]
            db.agregar_evento(cid, etapa, ev["paso"], ev["nivel"], ev["mensaje"], ev.get("ts"))
            campos = {"etapa": etapa, "paso": ev["paso"], "mensaje": ev["mensaje"]}
            av = calc_avance(ev)
            if av is not None:
                campos["avance"] = av
            if (ev.get("datos") or {}).get("captura"):
                campos["captura"] = ev["datos"]["captura"]
            db.actualizar(cid, **campos)
            continue
        # salida suelta del script (avisos, sys.exit, tracebacks)
        ultimas = (ultimas + [linea])[-8:]
        db.agregar_evento(cid, etapa, "detalle", "detalle", linea[:1000])
    proc.wait()
    return proc.returncode == 0, resultado, ultimas


def mensaje_error(ultimas, codigo_ok):
    utiles = [l for l in ultimas if not l.startswith((" ", "Traceback"))]
    return (utiles[-1] if utiles else ultimas[-1] if ultimas else "El proceso terminó con error")[:500]


# ---------- extracción ----------

def extraer(cid):
    fila = db.obtener(cid)
    base = os.path.join(BORRADORES, f"clinica-{cid}")
    db.actualizar(cid, etapa="extraccion", paso="inicio", mensaje="Arrancando la extracción", avance=1)
    db.agregar_evento(cid, "extraccion", "inicio", "info", f"Extraer {fila['url']}")
    ok, res, ultimas = correr(cid, ["extraer_web.py", fila["url"], "--salida", base], "extraccion",
                              avance_extraccion)
    if not ok or not res.get("borrador"):
        err = mensaje_error(ultimas, ok)
        db.agregar_evento(cid, "extraccion", "error", "error", err)
        db.actualizar(cid, estado=db.ERROR_EXTRACCION, error=err, mensaje=err)
        return
    cargar_borrador(cid, res["borrador"])


def cargar_borrador(cid, ruta):
    """Pasa el borrador de extraer_web.py a la base: una fila por sede, lista para revisar."""
    fila = db.obtener(cid)
    with open(ruta) as f:
        borrador = json.load(f)
    if not borrador["clinicas"]:
        err = "La web no tiene datos de una clínica (Claude no encontró ninguna sede)"
        db.agregar_evento(cid, "extraccion", "error", "error", err)
        db.actualizar(cid, estado=db.ERROR_EXTRACCION, error=err, mensaje=err)
        return
    comun = {"revision": borrador["revisar"], "fuentes": borrador["fuentes"], "extra": borrador["extra"]}
    for i, clinica in enumerate(borrador["clinicas"]):
        clinica["slug"] = db.datos.slug_disponible(clinica["slug"])  # otra extracción de la misma web, etc.
        campos = {**comun, "datos": clinica, "slug": clinica["slug"], "nombre": clinica["clinica"],
                  "estado": db.POR_REVISAR, "etapa": "revision", "paso": "", "avance": 100,
                  "mensaje": "Lista para revisar" + (f" (sede {i + 1} de {len(borrador['clinicas'])})"
                                                     if len(borrador["clinicas"]) > 1 else ""),
                  "error": None}
        if i == 0:
            db.actualizar(cid, **campos)
        else:
            db.crear_sede(cid, fila["url"], **campos)
    db.agregar_evento(cid, "extraccion", "fin", "info",
                      f"Extracción lista: {len(borrador['clinicas'])} clínica(s) para revisar")


# ---------- alta ----------

def brave_abierto():
    try:
        return requests.get(f"{CDP}/json/version", timeout=3).ok
    except requests.RequestException:
        return False


def alta(cid):
    fila = db.obtener(cid)
    db.actualizar(cid, etapa="subcuenta", paso="inicio", mensaje="Arrancando el alta", avance=1)
    db.agregar_evento(cid, "subcuenta", "inicio", "info", f"Alta de {fila['nombre']} ({fila['slug']})")
    if not brave_abierto():
        err = "Brave no está abierto con --remote-debugging-port=9222 (o no se puede conectar). Abrilo, iniciá sesión en GHL y reintentá."
        db.agregar_evento(cid, "subcuenta", "error", "error", err)
        db.actualizar(cid, estado=db.ERROR_ALTA, error=err, mensaje=err)
        return
    ok, res, ultimas = correr(cid, ["crear_subcuenta.py", fila["slug"], "--alta"], "subcuenta",
                              lambda ev: AVANCE_ALTA.get((ev["etapa"], ev["paso"])))
    if res.get("location_id"):
        db.actualizar(cid, location_id=res["location_id"])
    if not ok or not res.get("agent_id"):
        err = mensaje_error(ultimas, ok)
        db.agregar_evento(cid, db.obtener(cid)["etapa"] or "alta", "error", "error", err)
        db.actualizar(cid, estado=db.ERROR_ALTA, error=err, mensaje=err)
        return
    db.actualizar(cid, estado=db.LISTA, resultado=res, avance=100, etapa="fin", paso="ok",
                  mensaje="Alta lista", error=None)


# ---------- hilos ----------

def _bucle(en_cola, corriendo, funcion, estado_error):
    while True:
        cid = db.tomar_siguiente(en_cola, corriendo)
        if cid is None:
            time.sleep(1)
            continue
        try:
            funcion(cid)
        except Exception as e:  # un error del panel no debe frenar la cola
            err = f"Error interno del panel: {e!r}"
            db.agregar_evento(cid, "panel", "error", "error", err)
            db.actualizar(cid, estado=estado_error, error=err, mensaje=err)


def arrancar():
    for args in ((db.EN_COLA_EXTRACCION, db.EXTRAYENDO, extraer, db.ERROR_EXTRACCION),
                 (db.EN_COLA_ALTA, db.EN_ALTA, alta, db.ERROR_ALTA)):
        threading.Thread(target=_bucle, args=args, daemon=True).start()
