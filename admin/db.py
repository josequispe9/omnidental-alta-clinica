"""Operaciones del panel sobre la base del proyecto (datos.py, omnidental.db).

Cada clínica es una fila de la tabla clinicas, de punta a punta. Una web se encola como una fila; al terminar
la extracción, si la web tiene varias sedes, la primera queda en esa fila y las demás se agregan como filas nuevas
con origen_id = la fila original (comparten los eventos de la extracción).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import datos  # noqa: E402
from datos import (DESCARTADA, EN_ALTA, EN_COLA_ALTA, EN_COLA_EXTRACCION, ERROR_ALTA,  # noqa: E402,F401
                   ERROR_EXTRACCION, EXTRAYENDO, LISTA, POR_REVISAR, actualizar, agregar_evento, eventos)

obtener = datos.fila


def iniciar():
    """Lo que estaba corriendo cuando se apagó el panel queda interrumpido (se puede reintentar)."""
    ahora = time.time()
    for f in datos.filas():
        for corriendo, err in ((EXTRAYENDO, ERROR_EXTRACCION), (EN_ALTA, ERROR_ALTA)):
            if f["estado"] == corriendo:
                actualizar(f["id"], estado=err, error="Interrumpida: el panel se reinició mientras corría",
                           mensaje="Interrumpida")
                agregar_evento(f["id"], "panel", "reinicio", "error", "Interrumpida: el panel se reinició", ahora)


def crear(url):
    return datos.crear_fila(url=url, estado=EN_COLA_EXTRACCION, mensaje="En cola para extraer", encolada=time.time())


def listar():
    return datos.filas()


def tomar_siguiente(en_cola, corriendo):
    """Toma la fila más antigua de la cola y la marca como corriendo (de a una por cola)."""
    with datos.conectar() as c:
        c.execute("BEGIN IMMEDIATE")
        f = c.execute("SELECT id FROM clinicas WHERE estado=? ORDER BY encolada, id LIMIT 1", (en_cola,)).fetchone()
        if not f:
            return None
        c.execute("UPDATE clinicas SET estado=?, error=NULL, captura=NULL, avance=0, actualizada=? WHERE id=?",
                  (corriendo, time.time(), f["id"]))
        return f["id"]


def cantidad(estado):
    with datos.conectar() as c:
        return c.execute("SELECT COUNT(*) FROM clinicas WHERE estado=?", (estado,)).fetchone()[0]


def crear_sede(origen, url, **campos):
    return datos.crear_fila(url=url, origen_id=origen, **campos)
