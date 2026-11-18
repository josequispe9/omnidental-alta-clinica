"""Configura en GHL la subcuenta de una clínica para la demo del agente de voz.

Pasos (idempotentes: si algo ya existe, lo reutiliza en vez de duplicarlo):
  0. Completar perfil de la subcuenta           -> token de AGENCIA
  1. Crear el usuario del profesional           -> token de SUBCUENTA
  2. Crear calendario con horario y profesional -> token de SUBCUENTA
  2a. Igualar el horario de trabajo del profesional y vincularlo al calendario -> token de SUBCUENTA
  2b. Desactivar el calendario de ejemplo "Schedule an Appointment" -> token de SUBCUENTA
  3. Completar custom values                    -> token de SUBCUENTA
  4. Crear notificaciones por email al paciente (booked + confirmation) -> token de SUBCUENTA
  4b. Crear notificaciones por email al profesional (booked + confirmation) -> token de SUBCUENTA
  5. Crear pipeline de pacientes                -> token de SUBCUENTA
  6. Crear menú "Llamadas del agente" (iframe a la página de llamadas del middleware) -> token de AGENCIA

Tokens:
  AGENCIA   -> GHL_AGENCY_TOKEN (integración privada de agencia; sirve para cualquier subcuenta)
  SUBCUENTA -> se pide solo con la app de Marketplace (ghl_oauth.token_subcuenta), no hace falta
               crear una clave por subcuenta. Requiere haber corrido antes: python ghl_oauth.py instalar

Variables (en el .env de la carpeta principal):
  GHL_AGENCY_TOKEN, GHL_COMPANY_ID, GHL_CLIENT_ID, GHL_CLIENT_SECRET, MIDDLEWARE_URL
  GHL_API_VERSION (opcional, por defecto 2021-07-28)

Uso (normalmente se corre desde ../alta.py, que además crea el agente de Retell):
  python alta_ghl.py dental-sol --location <locationId> --dry-run
  python alta_ghl.py dental-sol --location <locationId>

Lee la clínica de la base (datos.py) y al terminar guarda ahí los IDs de GHL.
"""
import argparse
import json
import os
import re
import secrets
import string
import sys

import requests

import ghl_oauth

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from progreso import evento  # noqa: E402
import datos  # noqa: E402

BASE = "https://services.leadconnectorhq.com"


# ---------- utilidades ----------

REQUERIDAS = ("GHL_AGENCY_TOKEN", "GHL_COMPANY_ID", "GHL_CLIENT_ID", "GHL_CLIENT_SECRET")


def slug(texto):
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")


def generar_password(n=16):
    """GHL exige 12+ caracteres con mayúscula, minúscula, número y símbolo."""
    simbolos = "!@#$%&*?"
    base = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits), secrets.choice(simbolos)]
    resto = [secrets.choice(string.ascii_letters + string.digits + simbolos) for _ in range(n - 4)]
    pw = base + resto
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)


def hhmm(texto):
    h, m = texto.split(":")
    return int(h), int(m)


class GHL:
    def __init__(self, token, dry_run=False):
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Version": os.getenv("GHL_API_VERSION", "2021-07-28"),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def pedir(self, metodo, ruta, **kwargs):
        url = f"{BASE}{ruta}"
        if self.dry_run:
            cuerpo = kwargs.get("json")
            if isinstance(cuerpo, dict) and "password" in cuerpo:
                cuerpo = {**cuerpo, "password": "********"}  # nunca mostrar contraseñas
            print(f"    [dry-run] {metodo} {ruta}" + (f"\n    {json.dumps(cuerpo, ensure_ascii=False)[:400]}" if cuerpo else ""))
            return {}
        r = requests.request(metodo, url, headers=self.headers, timeout=30, **kwargs)
        if r.status_code >= 400:
            # Mostramos el error de GHL tal cual: suele decir qué campo falta o sobra
            raise RuntimeError(f"{metodo} {ruta} -> HTTP {r.status_code}: {r.text[:800]}")
        return r.json() if r.text else {}


def paso(n, texto):
    evento("ghl", n, texto, texto=f"[{n}] {texto}")


# ---------- pasos ----------

