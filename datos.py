"""Base de datos del proyecto (Postgres en Supabase, schema "omnidental"): única puerta de acceso a los datos.

Tablas:
  clinicas  una fila por clínica (o sede), de punta a punta: datos de la clínica, estado del panel,
            IDs de GHL y Retell y secretos
  eventos   registro de actividad de cada clínica (panel)
  llamadas  call_id de Retell -> contacto de GHL (para la nota de fin de llamada)
  tokens    tokens OAuth de la app de Marketplace de GHL

Conexión: SUPABASE_DB_URL en el .env (cadena de conexión del "Session pooler" de Supabase). El schema no está
expuesto en la API pública de Supabase y tiene RLS sin políticas: solo se accede con esta conexión directa.

Uso desde la terminal:
  .venv/bin/python datos.py listar
  .venv/bin/python datos.py exportar <slug> [archivo.json]     # para editar una clínica a mano
  .venv/bin/python datos.py importar <archivo.json>            # una clínica o una lista (formato clinicas.example.json)
"""
import argparse
import atexit
import json
import os
import sys
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

RAIZ = os.path.dirname(os.path.abspath(__file__))
T = "omnidental."  # schema

# estados de una clínica
EN_COLA_EXTRACCION, EXTRAYENDO, POR_REVISAR = "en_cola_extraccion", "extrayendo", "por_revisar"
EN_COLA_ALTA, EN_ALTA, LISTA = "en_cola_alta", "en_alta", "lista"
ERROR_EXTRACCION, ERROR_ALTA, DESCARTADA = "error_extraccion", "error_alta", "descartada"

JSON_COLS = ("datos", "revision", "fuentes", "extra", "resultado", "ghl", "retell")
COLUMNAS = ("slug", "nombre", "url", "origen_id", "estado", "etapa", "paso", "avance", "mensaje", "error", "captura",
            *JSON_COLS, "location_id", "vista_token", "encolada", "creada", "actualizada")

_pool = None
_lock = threading.Lock()

# Cache corta para lo que el middleware consulta en cada llamada y solo cambia con un alta
# (agente -> subcuenta, datos de la subcuenta). Los tokens de GHL no se cachean: GHL los rota.
CACHE_SEG = 30
_cache = {}


def _cacheado(clave, funcion):
    hit = _cache.get(clave)
    if hit and time.time() - hit[0] < CACHE_SEG:
        return hit[1]
    valor = funcion()
    _cache[clave] = (time.time(), valor)
    return valor


def _url():
    if not os.getenv("SUPABASE_DB_URL"):
        ruta = os.path.join(RAIZ, ".env")  # mismo formato que ghl_oauth.cargar_env (sin importarlo: evita ciclos)
        if os.path.exists(ruta):
            with open(ruta) as f:
                for linea in f:
                    linea = linea.strip()
                    if linea and not linea.startswith("#") and "=" in linea:
                        k, v = linea.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("Falta SUPABASE_DB_URL en el .env (Supabase > Connect > Session pooler)")
    return url


def _conexion():
    """Conexión del pool del proceso (la usan a la vez el middleware, el panel y los scripts)."""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                url = _url()
                try:  # una prueba directa primero: si la contraseña o el host están mal, falla rápido y claro
                    psycopg.connect(url, connect_timeout=15).close()
                except psycopg.OperationalError as e:
                    motivo = str(e).strip().splitlines()[-1]
                    sys.exit(f"No pude conectarme a Supabase (SUPABASE_DB_URL): {motivo}")
                # sin "check" por conexión: agrega una ida y vuelta a cada consulta. Las conexiones rotas se
                # descartan al devolverlas y max_idle/max_lifetime renuevan las viejas.
                _pool = ConnectionPool(url, min_size=1, max_size=4, open=True, timeout=30,
                                       max_idle=240, max_lifetime=1800,
                                       kwargs={"autocommit": True, "row_factory": dict_row,
                                               "prepare_threshold": None, "connect_timeout": 15})
                atexit.register(_pool.close)
    return _pool.connection()


def _todas(sql, params=()):
    with _conexion() as c:
        return c.execute(sql, params).fetchall()


def _una(sql, params=()):
    with _conexion() as c:
        return c.execute(sql, params).fetchone()


def _ejecutar(sql, params=()):
    with _conexion() as c:
        c.execute(sql, params)


def _json(campos):
    return {k: (Jsonb(v) if k in JSON_COLS and v is not None and not isinstance(v, Jsonb) else v)
            for k, v in campos.items()}


def _validar_columnas(campos):
    malas = set(campos) - set(COLUMNAS)
    if malas:
        raise ValueError(f"Columnas desconocidas: {sorted(malas)}")


# ---------- clínicas (filas completas) ----------

def fila(cid):
    return _una(f"SELECT * FROM {T}clinicas WHERE id=%s", (cid,))


def fila_por_slug(slug):
    """La clínica activa con ese slug (no descartada)."""
    return _una(f"""SELECT * FROM {T}clinicas WHERE slug=%s AND estado <> %s
                    ORDER BY (location_id IS NOT NULL) DESC, id DESC LIMIT 1""", (slug, DESCARTADA))


def fila_por_location(location_id):
    return _una(f"SELECT * FROM {T}clinicas WHERE location_id=%s AND estado <> %s ORDER BY id DESC LIMIT 1",
                (location_id, DESCARTADA))


