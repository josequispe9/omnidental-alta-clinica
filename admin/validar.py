"""Reglas que una clínica tiene que cumplir antes de encolar su alta."""
import re
import zoneinfo

from . import db
PAISES = ("AR", "ES", "MX", "CL", "UY", "CO", "PE", "US")  # los que sabe elegir crear_subcuenta.py
DIAS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
TEL = re.compile(r"^\+\d{8,15}$")
HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")


def _buscar_completar(obj, ruta=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _buscar_completar(v, f"{ruta}.{k}" if ruta else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _buscar_completar(v, f"{ruta}[{i}]")
    elif isinstance(obj, str) and obj.strip().upper() == "COMPLETAR":
        yield ruta


def errores(c, cid):
    """Lista de problemas ([] si se puede dar de alta). cid: la fila de la clínica (para no compararse con sí misma)."""
    e = [f"{r}: falta completar" for r in _buscar_completar(c)]
    g = lambda *ruta: _get(c, ruta)  # noqa: E731

    if not SLUG.match(str(g("slug") or "")):
        e.append("slug: solo minúsculas, números y guiones")
    elif not db.datos.slug_libre(g("slug"), excepto=cid):
        e.append(f"slug: '{g('slug')}' ya lo usa otra clínica")
    for ruta in (("clinica",), ("perfil", "direccion"), ("perfil", "ciudad"), ("perfil", "provincia"),
                 ("perfil", "cp"), ("profesional", "nombre"), ("profesional", "apellido"),
                 ("perfil", "contacto", "nombre"), ("perfil", "contacto", "apellido"), ("calendario", "nombre"),
                 ("agente", "nombre_asistente"), ("agente", "voz"), ("agente", "saludo")):
        if not str(g(*ruta) or "").strip():
            e.append(f"{'.'.join(ruta)}: obligatorio")
    for ruta in (("perfil", "email"), ("perfil", "contacto", "email"), ("profesional", "email")):
        if not EMAIL.match(str(g(*ruta) or "")):
            e.append(f"{'.'.join(ruta)}: email inválido")
    for ruta in (("perfil", "telefono"), ("profesional", "telefono")):
        if not TEL.match(str(g(*ruta) or "")):
            e.append(f"{'.'.join(ruta)}: teléfono en formato internacional (+34...)")
    if g("perfil", "pais") not in PAISES:
        e.append(f"perfil.pais: tiene que ser uno de {', '.join(PAISES)}")
    try:
        zoneinfo.ZoneInfo(str(g("perfil", "timezone") or ""))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        e.append("perfil.timezone: zona horaria inválida (ej. Europe/Madrid)")
    if g("agente", "idioma") not in ("es-ES", "es-419"):
        e.append("agente.idioma: es-ES o es-419")
    d = g("calendario", "duracion_min")
    if not isinstance(d, int) or not 5 <= d <= 240:
        e.append("calendario.duracion_min: número de minutos entre 5 y 240")

    h = g("calendario", "horario")
    if not isinstance(h, dict) or not h:
        e.append("calendario.horario: cargá al menos un día")
    else:
        for dia, tramos in h.items():
            if dia not in DIAS:
                e.append(f"calendario.horario: día desconocido '{dia}'")
                continue
            for t in tramos or []:
                if len(t) != 2 or not all(HORA.match(str(x)) for x in t) or t[0] >= t[1]:
                    e.append(f"calendario.horario.{dia}: tramo inválido {t} (HH:MM, desde < hasta)")

    u = g("urgencias") or {}
    if u.get("accion") not in ("derivar", "tomar_datos"):
        e.append("urgencias.accion: derivar o tomar_datos")
    elif u.get("accion") == "derivar" and not TEL.match(str(u.get("telefono") or "")):
        e.append("urgencias.telefono: obligatorio para derivar, en formato internacional")
    if not str(u.get("texto") or "").strip():
        e.append("urgencias.texto: obligatorio")
    for i, s in enumerate(g("servicios") or []):
        if not str(s.get("nombre") or "").strip():
            e.append(f"servicios[{i}].nombre: vacío")
    return e


def _get(obj, ruta):
    for k in ruta:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj
