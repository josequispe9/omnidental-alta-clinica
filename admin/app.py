"""Panel de administración: extraer webs, revisar los datos y dar de alta clínicas, en cola.

Solo para uso local (no pasar por ngrok: crea subcuentas y gasta créditos):

  .venv/bin/uvicorn admin.app:app --host 127.0.0.1 --port 8001
"""
import os
import re
import sys
import time

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import colas, db, validar

RAIZ = colas.RAIZ
sys.path[:0] = [os.path.join(RAIZ, "ghl-alta"), os.path.join(RAIZ, "middleware")]
import ghl_oauth  # noqa: E402
from prompt import armar_prompt  # noqa: E402

ghl_oauth.cargar_env(())
ESTATICOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CLAVES = ("GHL_AGENCY_TOKEN", "GHL_COMPANY_ID", "GHL_CLIENT_ID", "GHL_CLIENT_SECRET", "RETELL_APIKEY",
          "MIDDLEWARE_URL", "ANTHROPIC_API_KEY")

app = FastAPI(title="Panel de alta de clínicas")
app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")


@app.on_event("startup")
def _inicio():
    db.iniciar()
    colas.arrancar()


@app.get("/")
def inicio():
    return FileResponse(os.path.join(ESTATICOS, "index.html"))


def _responde(url, **kw):
    try:
        return requests.get(url, timeout=4, **kw).ok
    except requests.RequestException:
        return False


@app.get("/api/estado")
def estado():
    mw = os.getenv("MIDDLEWARE_URL", "").rstrip("/")
    return {
        "brave": colas.brave_abierto(),
        "middleware": _responde("http://127.0.0.1:8000/salud"),
        "ngrok": bool(mw) and _responde(f"{mw}/salud", headers={"ngrok-skip-browser-warning": "1"}),
        "claves_faltantes": [k for k in CLAVES if not os.getenv(k)],
        "colas": {"extraccion": {"en_cola": db.cantidad(db.EN_COLA_EXTRACCION), "corriendo": db.cantidad(db.EXTRAYENDO)},
                  "alta": {"en_cola": db.cantidad(db.EN_COLA_ALTA), "corriendo": db.cantidad(db.EN_ALTA)}},
    }


class Urls(BaseModel):
    urls: list[str]


@app.post("/api/extracciones")
def encolar_extracciones(body: Urls):
    ids, rechazadas = [], []
    for u in body.urls:
        u = u.strip()
        if not u:
            continue
        if not re.match(r"^https?://", u):
            u = "https://" + u
        if not re.match(r"^https?://[^/\s]+\.[^/\s]+", u):
            rechazadas.append(u)
            continue
        ids.append(db.crear(u))
    return {"ids": ids, "rechazadas": rechazadas}


def _resumen(f):
    return {k: f[k] for k in ("id", "url", "origen_id", "estado", "etapa", "paso", "avance", "mensaje", "slug",
                              "nombre", "location_id", "error", "actualizada")}


@app.get("/api/clinicas")
def listar():
    return [_resumen(f) for f in db.listar()]


def _fila(cid):
    f = db.obtener(cid)
    if not f or f["estado"] == db.DESCARTADA:
        raise HTTPException(404, "No existe esa clínica")
    return f


def _links(f):
    if not f["location_id"]:
        return {}
    retell = f["retell"] or {}
    mw = os.getenv("MIDDLEWARE_URL", "").rstrip("/")
    links = {"ghl": f"https://app.gohighlevel.com/v2/location/{f['location_id']}/dashboard"}
    if retell.get("agent_id"):
        links["retell"] = f"https://dashboard.retellai.com/agents/{retell['agent_id']}"
    if f["vista_token"]:
        links["llamadas"] = f"{mw}/llamadas?location={f['location_id']}&token={f['vista_token']}"
    return links


@app.get("/api/clinicas/{cid}")
def detalle(cid: int):
    f = _fila(cid)
    datos = f["datos"]
    return {**_resumen(f), "datos": datos, "revision": f["revision"], "fuentes": f["fuentes"],
            "extra": f["extra"], "resultado": f["resultado"], "captura": bool(f["captura"]),
            "links": _links(f),
            "problemas": validar.errores(datos, cid) if datos and f["estado"] in EDITABLE else []}


EDITABLE = (db.POR_REVISAR, db.ERROR_ALTA)


@app.put("/api/clinicas/{cid}")
def guardar(cid: int, datos: dict):
    f = _fila(cid)
    if f["estado"] not in EDITABLE:
        raise HTTPException(409, "Solo se puede editar una clínica por revisar o con error en el alta")
    db.actualizar(cid, datos=datos, slug=datos.get("slug"), nombre=datos.get("clinica"))
    return {"ok": True, "problemas": validar.errores(datos, cid)}


@app.post("/api/clinicas/{cid}/prompt")
def vista_prompt(cid: int, datos: dict):
    _fila(cid)
    try:
        return {"prompt": armar_prompt(datos)}
    except Exception as e:  # datos incompletos: se muestra el problema en vez del prompt
        raise HTTPException(422, f"No se pudo armar el prompt: {e!r}")


@app.post("/api/clinicas/{cid}/validar")
def validar_y_encolar(cid: int, datos: dict):
    f = _fila(cid)
    if f["estado"] not in EDITABLE:
        raise HTTPException(409, "Esta clínica no está para validar")
    problemas = validar.errores(datos, cid)
    db.actualizar(cid, datos=datos, slug=datos.get("slug"), nombre=datos.get("clinica"))
    if problemas:
        return {"ok": False, "problemas": problemas}
    db.actualizar(cid, estado=db.EN_COLA_ALTA, encolada=time.time(), etapa="subcuenta", paso="", avance=0,
                  mensaje="Validada: en cola para el alta", error=None)
    db.agregar_evento(cid, "revision", "validada", "info", "Datos validados: en cola para el alta")
    return {"ok": True, "problemas": []}


@app.post("/api/clinicas/{cid}/reintentar")
def reintentar(cid: int):
    f = _fila(cid)
    if f["estado"] == db.ERROR_EXTRACCION:
        db.actualizar(cid, estado=db.EN_COLA_EXTRACCION, encolada=time.time(), mensaje="En cola para extraer",
                      error=None, avance=0)
    elif f["estado"] == db.ERROR_ALTA:
        return validar_y_encolar(cid, f["datos"])
    else:
        raise HTTPException(409, "Solo se reintenta una clínica con error")
    db.agregar_evento(cid, "panel", "reintento", "info", "Reintento encolado")
    return {"ok": True}


@app.delete("/api/clinicas/{cid}")
def descartar(cid: int):
    f = _fila(cid)
    if f["estado"] in (db.EXTRAYENDO, db.EN_ALTA):
        raise HTTPException(409, "No se puede descartar mientras corre: esperá a que termine")
    if f["estado"] == db.LISTA:
        raise HTTPException(409, "Ya está dada de alta: no se descarta desde el panel")
    db.actualizar(cid, estado=db.DESCARTADA, mensaje="Descartada")
    return {"ok": True}


@app.get("/api/clinicas/{cid}/eventos")
def eventos(cid: int, desde: int = 0):
    _fila(cid)
    return db.eventos(cid, desde)


@app.get("/api/clinicas/{cid}/captura")
def captura(cid: int):
    f = _fila(cid)
    if not f["captura"] or not os.path.exists(f["captura"]):
        raise HTTPException(404, "Sin captura")
    return FileResponse(f["captura"], media_type="image/png")
