"""Middleware Retell <-> GHL.

Correr (desde middleware/):
  ../.venv/bin/uvicorn app:app --port 8000
  ngrok http 8000 --url <MIDDLEWARE_URL>

Endpoints:
  POST /tools/{nombre}   herramientas del agente de Retell (firma verificada)
  POST /retell/webhook   fin de llamada: guarda el resumen como nota en el contacto de GHL
  GET  /llamadas         lista de llamadas de una clínica (se ve dentro de GHL)
  GET  /llamadas/{id}    transcripción y grabación de una llamada
  GET  /salud            para probar que está vivo
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response

import ghl
import llamadas

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
# Retell firma con la API key marcada como "webhook" en su panel. Si no es la misma que RETELL_APIKEY,
# se pone en RETELL_WEBHOOK_KEY. (ghl.py ya cargó el .env de la carpeta principal)
RETELL_FIRMA_KEY = os.getenv("RETELL_WEBHOOK_KEY") or os.environ["RETELL_APIKEY"]

log = logging.getLogger("middleware")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
app = FastAPI(title="Middleware Retell-GHL")
app.include_router(llamadas.router)

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


# ---------- utilidades ----------

def firma_valida(cuerpo: str, firma: str) -> bool:
    """Mismo esquema que el SDK de Retell: 'v=<ms>,d=<hmac_sha256(api_key, cuerpo + ms)>', 5 minutos de margen."""
    m = re.fullmatch(r"v=(\d+),d=([0-9a-f]{64})", firma or "")
    if not m or abs(time.time() * 1000 - int(m.group(1))) > 5 * 60 * 1000:
        return False
    esperado = hmac.new(RETELL_FIRMA_KEY.encode(), (cuerpo + m.group(1)).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, m.group(2))


def token_clinica(location_id):
    return ghl.clinica(location_id).get("retell", {}).get("token")


async def leer_firmado(request: Request) -> dict:
    """Acepta el pedido si trae el secreto de su clínica (lo pone el alta del agente en las tools y en la URL
    del webhook) o, si no, una firma de Retell válida."""
    cuerpo = (await request.body()).decode()
    datos = json.loads(cuerpo)
    secreto = request.headers.get("x-middleware-token") or request.query_params.get("token")
    if secreto:
        esperado = token_clinica(clinica_por_agente(datos.get("call", {}).get("agent_id"))) or ""
        if hmac.compare_digest(secreto, esperado):
            return datos
        log.warning("token de clínica inválido en %s", request.url.path)
        raise HTTPException(401, "token inválido")
    if not firma_valida(cuerpo, request.headers.get("x-retell-signature")):
        f = request.headers.get("x-retell-signature") or ""
        forma = re.sub(r"[0-9a-f]", "x", f)[:40]  # solo la forma, sin el valor
        log.warning("firma inválida en %s | header: %s | edad: %s s", request.url.path, forma or "(no vino)",
                    round(time.time() - int(f[2:15]) / 1000) if f.startswith("v=") and f[2:15].isdigit() else "?")
        raise HTTPException(401, "firma inválida")
    return datos


def clinica_por_agente(agent_id: str):
    location_id = ghl.datos.location_por_agente(agent_id)
    if not location_id:
        raise HTTPException(404, f"agente {agent_id} sin clínica en la base")
    return location_id


def recordar_contacto(call_id, location_id, contact_id, forzar=False):
    """Contacto al que va la nota de la llamada: el primero registrado, o el que agendó un turno."""
    ghl.datos.recordar_llamada(call_id, location_id, contact_id, forzar)


def en_palabras(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{DIAS[d.weekday()]} {d.day}/{d.month} a las {d.strftime('%H:%M')}"


# ---------- herramientas ----------

def t_buscar_o_crear_cliente(loc, a, call_id):
    contact_id, nuevo = ghl.buscar_o_crear_cliente(loc, a["nombre_completo"], a.get("telefono"), a.get("correo"))
    recordar_contacto(call_id, loc, contact_id)
    return "Paciente registrado." if nuevo else "El paciente ya estaba registrado; se actualizaron sus datos."


def t_consultar_disponibilidad(loc, a, call_id):
    r = ghl.consultar_disponibilidad(loc, a["fecha"], a.get("hora") or None)
    if r["disponible"] and r["pedido"]:
        return f"Hay turno libre el {en_palabras(r['pedido'])}."
    if not r["alternativas"]:
        return "No hay turnos libres en los proximos 7 dias a partir de esa fecha."
    opciones = "; ".join(en_palabras(s) for s in r["alternativas"])
    previo = "Ese horario no esta disponible. " if a.get("hora") else ""
    return f"{previo}Horarios libres mas cercanos: {opciones}."


def t_agendar_cita(loc, a, call_id):
    contact_id, _ = ghl.buscar_o_crear_cliente(loc, a["nombre_completo"], a.get("telefono"), a.get("correo"))
    recordar_contacto(call_id, loc, contact_id)
    r = ghl.consultar_disponibilidad(loc, a["fecha"], a["hora"])
    if not r["disponible"]:
        opciones = "; ".join(en_palabras(s) for s in r["alternativas"]) or "ninguno en los proximos 7 dias"
        return f"No se agendo: ese horario no esta disponible. Horarios libres mas cercanos: {opciones}."
    cita = ghl.agendar_cita(loc, contact_id, a["fecha"], a["hora"])
    recordar_contacto(call_id, loc, contact_id, forzar=True)
    aviso = " Le llega un correo de confirmacion." if a.get("correo") else ""
    return f"Turno agendado para el {en_palabras(cita['inicio'])}.{aviso}"


def t_enviar_correo(loc, a, call_id):
    contact_id, _ = ghl.buscar_o_crear_cliente(loc, a["nombre_completo"], a.get("telefono"), a.get("correo"))
    recordar_contacto(call_id, loc, contact_id)
    html = "<p>" + a["mensaje"].replace("\n", "<br>") + "</p>"
    ghl.enviar_correo(loc, contact_id, a["asunto"], html)
    return "Correo enviado."


HERRAMIENTAS = {
    "buscar_o_crear_cliente": t_buscar_o_crear_cliente,
    "consultar_disponibilidad": t_consultar_disponibilidad,
    "agendar_cita": t_agendar_cita,
    "enviar_correo": t_enviar_correo,
}


@app.post("/tools/{nombre}")
async def herramienta(nombre: str, request: Request):
    datos = await leer_firmado(request)
    if nombre not in HERRAMIENTAS:
        raise HTTPException(404, f"herramienta desconocida: {nombre}")
    call = datos.get("call", {})
    args = datos.get("args", {})
    loc = clinica_por_agente(call.get("agent_id"))
    log.info("tool %s | clínica %s | llamada %s | args %s", nombre, loc, call.get("call_id"), args)
    try:
        resultado = HERRAMIENTAS[nombre](loc, args, call.get("call_id"))
    except (ghl.ErrorGHL, KeyError, ValueError) as e:
        log.error("tool %s falló: %s", nombre, e)
        # Se responde 200 para que el agente lea el mensaje y siga la regla de "si una herramienta falla"
        resultado = "ERROR: no se pudo completar la operacion en el sistema de la clinica."
    log.info("tool %s -> %s", nombre, resultado)
    return {"resultado": resultado}


# ---------- fin de llamada ----------

@app.post("/retell/webhook")
async def webhook(request: Request):
    datos = await leer_firmado(request)
    evento, call = datos.get("event"), datos.get("call", {})
    log.info("webhook %s | llamada %s", evento, call.get("call_id"))
    if evento != "call_analyzed":
        return Response(status_code=204)

    loc = clinica_por_agente(call.get("agent_id"))
    contacto = (ghl.datos.llamada(call.get("call_id")) or {}).get("contact_id")
    if not contacto and call.get("from_number"):
        contacto, _ = ghl.buscar_o_crear_cliente(loc, "Paciente sin nombre", call["from_number"])
    if not contacto:
        log.info("llamada %s sin paciente identificado: no se guarda nota", call.get("call_id"))
        return Response(status_code=204)

    analisis = call.get("call_analysis") or {}
    custom = analisis.get("custom_analysis_data") or {}
    tz = ZoneInfo(ghl.clinica(loc)["timezone"])
    inicio = datetime.fromtimestamp(call.get("start_timestamp", 0) / 1000, tz).strftime("%d/%m/%Y %H:%M")
    duracion = round((call.get("end_timestamp", 0) - call.get("start_timestamp", 0)) / 1000)
    nota = "\n".join([
        f"Llamada con el agente de voz - {inicio} ({duracion} s)",
        f"Resumen: {analisis.get('call_summary') or '(sin resumen)'}",
        f"Resuelta: {'si' if analisis.get('call_successful') else 'no'}"
        + (" | URGENCIA" if custom.get("es_urgencia") else ""),
        f"Transcripcion y grabacion: {llamadas.url_llamada(loc, call.get('call_id')) or call.get('call_id')}",
    ])
    ghl.agregar_nota(loc, contacto, nota)
    log.info("nota guardada en el contacto %s", contacto)
    return Response(status_code=204)


@app.get("/salud")
def salud():
    return {"ok": True}