def filas(incluir_descartadas=False):
    donde = "" if incluir_descartadas else "WHERE estado <> %s"
    return _todas(f"SELECT * FROM {T}clinicas {donde} ORDER BY COALESCE(origen_id, id), id",
                  () if incluir_descartadas else (DESCARTADA,))


def crear_fila(**campos):
    ahora = time.time()
    campos = {"creada": ahora, "actualizada": ahora, **campos}
    _validar_columnas(campos)
    campos = _json(campos)
    r = _una(f"INSERT INTO {T}clinicas ({', '.join(campos)}) VALUES ({', '.join(['%s'] * len(campos))}) RETURNING id",
             tuple(campos.values()))
    return r["id"]


def actualizar(cid, **campos):
    if not campos:
        return
    _cache.clear()
    campos = {**campos, "actualizada": time.time()}
    _validar_columnas(campos)
    campos = _json(campos)
    _ejecutar(f"UPDATE {T}clinicas SET {', '.join(f'{k}=%s' for k in campos)} WHERE id=%s", (*campos.values(), cid))


def slug_libre(slug, excepto=None):
    """True si ninguna otra clínica activa usa ese slug."""
    return _una(f"SELECT 1 FROM {T}clinicas WHERE slug=%s AND estado <> %s AND id <> %s",
                (slug, DESCARTADA, excepto or -1)) is None


def slug_disponible(base):
    """base, base-2, base-3... el primero libre (para las sedes y extracciones repetidas)."""
    slug, i = base, 2
    while not slug_libre(slug):
        slug, i = f"{base[:36]}-{i}", i + 1
    return slug


def tomar_siguiente(en_cola, corriendo):
    """Toma la fila más antigua de una cola y la marca como corriendo, en una sola operación atómica."""
    r = _una(f"""UPDATE {T}clinicas SET estado=%s, error=NULL, captura=NULL, avance=0, actualizada=%s
                 WHERE id = (SELECT id FROM {T}clinicas WHERE estado=%s ORDER BY encolada, id
                             LIMIT 1 FOR UPDATE SKIP LOCKED)
                 RETURNING id""", (corriendo, time.time(), en_cola))
    return r["id"] if r else None


def cantidad(estado):
    return _una(f"SELECT COUNT(*) AS n FROM {T}clinicas WHERE estado=%s", (estado,))["n"]


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
    """{slug, clinica, timezone, vista_token, ghl, retell} de la clínica de esa subcuenta, o None."""
    return _cacheado(("registro", location_id), lambda: _registro(location_id))


def _registro(location_id):
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
    return _cacheado(("agente", agent_id), lambda: _location_por_agente(agent_id))


def _location_por_agente(agent_id):
    r = _una(f"""SELECT location_id FROM {T}clinicas WHERE estado <> %s AND location_id IS NOT NULL
                 AND retell->>'agent_id' = %s""", (DESCARTADA, agent_id))
    return r["location_id"] if r else None


def recordar_llamada(call_id, location_id, contact_id, forzar=False):
    """Contacto al que va la nota de la llamada: el primero registrado, o el que agendó un turno (forzar)."""
    accion = ("DO UPDATE SET location_id=EXCLUDED.location_id, contact_id=EXCLUDED.contact_id, creada=EXCLUDED.creada"
              if forzar else "DO NOTHING")
    _ejecutar(f"INSERT INTO {T}llamadas (call_id, location_id, contact_id, creada) VALUES (%s,%s,%s,%s) "
              f"ON CONFLICT (call_id) {accion}", (call_id, location_id, contact_id, time.time()))


def llamada(call_id):
    return _una(f"SELECT * FROM {T}llamadas WHERE call_id=%s", (call_id,))


def leer_token(clave):
    r = _una(f"SELECT valor FROM {T}tokens WHERE clave=%s", (clave,))
    return r["valor"] if r else None


def guardar_token(clave, valor):
    _ejecutar(f"""INSERT INTO {T}tokens (clave, valor, actualizado) VALUES (%s,%s,%s)
                  ON CONFLICT (clave) DO UPDATE SET valor=EXCLUDED.valor, actualizado=EXCLUDED.actualizado""",
              (clave, Jsonb(valor), time.time()))


# ---------- eventos (panel) ----------

def agregar_evento(cid, etapa, paso, nivel, mensaje, ts=None):
    _ejecutar(f"INSERT INTO {T}eventos (clinica_id, ts, etapa, paso, nivel, mensaje) VALUES (%s,%s,%s,%s,%s,%s)",
              (cid, ts or time.time(), etapa, paso, nivel, mensaje))


def eventos(cid, desde=0):
    """Eventos de la clínica y, si es una sede agregada, también los de la extracción de la fila original."""
    f = fila(cid)
    ids = [cid] + ([f["origen_id"]] if f and f["origen_id"] else [])
    return _todas(f"SELECT * FROM {T}eventos WHERE clinica_id = ANY(%s) AND id > %s ORDER BY id", (ids, desde))


def _leer_json(ruta):
    with open(ruta) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar")
    e = sub.add_parser("exportar")
    e.add_argument("slug")
    e.add_argument("archivo", nargs="?")
    i = sub.add_parser("importar")
    i.add_argument("archivo")
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
        contenido = _leer_json(a.archivo)
        for c in contenido if isinstance(contenido, list) else [contenido]:
            if not c.get("slug"):
                sys.exit("Cada clínica necesita un slug")
            print(f"  {c['slug']}: id {guardar_clinica(c)}")


if __name__ == "__main__":
    main()
