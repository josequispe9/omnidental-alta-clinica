"""Crea (o actualiza) el agente de Retell de una clínica y lo guarda en registro.json.

Uso:
  python retell_alta.py <slug> --location <locationId>             # crea o actualiza y publica
  python retell_alta.py <slug> --location <locationId> --dry-run   # muestra lo que enviaría

Necesita RETELL_APIKEY y MIDDLEWARE_URL en el .env de la carpeta principal, y que la clínica
ya tenga el alta de GHL hecha (registro.json). Si la clínica ya tiene agente, lo actualiza en vez de crear otro.
"""
import argparse
import json
import os
import secrets
import sys

import requests

from prompt import armar_prompt

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "ghl-alta"))
import ghl_oauth  # noqa: E402  (solo para leer el .env)

sys.path.insert(0, RAIZ)
from progreso import evento  # noqa: E402

REGISTRO = os.path.join(RAIZ, "registro.json")
CLINICAS = os.path.join(RAIZ, "clinicas.json")
BASE = "https://api.retellai.com"
MODELO = "gpt-4.1"


def cargar_env():
    ghl_oauth.cargar_env(("RETELL_APIKEY", "MIDDLEWARE_URL"))


def retell(metodo, ruta, **kwargs):
    r = requests.request(metodo, BASE + ruta, timeout=30, headers={
        "Authorization": f"Bearer {os.environ['RETELL_APIKEY']}"}, **kwargs)
    if r.status_code >= 400:
        sys.exit(f"{metodo} {ruta} -> HTTP {r.status_code}: {r.text[:500]}")
    return r.json() if r.text else {}


def _tool(token, nombre, descripcion, mensaje_espera, propiedades, requeridos):
    return {
        "type": "custom", "name": nombre, "description": descripcion,
        "url": f"{os.environ['MIDDLEWARE_URL'].rstrip('/')}/tools/{nombre}", "method": "POST",
        # el dominio gratuito de ngrok muestra una página de aviso si no va este header
        # X-Middleware-Token: secreto de la clínica para que el middleware sepa que el pedido viene de su agente
        "headers": {"ngrok-skip-browser-warning": "1", "X-Middleware-Token": token},
        "parameters": {"type": "object", "properties": propiedades, "required": requeridos},
        "speak_during_execution": True, "execution_message_description": mensaje_espera,
        "speak_after_execution": True, "timeout_ms": 20000,
    }


TXT = {"type": "string"}


def herramientas(token):
    nombre = {**TXT, "description": "Nombre y apellido del paciente"}
    telefono = {**TXT, "description": "Telefono del paciente, con codigo de area"}
    correo = {**TXT, "description": "Correo electronico del paciente"}
    fecha = {**TXT, "description": "Fecha en formato AAAA-MM-DD"}
    hora = {**TXT, "description": "Hora en formato HH:MM (24 h), hora de la clinica"}
    return [
        _tool(token, "buscar_o_crear_cliente",
              "Registra al paciente en el sistema de la clinica (si ya existe, lo reutiliza).",
              "Avisale que estas registrando sus datos.",
              {"nombre_completo": nombre, "telefono": telefono, "correo": correo}, ["nombre_completo", "telefono"]),
        _tool(token, "consultar_disponibilidad",
              "Revisa si hay turno libre en una fecha y hora. Si no hay, devuelve los horarios libres mas cercanos.",
              "Avisale que estas revisando la agenda.",
              {"fecha": fecha, "hora": hora}, ["fecha"]),
        _tool(token, "agendar_cita",
              "Agenda el turno del paciente en la fecha y hora elegidas. Registra al paciente si no existe.",
              "Avisale que estas agendando el turno.",
              {"fecha": fecha, "hora": hora, "nombre_completo": nombre, "telefono": telefono, "correo": correo},
              ["fecha", "hora", "nombre_completo", "telefono"]),
        _tool(token, "enviar_correo",
              "Envia un correo al paciente (por ejemplo con la direccion o las indicaciones que pidio).",
              "Avisale que le estas enviando el correo.",
              {"nombre_completo": nombre, "telefono": telefono, "correo": correo,
               "asunto": {**TXT, "description": "Asunto del correo"},
               "mensaje": {**TXT, "description": "Texto del correo"}},
              ["nombre_completo", "correo", "asunto", "mensaje"]),
        {"type": "end_call", "name": "end_call", "description": "Termina la llamada cuando el paciente se despide."},
    ]


def cuerpo_llm(c, token):
    return {
        "model_config": {"type": "cascading", "primary": {"model": MODELO}, "temperature": 0.2},
        "general_prompt": armar_prompt(c),
        "begin_message": c["agente"]["saludo"],
        "start_speaker": "agent",
        "general_tools": herramientas(token),
    }


def palabras_clave(c):
    """Nombres propios que el reconocimiento de voz suele escribir mal: clínica, profesionales, calle, ciudad
    y tratamientos. Retell los usa para sesgar la transcripción (boosted_keywords)."""
    import re
    p = c["perfil"]
    personas = [c["profesional"]] + c.get("equipo", [])
    candidatas = [c["clinica"], c["agente"]["nombre_asistente"], p.get("ciudad", ""),
                  re.split(r"\s*\d|,", p.get("direccion", ""))[0]]
    candidatas += [x for m in personas for x in (m.get("nombre", ""), m.get("apellido", ""))]
    candidatas += [s["nombre"] for s in c.get("servicios", []) if len(s["nombre"].split()) <= 3]
    vistas, salida = set(), []
    for x in candidatas:
        x = x.strip()
        if x and x != "COMPLETAR" and x.lower() not in vistas:
            vistas.add(x.lower())
            salida.append(x)
    return salida[:40]