def completar_perfil(agencia, cfg, company_id, location_id):
    paso(0, "Completar perfil de la subcuenta")
    p = cfg["perfil"]
    agencia.pedir("PUT", f"/locations/{location_id}", json={
        "companyId": company_id,
        "name": cfg["clinica"],
        "phone": p["telefono"],
        **({"email": p["email"]} if p.get("email") else {}),
        "address": p["direccion"], "city": p["ciudad"], "state": p["provincia"],
        "country": p["pais"], "postalCode": p["cp"],
        "website": p["web"],
        "timezone": p["timezone"],
        "prospectInfo": {"firstName": p["contacto"]["nombre"],
                         "lastName": p["contacto"]["apellido"],
                         "email": p["contacto"]["email"]},
    })


def crear_profesional(sub, cfg, company_id, location_id):
    # Con token de AGENCIA, POST /users/ da "company does not have access to this feature"
    # en planes sin Agency Pro; con token de SUBCUENTA funciona.
    paso(1, "Crear usuario del profesional")
    prof = cfg["profesional"]
    existentes = sub.pedir("GET", "/users/", params={"locationId": location_id}).get("users", [])
    for u in existentes:
        if u.get("email", "").lower() == prof["email"].lower():
            print("    ya existía, se reutiliza")
            return u["id"]
    u = sub.pedir("POST", "/users/", json={
        "companyId": company_id,
        "firstName": prof["nombre"], "lastName": prof["apellido"],
        "email": prof["email"],
        "password": generar_password(),  # no se guarda: el profesional usa "olvidé mi contraseña"
        "type": "account", "role": "user",
        "locationIds": [location_id],
    })
    return u.get("id") or u.get("user", {}).get("id")


# datos de la clínica: calendario.horario = {"lunes": [["09:30", "13:30"], ["16:30", "20:30"]], ...}
DIAS_SEMANA = ["domingo", "lunes", "martes", "miercoles", "jueves", "viernes", "sabado"]  # índice = día de GHL
DIAS_GHL = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]


def dias_abiertos(cal):
    """[(número de día GHL 0=domingo, [[desde, hasta], ...]), ...] en orden de semana."""
    h = cal["horario"]
    desconocidos = set(h) - set(DIAS_SEMANA)
    if desconocidos:
        raise ValueError(f"Días desconocidos en calendario.horario: {sorted(desconocidos)}")
    return [(DIAS_SEMANA.index(d), h[d]) for d in DIAS_SEMANA if h.get(d)]


def horario(cal):
    """openHours de GHL: una entrada por día, con uno o más intervalos (turno partido)."""
    salida = []
    for dia, tramos in dias_abiertos(cal):
        horas = []
        for desde, hasta in tramos:
            (oh, om), (ch, cm) = hhmm(desde), hhmm(hasta)
            horas.append({"openHour": oh, "openMinute": om, "closeHour": ch, "closeMinute": cm})
        salida.append({"daysOfTheWeek": [dia], "hours": horas})
    return salida


def igualar_horario_profesional(sub, cfg, location_id, user_id, calendar_id):
    """GHL cruza el horario del calendario con el horario de trabajo ("Work Hours") del profesional, que se crea
    de lunes a viernes y sin vincular. Si la clínica abre otros días (ej. sábados), esos turnos nunca aparecen.
    Se igualan los días y horas a los de la clínica y se vincula el horario al calendario."""
    paso("2a", "Igualar el horario de trabajo del profesional")
    cal = cfg["calendario"]
    horarios = sub.pedir("GET", "/calendars/schedules/search",
                         params={"locationId": location_id, "userId": user_id}).get("schedules", [])
    if not horarios:
        print("    el profesional no tiene horario de trabajo en GHL; se sigue")
        return None
    h = horarios[0]
    sub.pedir("PUT", f"/calendars/schedules/{h['id']}", json={
        "rules": [{"type": "wday", "day": DIAS_GHL[d], "intervals": [{"from": a, "to": b} for a, b in tramos]}
                  for d, tramos in dias_abiertos(cal)],
        "calendarIds": sorted(set((h.get("calendarIds") or []) + [calendar_id])),
    })
    print(f"    {h.get('name')}: {len(dias_abiertos(cal))} días con el horario de la clínica, vinculado al calendario")
    return h["id"]


