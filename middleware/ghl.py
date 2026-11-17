"""Funciones de GHL que usa el middleware. Cada una recibe el locationId de la clínica (su ID).

Los IDs de cada clínica (calendario, embudo, etapas, zona horaria) salen de ../registro.json,
que completa el alta. El token de la subcuenta lo pide ghl_oauth con la app de Marketplace.
"""
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "ghl-alta"))
import ghl_oauth  # noqa: E402

ghl_oauth.cargar_env()
BASE = "https://services.leadconnectorhq.com"
REGISTRO = os.path.join(RAIZ, "registro.json")


class ErrorGHL(Exception):
    def __init__(self, mensaje, respuesta=None):
        super().__init__(mensaje)
        self.respuesta = respuesta or {}


def clinica(location_id):
    with open(REGISTRO) as f:
        registro = json.load(f)
    if location_id not in registro:
        raise ErrorGHL(f"La clínica {location_id} no está en registro.json (falta correr el alta)")
    return registro[location_id]


def _pedir(location_id, metodo, ruta, version="2021-07-28", **kwargs):
    token = ghl_oauth.token_subcuenta(location_id)
    r = requests.request(metodo, BASE + ruta, timeout=30, headers={
        "Authorization": f"Bearer {token}", "Version": version, "Accept": "application/json"}, **kwargs)
    if r.status_code >= 400:
        try:
            cuerpo = r.json()
        except ValueError:
            cuerpo = {}
        raise ErrorGHL(f"{metodo} {ruta} -> HTTP {r.status_code}: {r.text[:300]}", cuerpo)
    return r.json() if r.text else {}


# ---------- contactos ----------

def buscar_o_crear_cliente(location_id, nombre_completo, telefono=None, correo=None):
    """Crea el contacto o, si ya existe (mismo teléfono o correo), lo reutiliza. Devuelve (id, es_nuevo)."""
    partes = nombre_completo.strip().split(" ", 1)
    datos = {"locationId": location_id, "firstName": partes[0], "lastName": partes[1] if len(partes) > 1 else "",
             "name": nombre_completo.strip(), "source": "Agente de voz"}
    if telefono:
        datos["phone"] = telefono
    if correo:
        datos["email"] = correo
    r = _pedir(location_id, "POST", "/contacts/upsert", json=datos)
    return r["contact"]["id"], bool(r.get("new"))


def agregar_nota(location_id, contact_id, texto):
    return _pedir(location_id, "POST", f"/contacts/{contact_id}/notes", json={"body": texto})["note"]["id"]


# ---------- agenda ----------

def _calendario(location_id):
    c = clinica(location_id)
    cal = _pedir(location_id, "GET", f"/calendars/{c['ghl']['calendar_id']}", version="2021-04-15")["calendar"]
    return c, cal


def _slots(location_id, calendar_id, tz, desde, dias):
    inicio = datetime.combine(desde, time.min, ZoneInfo(tz))
    fin = inicio + timedelta(days=dias)
    r = _pedir(location_id, "GET", f"/calendars/{calendar_id}/free-slots", version="2021-04-15", params={
        "startDate": int(inicio.timestamp() * 1000), "endDate": int(fin.timestamp() * 1000), "timezone": tz})
    slots = []
    for clave, valor in r.items():
        if isinstance(valor, dict) and "slots" in valor:
            slots += [datetime.fromisoformat(s).astimezone(ZoneInfo(tz)) for s in valor["slots"]]
    return sorted(slots)


def consultar_disponibilidad(location_id, fecha, hora=None, alternativas=3):
    """fecha 'AAAA-MM-DD', hora 'HH:MM' (hora local de la clínica).
    Devuelve {"disponible": bool, "pedido": iso | None, "alternativas": [iso, ...]}."""
    c, cal = _calendario(location_id)
    tz = c["timezone"]
    dia = date.fromisoformat(fecha)
    slots = _slots(location_id, cal["id"], tz, dia, 7)
    pedido = None
    if hora:
        pedido = datetime.combine(dia, time.fromisoformat(hora), ZoneInfo(tz))
        if pedido in slots:
            return {"disponible": True, "pedido": pedido.isoformat(), "alternativas": []}
        cercanos = sorted(slots, key=lambda s: abs(s - pedido))[:alternativas]
    else:
        cercanos = slots[:alternativas]
    return {"disponible": False if hora else bool(slots), "pedido": pedido.isoformat() if pedido else None,
            "alternativas": [s.isoformat() for s in sorted(cercanos)]}


def agendar_cita(location_id, contact_id, fecha, hora, titulo=None):
    """Agenda la cita y mueve/crea la oportunidad del contacto en la etapa "Turno agendado".
    Devuelve {"cita_id", "inicio", "oportunidad_id"}."""
    c, cal = _calendario(location_id)
    tz = c["timezone"]
    inicio = datetime.combine(date.fromisoformat(fecha), time.fromisoformat(hora), ZoneInfo(tz))
    fin = inicio + timedelta(minutes=int(cal.get("slotDuration") or 30))
    cita = _pedir(location_id, "POST", "/calendars/events/appointments", version="2021-04-15", json={
        "calendarId": cal["id"], "locationId": location_id, "contactId": contact_id,
        "startTime": inicio.isoformat(), "endTime": fin.isoformat(),
        "title": titulo or f"{cal['name']} - agendado por el agente de voz",
        "appointmentStatus": "confirmed",
    })
    oportunidad = mover_oportunidad(location_id, contact_id, "Turno agendado")
    return {"cita_id": cita["id"], "inicio": inicio.isoformat(), "oportunidad_id": oportunidad}


def cancelar_cita(location_id, cita_id):
    _pedir(location_id, "DELETE", f"/calendars/events/{cita_id}", version="2021-04-15")


# ---------- embudo ----------

def mover_oportunidad(location_id, contact_id, etapa):
    """Deja la oportunidad del contacto (una por contacto en el embudo) en esa etapa. Devuelve su ID."""
    g = clinica(location_id)["ghl"]
    etapa_id = g["etapas"][etapa]
    contacto = _pedir(location_id, "GET", f"/contacts/{contact_id}")["contact"]
    nombre = " ".join(filter(None, [contacto.get("firstName"), contacto.get("lastName")])) or "Paciente"
    try:
        return _pedir(location_id, "POST", "/opportunities/", json={
            "locationId": location_id, "pipelineId": g["pipeline_id"], "pipelineStageId": etapa_id,
            "contactId": contact_id, "name": nombre, "status": "open"})["opportunity"]["id"]
    except ErrorGHL as e:
        # GHL no permite dos oportunidades por contacto y devuelve la existente en meta.existingId
        # (la búsqueda /opportunities/search tarda en indexar, por eso no se usa)
        if e.respuesta.get("code") != "OPPORTUNITY_NO_DUPLICATE":
            raise
        op_id = e.respuesta["meta"]["existingId"]
        _pedir(location_id, "PUT", f"/opportunities/{op_id}", json={"pipelineStageId": etapa_id})
        return op_id


# ---------- correo ----------

def enviar_correo(location_id, contact_id, asunto, mensaje_html):
    """Envía un email al contacto desde la subcuenta. Ojo: con el dominio compartido de GHL suele ir a spam."""
    r = _pedir(location_id, "POST", "/conversations/messages", version="2021-04-15", json={
        "type": "Email", "contactId": contact_id, "subject": asunto, "html": mensaje_html})
    return r.get("messageId") or r.get("emailMessageId")
