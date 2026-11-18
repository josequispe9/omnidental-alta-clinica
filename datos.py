"""Base de datos del proyecto (SQLite, omnidental.db): única puerta de acceso a los datos.

Tablas:
  clinicas  una fila por clínica (o sede), de punta a punta: datos de la clínica (lo que era clinicas.json),
            estado del panel, IDs de GHL y Retell y secretos (lo que era registro.json)
  eventos   registro de actividad de cada clínica (panel)
  llamadas  call_id de Retell -> contacto de GHL (para la nota de fin de llamada)
  tokens    tokens OAuth de la app de Marketplace de GHL

Uso desde la terminal:
  .venv/bin/python datos.py listar
  .venv/bin/python datos.py exportar <slug> [archivo.json]     # para editar una clínica a mano
  .venv/bin/python datos.py importar <archivo.json>            # una clínica o una lista (formato clinicas.example.json)
  .venv/bin/python datos.py migrar                             # trae clinicas.json, registro.json, llamadas.json y tokens
"""
import argparse
import json
import os
import sqlite3
import sys
import time

RAIZ = os.path.dirname(os.path.abspath(__file__))
RUTA = os.getenv("OMNIDENTAL_DB", os.path.join(RAIZ, "omnidental.db"))

# estados de una clínica
EN_COLA_EXTRACCION, EXTRAYENDO, POR_REVISAR = "en_cola_extraccion", "extrayendo", "por_revisar"
EN_COLA_ALTA, EN_ALTA, LISTA = "en_cola_alta", "en_alta", "lista"
ERROR_EXTRACCION, ERROR_ALTA, DESCARTADA = "error_extraccion", "error_alta", "descartada"