def crear_calendario(sub, cfg, location_id, user_id):
    paso(2, "Crear calendario con horario y profesional")
    cal = cfg["calendario"]
    existentes = sub.pedir("GET", "/calendars/", params={"locationId": location_id}).get("calendars", [])
    for c in existentes:
        if c.get("name") == cal["nombre"]:
            # se actualizan horario y duración por si cambiaron los datos de la clínica
            sub.pedir("PUT", f"/calendars/{c['id']}", json={
                "slotDuration": cal["duracion_min"], "slotDurationUnit": "mins",
                "slotInterval": cal["duracion_min"], "slotIntervalUnit": "mins",
                "openHours": horario(cal)})
            print("    ya existía: se actualizaron horario y duración")
            return c["id"]
    c = sub.pedir("POST", "/calendars/", json={
        "locationId": location_id,
        "name": cal["nombre"],
        "description": f"Turnos de {cal['nombre'].lower()} - {cfg['clinica']}",
        "calendarType": "round_robin",       # con 1 profesional funciona como calendario individual
        "teamMembers": [{"userId": user_id, "priority": 1, "isPrimary": True}],
        "slotDuration": cal["duracion_min"], "slotDurationUnit": "mins",
        "slotInterval": cal["duracion_min"], "slotIntervalUnit": "mins",
        "openHours": horario(cal),
        "autoConfirm": True,
    })
    return c.get("id") or c.get("calendar", {}).get("id")


CALENDARIO_EJEMPLO = "Schedule an Appointment"


def desactivar_calendario_ejemplo(sub, location_id, calendar_id):
    """Desactiva (no borra) el calendario de ejemplo que GHL crea en cada subcuenta nueva.

    Nunca toca el calendario del script (se compara por ID) ni los calendarios personales.
    """
    paso("2b", "Desactivar calendario de ejemplo")
    existentes = sub.pedir("GET", "/calendars/", params={"locationId": location_id}).get("calendars", [])
    if sub.dry_run:
        # En dry-run el GET no devuelve nada: mostramos el PUT que se enviaría por cada coincidencia
        sub.pedir("PUT", "/calendars/ID_CALENDARIO_EJEMPLO", json={"isActive": False})
        return []
    desactivados, encontrado = [], False
    for c in existentes:
        if c.get("name") != CALENDARIO_EJEMPLO:
            continue
        if c.get("id") == calendar_id or c.get("calendarType") == "personal":
            continue
        encontrado = True
        if c.get("isActive") is False:
            print(f"    {c['id']}: ya estaba desactivado")
            continue
        sub.pedir("PUT", f"/calendars/{c['id']}", json={"isActive": False})  # si GHL rechaza el campo, pedir() muestra el error
        print(f"    {c['id']}: desactivado")
        desactivados.append(c["id"])
    if not encontrado:
        print("    no existe, nada que desactivar")
    return desactivados


def completar_custom_values(sub, cfg, location_id):
    paso(3, "Completar custom values")
    ruta = f"/locations/{location_id}/customValues"
    existentes = {v["name"]: v for v in sub.pedir("GET", ruta).get("customValues", [])}
    claves = {}
    for nombre, valor in cfg["custom_values"].items():
        if nombre in existentes:
            v = sub.pedir("PUT", f"{ruta}/{existentes[nombre]['id']}", json={"name": nombre, "value": valor})
        else:
            v = sub.pedir("POST", ruta, json={"name": nombre, "value": valor})
        v = v.get("customValue", v)
        # fieldKey es la variable para usar en textos, p. ej. {{ custom_values.direccion }}
        claves[nombre] = v.get("fieldKey") or f"{{{{ custom_values.{slug(nombre).replace('-', '_')} }}}}"
    return claves


# Tipos de notificación de calendario en GHL:
#   "booked"       -> turno agendado SIN confirmar
#   "confirmation" -> turno agendado y confirmado ("Appointment booked (Status: Confirmed)")
# El calendario se crea con autoConfirm=True, así que los turnos nacen confirmados y la que se
# dispara es "confirmation". Se crean las dos por si algún turno se crea sin confirmar.
TIPOS_NOTIFICACION = ("booked", "confirmation")


def listar_notificaciones(sub, calendar_id):
    r = sub.pedir("GET", f"/calendars/{calendar_id}/notifications")
    return r if isinstance(r, list) else r.get("notifications", [])