def cuerpo_agente(c, llm_id, token):
    a = c["agente"]
    return {
        "agent_name": f"{c['clinica']} - {a['nombre_asistente']}",
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "voice_id": a["voz"],
        "language": a["idioma"],
        "timezone": c["perfil"]["timezone"],
        "boosted_keywords": palabras_clave(c),
        "webhook_url": f"{os.environ['MIDDLEWARE_URL'].rstrip('/')}/retell/webhook?token={token}",
        "webhook_events": ["call_analyzed"],
        "post_call_analysis_data": [
            {"type": "system-presets", "name": "call_summary",
             "description": "Resumen breve en espanol: motivo de la llamada, datos del paciente y turno agendado si lo hubo."},
            {"type": "system-presets", "name": "call_successful"},
            {"type": "boolean", "name": "es_urgencia",
             "description": "true si el paciente menciono una urgencia (dolor fuerte, sangrado, golpe)."},
        ],
        "end_call_after_silence_ms": 30000,
        "max_call_duration_ms": 900000,
    }


def alta_retell(slug, location_id, dry_run=False):
    with open(CLINICAS) as f:
        c = next((x for x in json.load(f) if x["slug"] == slug), None)
    if not c:
        sys.exit(f"No existe la clínica '{slug}' en clinicas.json")
    with open(REGISTRO) as f:
        registro = json.load(f)
    if location_id not in registro:
        sys.exit(f"{location_id} no está en registro.json: corré primero el alta de GHL")
    previo = registro[location_id].get("retell", {})

    # Secreto de la clínica: se genera una vez y se reutiliza en las actualizaciones
    token = previo.get("token") or secrets.token_urlsafe(24)
    llm = cuerpo_llm(c, token)
    if dry_run:
        print(json.dumps(llm, indent=2, ensure_ascii=False))
        print(json.dumps(cuerpo_agente(c, previo.get("llm_id", "LLM_ID"), token), indent=2, ensure_ascii=False))
        return

    if previo.get("agent_id"):
        # Las versiones publicadas no se pueden editar: se crea un borrador a partir de la última
        # (Retell clona también el LLM con el mismo número de versión), se edita y se publica.
        agent_id, llm_id = previo["agent_id"], previo["llm_id"]
        ultima = retell("GET", f"/list-agent-versions/{agent_id}", params={"limit": 1})["items"][0]
        version = ultima["version"]
        if ultima["is_published"]:
            version = retell("POST", f"/create-agent-version/{agent_id}", json={"base_version": version})["version"]
            evento("retell", "R0", f"Borrador nuevo: versión {version}", texto=f"[R0] Borrador nuevo: versión {version}")
        retell("PATCH", f"/update-retell-llm/{llm_id}", params={"version": version}, json=llm)
        evento("retell", "R1", f"LLM actualizado (versión {version})", texto=f"[R1] LLM actualizado: {llm_id} (versión {version})")
        agente = cuerpo_agente(c, llm_id, token)
        agente["response_engine"]["version"] = version
        retell("PATCH", f"/update-agent/{agent_id}", params={"version": version}, json=agente)
        evento("retell", "R2", f"Agente actualizado (versión {version})", texto=f"[R2] Agente actualizado: {agent_id} (versión {version})")
    else:
        llm_id = retell("POST", "/create-retell-llm", json=llm)["llm_id"]
        evento("retell", "R1", "LLM creado", texto=f"[R1] LLM creado: {llm_id}")
        r = retell("POST", "/create-agent", json=cuerpo_agente(c, llm_id, token))
        agent_id, version = r["agent_id"], r.get("version", 0)
        evento("retell", "R2", "Agente creado", texto=f"[R2] Agente creado: {agent_id}")

    retell("POST", f"/publish-agent-version/{agent_id}", json={"version": version})
    evento("retell", "R3", f"Agente publicado (versión {version})", texto=f"[R3] Agente publicado (versión {version})")

    # Deja solo la versión publicada: si quedan versiones viejas, el botón Test del panel puede usar una de ellas.
    # Retell no deja borrar la versión 0 (por eso el secreto va desde la creación del agente).
    viejas = [v["version"] for v in retell("GET", f"/list-agent-versions/{agent_id}", params={"limit": 1000})["items"]
              if v["version"] not in (version, 0)]
    for v in viejas:
        r = requests.delete(f"{BASE}/delete-agent-version/{agent_id}", params={"version": v}, timeout=30,
                            headers={"Authorization": f"Bearer {os.environ['RETELL_APIKEY']}"})
        if r.status_code >= 400:
            evento("retell", "R4", f"No se pudo borrar la versión {v}: {r.text[:200]}", nivel="aviso",
                   texto=f"    aviso: no se pudo borrar la versión {v}: {r.text[:200]}")
    if viejas:
        evento("retell", "R4", f"Versiones viejas borradas: {sorted(viejas)}", texto=f"[R4] Versiones viejas borradas: {sorted(viejas)}")

    registro[location_id]["retell"] = {"agent_id": agent_id, "llm_id": llm_id, "version_publicada": version,
                                       "token": token}
    with open(REGISTRO, "w") as f:
        json.dump(registro, f, indent=2, ensure_ascii=False)
    print(f"IDs guardados en {REGISTRO}")
    return registro[location_id]["retell"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug")
    ap.add_argument("--location", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cargar_env()
    alta_retell(args.slug, args.location, args.dry_run)