JSON_COLS = ("datos", "revision", "fuentes", "extra", "resultado", "ghl", "retell")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS clinicas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT, nombre TEXT, url TEXT, origen_id INTEGER,
  estado TEXT NOT NULL, etapa TEXT, paso TEXT, avance REAL DEFAULT 0, mensaje TEXT, error TEXT, captura TEXT,
  datos TEXT,                         -- la clínica (formato clinicas.example.json)
  revision TEXT, fuentes TEXT, extra TEXT, resultado TEXT,   -- extracción y panel
  location_id TEXT, ghl TEXT, retell TEXT, vista_token TEXT, -- alta (antes registro.json)
  encolada REAL, creada REAL, actualizada REAL
);
CREATE INDEX IF NOT EXISTS clinicas_slug ON clinicas(slug);
CREATE INDEX IF NOT EXISTS clinicas_location ON clinicas(location_id);
CREATE TABLE IF NOT EXISTS eventos (
  id INTEGER PRIMARY KEY AUTOINCREMENT, clinica_id INTEGER NOT NULL,
  ts REAL, etapa TEXT, paso TEXT, nivel TEXT, mensaje TEXT
);
CREATE INDEX IF NOT EXISTS eventos_clinica ON eventos(clinica_id, id);
CREATE TABLE IF NOT EXISTS llamadas (
  call_id TEXT PRIMARY KEY, location_id TEXT, contact_id TEXT, creada REAL
);
CREATE TABLE IF NOT EXISTS tokens (
  clave TEXT PRIMARY KEY, valor TEXT, actualizado REAL
);
"""

_iniciada = False


def conectar():
    """Conexión nueva (una por operación: la usan a la vez el middleware, el panel y los scripts)."""
    global _iniciada
    nueva = not os.path.exists(RUTA)
    c = sqlite3.connect(RUTA, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout = 30000")
    if not _iniciada:
        c.execute("PRAGMA journal_mode = WAL")  # lectores y un escritor a la vez sin bloquearse
        c.executescript(ESQUEMA)
        _iniciada = True
        if nueva:
            os.chmod(RUTA, 0o600)  # tiene secretos (tokens de GHL, secretos de cada agente)
    return c


def _fila(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS:
        if k in d:
            d[k] = json.loads(d[k]) if d[k] else None
    return d


def _json(campos):
    for k in JSON_COLS:
        if k in campos and campos[k] is not None and not isinstance(campos[k], str):
            campos[k] = json.dumps(campos[k], ensure_ascii=False)
    return campos


# ---------- clínicas (filas completas) ----------

def fila(cid):
    with conectar() as c:
        return _fila(c.execute("SELECT * FROM clinicas WHERE id=?", (cid,)).fetchone())


def fila_por_slug(slug):
    """La clínica activa con ese slug (no descartada); si hubiera varias, la más avanzada y reciente."""
    with conectar() as c:
        r = c.execute("""SELECT * FROM clinicas WHERE slug=? AND estado != ?
                         ORDER BY (location_id IS NOT NULL) DESC, id DESC LIMIT 1""", (slug, DESCARTADA)).fetchone()
    return _fila(r)


def fila_por_location(location_id):
    with conectar() as c:
        r = c.execute("SELECT * FROM clinicas WHERE location_id=? AND estado != ? ORDER BY id DESC LIMIT 1",
                      (location_id, DESCARTADA)).fetchone()
    return _fila(r)


def filas(incluir_descartadas=False):
    with conectar() as c:
        rs = c.execute("SELECT * FROM clinicas" + ("" if incluir_descartadas else " WHERE estado != ?") +
                       " ORDER BY COALESCE(origen_id, id), id", () if incluir_descartadas else (DESCARTADA,)).fetchall()
    return [_fila(r) for r in rs]


def crear_fila(**campos):
    ahora = time.time()
    campos = _json({"creada": ahora, "actualizada": ahora, **campos})
    with conectar() as c:
        cur = c.execute(f"INSERT INTO clinicas({', '.join(campos)}) VALUES ({', '.join('?' * len(campos))})",
                        tuple(campos.values()))
        return cur.lastrowid


def actualizar(cid, **campos):
    if not campos:
        return
    campos = _json({**campos, "actualizada": time.time()})
    with conectar() as c:
        c.execute(f"UPDATE clinicas SET {', '.join(f'{k}=?' for k in campos)} WHERE id=?", (*campos.values(), cid))


def slug_libre(slug, excepto=None):
    """True si ninguna otra clínica activa usa ese slug."""
    with conectar() as c:
        r = c.execute("SELECT id FROM clinicas WHERE slug=? AND estado != ? AND id != ?",
                      (slug, DESCARTADA, excepto or -1)).fetchone()
    return r is None


def slug_disponible(base):
    """base, base-2, base-3... el primero libre (para las sedes y extracciones repetidas)."""
    slug, i = base, 2
    while not slug_libre(slug):
        slug, i = f"{base[:36]}-{i}", i + 1
    return slug


# ---------- lo que usan los scripts del alta y el middleware ----------

def clinica(slug):
    """Datos de la clínica (formato clinicas.example.json) o None."""
    f = fila_por_slug(slug)
    return f["datos"] if f else None


def guardar_clinica(datos, estado=POR_REVISAR):
    """Agrega o reemplaza (por slug) los datos de una clínica. Devuelve el id."""
    f = fila_por_slug(datos["slug"])
    if f:
        actualizar(f["id"], datos=datos, nombre=datos.get("clinica"))
        return f["id"]
    return crear_fila(slug=datos["slug"], nombre=datos.get("clinica"), url=(datos.get("perfil") or {}).get("web"),
                      estado=estado, datos=datos)


def registro(location_id):
    """Lo que antes era la entrada de registro.json: {slug, clinica, timezone, vista_token, ghl, retell} o None."""
    f = fila_por_location(location_id)
    if not f:
        return None
    return {"slug": f["slug"], "clinica": f["nombre"], "timezone": ((f["datos"] or {}).get("perfil") or {}).get("timezone"),
            "vista_token": f["vista_token"], "ghl": f["ghl"] or {}, "retell": f["retell"] or {}}


def clinicas_dadas_de_alta():
    """{location_id: registro} de todas las clínicas con subcuenta."""
    return {f["location_id"]: registro(f["location_id"]) for f in filas() if f["location_id"]}


def guardar_ghl(slug, location_id, ghl, vista_token):
    f = fila_por_slug(slug)
    if not f:
        raise LookupError(f"No existe la clínica '{slug}'")
    otra = fila_por_location(location_id)
    if otra and otra["id"] != f["id"]:
        raise ValueError(f"La subcuenta {location_id} ya es de la clínica '{otra['slug']}'")
    actualizar(f["id"], location_id=location_id, ghl=ghl, vista_token=vista_token)


def guardar_retell(location_id, retell):
    f = fila_por_location(location_id)
    if not f:
        raise LookupError(f"La subcuenta {location_id} no tiene clínica: corré primero el alta de GHL")
    actualizar(f["id"], retell=retell)


def location_por_agente(agent_id):
    with conectar() as c:
        r = c.execute("""SELECT location_id FROM clinicas WHERE estado != ? AND location_id IS NOT NULL
                         AND json_extract(retell, '$.agent_id') = ?""", (DESCARTADA, agent_id)).fetchone()
    return r["location_id"] if r else None


def recordar_llamada(call_id, location_id, contact_id, forzar=False):
    """Contacto al que va la nota de la llamada: el primero registrado, o el que agendó un turno (forzar)."""
    with conectar() as c:
        c.execute(("INSERT OR REPLACE" if forzar else "INSERT OR IGNORE") +
                  " INTO llamadas(call_id, location_id, contact_id, creada) VALUES (?,?,?,?)",
                  (call_id, location_id, contact_id, time.time()))


def llamada(call_id):
    with conectar() as c:
        r = c.execute("SELECT * FROM llamadas WHERE call_id=?", (call_id,)).fetchone()
    return dict(r) if r else None


def leer_token(clave):
    with conectar() as c:
        r = c.execute("SELECT valor FROM tokens WHERE clave=?", (clave,)).fetchone()
    return json.loads(r["valor"]) if r else None


def guardar_token(clave, valor):
    with conectar() as c:
        c.execute("INSERT OR REPLACE INTO tokens(clave, valor, actualizado) VALUES (?,?,?)",
                  (clave, json.dumps(valor), time.time()))


# ---------- eventos (panel) ----------

def agregar_evento(cid, etapa, paso, nivel, mensaje, ts=None):
    with conectar() as c:
        c.execute("INSERT INTO eventos(clinica_id, ts, etapa, paso, nivel, mensaje) VALUES (?,?,?,?,?,?)",
                  (cid, ts or time.time(), etapa, paso, nivel, mensaje))


def eventos(cid, desde=0):
    """Eventos de la clínica y, si es una sede agregada, también los de la extracción de la fila original."""
    f = fila(cid)
    ids = [cid] + ([f["origen_id"]] if f and f["origen_id"] else [])
    with conectar() as c:
        rs = c.execute(f"SELECT * FROM eventos WHERE clinica_id IN ({','.join('?' * len(ids))}) AND id > ? ORDER BY id",
                       (*ids, desde)).fetchall()
    return [dict(r) for r in rs]


# ---------- migración desde los archivos sueltos ----------

def _leer_json(ruta, defecto):
    if not os.path.exists(ruta):
        return defecto
    with open(ruta) as f:
        return json.load(f)


def migrar():
    clinicas_json = _leer_json(os.path.join(RAIZ, "clinicas.json"), [])
    reg = _leer_json(os.path.join(RAIZ, "registro.json"), {})
    por_slug = {v.get("slug"): (loc, v) for loc, v in reg.items()}
    for c in clinicas_json:
        cid = guardar_clinica(c, estado=POR_REVISAR)
        if c["slug"] in por_slug:
            loc, v = por_slug[c["slug"]]
            actualizar(cid, location_id=loc, ghl=v.get("ghl"), retell=v.get("retell"), vista_token=v.get("vista_token"),
                       estado=LISTA, etapa="fin", paso="ok", avance=100, mensaje="Dada de alta (migrada de registro.json)",
                       resultado={"location_id": loc, **{k: (v.get("retell") or {}).get(k) for k in ("agent_id",)},
                                  "vista_token": v.get("vista_token")})
        print(f"  clínica {c['slug']}" + (f" -> {por_slug[c['slug']][0]}" if c["slug"] in por_slug else " (sin alta)"))
    sin_datos = [v.get("slug") for v in reg.values() if v.get("slug") not in {c["slug"] for c in clinicas_json}]
    if sin_datos:
        print(f"  aviso: en registro.json pero no en clinicas.json (no se migran): {sin_datos}")
    for call_id, v in _leer_json(os.path.join(RAIZ, "llamadas.json"), {}).items():
        recordar_llamada(call_id, v.get("location_id"), v.get("contact_id"), forzar=True)
    tokens = _leer_json(os.path.join(RAIZ, "ghl-alta", ".ghl_oauth.json"), None)
    if tokens:
        guardar_token("ghl_oauth", tokens)
        print("  tokens OAuth de GHL")
    print(f"Listo: {RUTA}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar")
    e = sub.add_parser("exportar")
    e.add_argument("slug")
    e.add_argument("archivo", nargs="?")
    i = sub.add_parser("importar")
    i.add_argument("archivo")
    sub.add_parser("migrar")
    a = ap.parse_args()

    if a.cmd == "listar":
        for f in filas():
            print(f"{f['id']:3d}  {f['estado']:<18} {f['slug'] or '-':<30} {f['location_id'] or '-':<22} {f['nombre'] or f['url']}")
    elif a.cmd == "exportar":
        c = clinica(a.slug)
        if not c:
            sys.exit(f"No existe la clínica '{a.slug}'")
        texto = json.dumps(c, indent=2, ensure_ascii=False) + "\n"
        if a.archivo:
            with open(a.archivo, "w") as f:
                f.write(texto)
            print(f"Guardada en {a.archivo}. Después de editarla: datos.py importar {a.archivo}")
        else:
            print(texto, end="")
    elif a.cmd == "importar":
        contenido = _leer_json(a.archivo, None)
        for c in contenido if isinstance(contenido, list) else [contenido]:
            if not c.get("slug"):
                sys.exit("Cada clínica necesita un slug")
            print(f"  {c['slug']}: id {guardar_clinica(c)}")
    elif a.cmd == "migrar":
        migrar()


if __name__ == "__main__":
    main()