def asegurar_notificaciones_email(sub, calendar_id, receiver_type, subject, body, from_name):
    """Crea una notificación de email por cada tipo de TIPOS_NOTIFICACION, si no existe ya.

    La existencia se verifica por separado para cada tipo (channel + notificationType + receiverType).
    Devuelve {tipo: id}.
    """
    existentes = listar_notificaciones(sub, calendar_id)
    ids = {}
    for tipo in TIPOS_NOTIFICACION:
        previa = next((n for n in existentes
                       if n.get("channel") == "email" and n.get("notificationType") == tipo
                       and n.get("receiverType") == receiver_type and not n.get("deleted")), None)
        if previa:
            print(f"    {tipo}: ya existía, se reutiliza")
            ids[tipo] = previa.get("_id") or previa.get("id")
            continue
        r = sub.pedir("POST", f"/calendars/{calendar_id}/notifications", json=[{
            "receiverType": receiver_type,
            "channel": "email",
            "notificationType": tipo,
            "isActive": True,
            "subject": subject,
            "body": body,
            "fromName": from_name,
        }])
        primero = r[0] if isinstance(r, list) and r else r
        ids[tipo] = (primero or {}).get("_id") or (primero or {}).get("id")
        print(f"    {tipo}: creada")
    return ids


def crear_notificacion(sub, cfg, calendar_id, claves):
    paso(4, "Crear notificaciones por email al paciente (booked + confirmation)")
    cuerpo = (
        "Hola {{contact.first_name}},<br><br>"
        f"Tu turno en {cfg['clinica']} quedó agendado para el "
        "{{appointment.start_time}}.<br>"
        f"Te esperamos en {claves.get('Direccion', '')}.<br>"
        f"Si necesitás cambiarlo, llamanos al {claves.get('Telefono', '')}.<br><br>"
        f"Te atiende: {claves.get('Profesional', '')}"
    )
    return asegurar_notificaciones_email(
        sub, calendar_id, "contact",
        subject=f"Tu turno en {cfg['clinica']} está confirmado",
        body=cuerpo, from_name=cfg["clinica"])


def crear_notificacion_profesional(sub, cfg, calendar_id):
    """Email al profesional asignado (assignedUser) cada vez que se agenda un turno."""
    paso("4b", "Crear notificaciones por email al profesional (booked + confirmation)")
    cuerpo = (
        "Nuevo turno agendado en " + cfg["clinica"] + ".<br><br>"
        "Paciente: {{contact.name}}<br>"
        "Teléfono: {{contact.phone}}<br>"
        "Email: {{contact.email}}<br>"
        "Fecha y hora: {{appointment.start_time}}<br>"
        f"Tipo de turno: {cfg['calendario']['nombre']}"
    )
    return asegurar_notificaciones_email(
        sub, calendar_id, "assignedUser",
        subject="Nuevo turno: {{contact.name}} - {{appointment.start_time}}",
        body=cuerpo, from_name=cfg["clinica"])


def crear_pipeline(sub, cfg, location_id):
    paso(5, "Crear pipeline de pacientes")
    pc = cfg["pipeline"]
    existentes = sub.pedir("GET", "/opportunities/pipelines", params={"locationId": location_id}).get("pipelines", [])
    pipe = next((p for p in existentes if p.get("name") == pc["nombre"]), None)
    if pipe:
        print("    ya existía, se reutiliza")
    else:
        r = sub.pedir("POST", "/opportunities/pipelines", json={
            "locationId": location_id,
            "name": pc["nombre"],
            "stages": [{"name": e, "position": i + 1} for i, e in enumerate(pc["etapas"])],
        })
        pipe = r.get("pipeline", r)
    etapas = {s.get("name"): s.get("id") for s in pipe.get("stages", [])}
    return pipe.get("id"), etapas


TITULO_MENU = "Llamadas del agente"


