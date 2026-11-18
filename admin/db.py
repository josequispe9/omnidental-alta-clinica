"""Estado del panel en SQLite (admin.db): una fila por clínica (o sede) y el registro de eventos de cada una.

Una web se encola como una fila. Al terminar la extracción, si la web tiene varias sedes, la primera queda en esa
fila y las demás se agregan como filas nuevas con origen_id = la fila original (comparten los eventos de la extracción).
"""
import json
import os
import sqlite3
import threading
import time

RUTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin.db")
_lock = threading.Lock()

# estados
EN_COLA_EXTRACCION, EXTRAYENDO, POR_REVISAR = "en_cola_extraccion", "extrayendo", "por_revisar"
EN_COLA_ALTA, EN_ALTA, LISTA = "en_cola_alta", "en_alta", "lista"
ERROR_EXTRACCION, ERROR_ALTA, DESCARTADA = "error_extraccion", "error_alta", "descartada"

JSON_COLS = ("datos", "revision", "fuentes", "extra", "resultado")


def _con():
    c = sqlite3.connect(RUTA, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def iniciar():
    with _lock, _con() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS clinicas (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          url TEXT NOT NULL,
          origen_id INTEGER,
          estado TEXT NOT NULL,
          etapa TEXT, paso TEXT, avance REAL DEFAULT 0, mensaje TEXT,
          slug TEXT, nombre TEXT,
          datos TEXT, revision TEXT, fuentes TEXT, extra TEXT,
          location_id TEXT, resultado TEXT, error TEXT, captura TEXT,
          encolada REAL, creada REAL, actualizada REAL
        );
        CREATE TABLE IF NOT EXISTS eventos (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          clinica_id INTEGER NOT NULL,
          ts REAL, etapa TEXT, paso TEXT, nivel TEXT, mensaje TEXT
        );
        CREATE INDEX IF NOT EXISTS eventos_clinica ON eventos(clinica_id, id);
        """)
        # lo que estaba corriendo cuando se apagó el panel queda interrumpido (se puede reintentar)
        ahora = time.time()
        for est, err in ((EXTRAYENDO, ERROR_EXTRACCION), (EN_ALTA, ERROR_ALTA)):
            filas = c.execute("SELECT id FROM clinicas WHERE estado=?", (est,)).fetchall()
            for f in filas:
                c.execute("UPDATE clinicas SET estado=?, error=?, actualizada=? WHERE id=?",
                          (err, "Interrumpida: el panel se reinició mientras corría", ahora, f["id"]))
                c.execute("INSERT INTO eventos(clinica_id, ts, etapa, paso, nivel, mensaje) VALUES (?,?,?,?,?,?)",
                          (f["id"], ahora, "panel", "reinicio", "error", "Interrumpida: el panel se reinició"))


def _fila(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS:
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def crear(url):
    ahora = time.time()
    with _lock, _con() as c:
        cur = c.execute("INSERT INTO clinicas(url, estado, mensaje, encolada, creada, actualizada) VALUES (?,?,?,?,?,?)",
                        (url, EN_COLA_EXTRACCION, "En cola para extraer", ahora, ahora, ahora))
        return cur.lastrowid


def actualizar(cid, **campos):
    if not campos:
        return
    campos["actualizada"] = time.time()
    for k in JSON_COLS:
        if k in campos and campos[k] is not None:
            campos[k] = json.dumps(campos[k], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in campos)
    with _lock, _con() as c:
        c.execute(f"UPDATE clinicas SET {cols} WHERE id=?", (*campos.values(), cid))


def obtener(cid):
    with _con() as c:
        return _fila(c.execute("SELECT * FROM clinicas WHERE id=?", (cid,)).fetchone())


def listar():
    with _con() as c:
        filas = c.execute("SELECT * FROM clinicas WHERE estado != ? ORDER BY COALESCE(origen_id, id), id",
                          (DESCARTADA,)).fetchall()
    return [_fila(f) for f in filas]


def tomar_siguiente(en_cola, corriendo):
    """Toma la fila más antigua de la cola y la marca como corriendo (de a una por cola)."""
    with _lock, _con() as c:
        f = c.execute("SELECT id FROM clinicas WHERE estado=? ORDER BY encolada, id LIMIT 1", (en_cola,)).fetchone()
        if not f:
            return None
        c.execute("UPDATE clinicas SET estado=?, error=NULL, captura=NULL, avance=0, actualizada=? WHERE id=?",
                  (corriendo, time.time(), f["id"]))
        return f["id"]


def cantidad(estado):
    with _con() as c:
        return c.execute("SELECT COUNT(*) FROM clinicas WHERE estado=?", (estado,)).fetchone()[0]


def agregar_evento(cid, etapa, paso, nivel, mensaje, ts=None):
    with _lock, _con() as c:
        c.execute("INSERT INTO eventos(clinica_id, ts, etapa, paso, nivel, mensaje) VALUES (?,?,?,?,?,?)",
                  (cid, ts or time.time(), etapa, paso, nivel, mensaje))


def eventos(cid, desde=0):
    """Eventos de la clínica y, si es una sede agregada, también los de la extracción de la fila original."""
    fila = obtener(cid)
    ids = [cid] + ([fila["origen_id"]] if fila and fila["origen_id"] else [])
    with _con() as c:
        filas = c.execute(f"SELECT * FROM eventos WHERE clinica_id IN ({','.join('?' * len(ids))}) AND id > ? "
                          "ORDER BY id", (*ids, desde)).fetchall()
    return [dict(f) for f in filas]


def crear_sede(origen, url, **campos):
    ahora = time.time()
    with _lock, _con() as c:
        cur = c.execute("INSERT INTO clinicas(url, origen_id, estado, creada, actualizada) VALUES (?,?,?,?,?)",
                        (url, origen, POR_REVISAR, ahora, ahora))
        cid = cur.lastrowid
    actualizar(cid, **campos)
    return cid