def crear_menu_llamadas(agencia, location_id, token_previo=None):
    """Menú en la barra lateral de la subcuenta que abre (en iframe) la página de llamadas del middleware.
    Usa el token de agencia: la app de Marketplace no tiene el scope de custom menus."""
    paso(6, "Crear menú de llamadas en la subcuenta")
    token = token_previo or secrets.token_urlsafe(24)
    base = os.getenv("MIDDLEWARE_URL", "https://MIDDLEWARE_URL").rstrip("/")
    url = f"{base}/llamadas?location={location_id}&token={token}"
    existentes = agencia.pedir("GET", "/custom-menus/", params={"limit": 100}).get("customMenus", [])
    menu = next((m for m in existentes if m.get("title") == TITULO_MENU
                 and location_id in (m.get("locations") or []) + [m.get("url", "")]), None)
    cuerpo = {"title": TITULO_MENU, "url": url, "icon": {"name": "phone", "fontFamily": "fas"},
              "showOnCompany": False, "showOnLocation": True, "showToAllLocations": False,
              "locations": [location_id], "openMode": "iframe", "userRole": "all",
              "allowCamera": False, "allowMicrophone": False}
    if menu:
        agencia.pedir("PUT", f"/custom-menus/{menu['id']}", json=cuerpo)
        print("    ya existía, se actualizó la URL")
        return menu["id"], token
    r = agencia.pedir("POST", "/custom-menus/", json=cuerpo)
    return (r.get("customMenu") or r).get("id"), token


# ---------- main ----------

def alta_ghl(cfg, location_id, dry_run=False):
    """Corre los pasos 0-5 sobre una subcuenta y devuelve el estado con los IDs."""
    company_id = os.getenv("GHL_COMPANY_ID", "COMPANY_ID")
    agencia = GHL(os.getenv("GHL_AGENCY_TOKEN", "x"), dry_run)
    if dry_run:
        sub = GHL("x", True)
    else:
        # Token de subcuenta pedido con la app de Marketplace (sin clave propia por subcuenta)
        sub = GHL(ghl_oauth.token_subcuenta(location_id))

    estado = {"clinica": cfg["clinica"], "location_id": location_id}
    completar_perfil(agencia, cfg, company_id, location_id)
    estado["user_id"] = crear_profesional(sub, cfg, company_id, location_id) or "USER_ID"
    estado["calendar_id"] = crear_calendario(sub, cfg, location_id, estado["user_id"]) or "CALENDAR_ID"
    estado["horario_profesional_id"] = igualar_horario_profesional(sub, cfg, location_id, estado["user_id"],
                                                                   estado["calendar_id"])
    estado["calendarios_desactivados"] = desactivar_calendario_ejemplo(sub, location_id, estado["calendar_id"])
    claves = completar_custom_values(sub, cfg, location_id)
    estado["custom_values"] = claves
    ids = crear_notificacion(sub, cfg, estado["calendar_id"], claves)
    estado["notification_id_booked"] = ids.get("booked")
    estado["notification_id_confirmation"] = ids.get("confirmation")
    ids = crear_notificacion_profesional(sub, cfg, estado["calendar_id"])
    estado["notification_profesional_id_booked"] = ids.get("booked")
    estado["notification_profesional_id_confirmation"] = ids.get("confirmation")
    estado["pipeline_id"], estado["etapas"] = crear_pipeline(sub, cfg, location_id)
    estado["etapa_turno_agendado"] = estado["etapas"].get("Turno agendado")
    previo = datos.registro(location_id) or {}
    estado["menu_llamadas_id"], estado["vista_token"] = crear_menu_llamadas(agencia, location_id,
                                                                           previo.get("vista_token"))
    return estado


def leer_clinica(slug_clinica):
    c = datos.clinica(slug_clinica)
    if not c:
        sys.exit(f"No existe la clínica '{slug_clinica}' en la base (cargala desde el panel o con datos.py importar)")
    return c


def guardar_registro(cfg, estado):
    """Guarda en la base los IDs de GHL de la clínica ."""
    estado = dict(estado)
    vista_token = estado.pop("vista_token", None)
    datos.guardar_ghl(cfg["slug"], estado["location_id"], estado, vista_token)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="slug de la clínica en la base")
    ap.add_argument("--location", required=True, help="ID de la subcuenta de GHL")
    ap.add_argument("--dry-run", action="store_true", help="muestra los pedidos sin enviarlos")
    args = ap.parse_args()

    ghl_oauth.cargar_env(() if args.dry_run else REQUERIDAS)
    cfg = leer_clinica(args.slug)
    estado = alta_ghl(cfg, args.location, args.dry_run)

    if args.dry_run:
        print("\nDry-run terminado. No se envió nada a GHL.")
        return
    guardar_registro(cfg, estado)
    print("\nAlta en GHL lista. IDs guardados en la base (Supabase)")
    print(json.dumps(estado, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
