"""Páginas para ver las llamadas de una clínica desde GHL (menú personalizado en modo iframe).

La transcripción y la grabación no se copian a GHL: se leen de Retell en el momento.
Acceso: ?location=<locationId>&token=<vista_token de la clínica en la base>
"""
import html
import os
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

import ghl

router = APIRouter()
RETELL = "https://api.retellai.com"


def url_llamada(location_id, call_id):
    """Link directo a una llamada (va en la nota del contacto). None si la clínica no tiene página de llamadas."""
    token = ghl.clinica(location_id).get("vista_token")
    if not token:
        return None
    base = os.environ["MIDDLEWARE_URL"].rstrip("/")
    return f"{base}/llamadas/{call_id}?" + urlencode({"location": location_id, "token": token})


def _retell(metodo, ruta, **kwargs):
    r = requests.request(metodo, RETELL + ruta, timeout=30,
                         headers={"Authorization": f"Bearer {os.environ['RETELL_APIKEY']}"}, **kwargs)
    if r.status_code >= 400:
        raise HTTPException(502, f"Retell respondió {r.status_code}")
    return r.json()


def _acceso(location, token):
    try:
        c = ghl.clinica(location)
    except ghl.ErrorGHL:
        raise HTTPException(404, "clínica desconocida")
    if not token or token != c.get("vista_token"):
        raise HTTPException(401, "acceso no autorizado")
    return c


def _fecha(ms, tz):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(tz)).strftime("%d/%m/%Y %H:%M") if ms else "-"


def _pagina(titulo, cuerpo):
    return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(titulo)}</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px;background:#f7f8fa;color:#1d2433}}
 h1{{font-size:20px;margin:0 0 16px}} a{{color:#155eef;text-decoration:none}} a:hover{{text-decoration:underline}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e4e7ec;border-radius:8px;overflow:hidden}}
 th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #eef0f3;font-size:14px;vertical-align:top}}
 th{{background:#f2f4f7;font-weight:600;color:#475467}}
 .tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}}
 .ok{{background:#dcfae6;color:#067647}} .no{{background:#fef0c7;color:#93370d}} .urg{{background:#fee4e2;color:#b42318}}
 .caja{{background:#fff;border:1px solid #e4e7ec;border-radius:8px;padding:16px;margin-bottom:16px}}
 .msg{{margin:8px 0;padding:8px 12px;border-radius:8px;max-width:80%;font-size:14px;line-height:1.4}}
 .agent{{background:#eef4ff}} .user{{background:#f2f4f7;margin-left:auto}}
 .quien{{font-size:12px;color:#667085;margin-bottom:2px}} audio{{width:100%}}
</style></head><body>{cuerpo}</body></html>""")


@router.get("/llamadas", response_class=HTMLResponse)
def lista(location: str, token: str = None):
    c = _acceso(location, token)
    agent_id = c.get("retell", {}).get("agent_id")
    tz = c["timezone"]
    llamadas = [x for x in _retell("POST", "/v3/list-calls", json={"limit": 200})["items"]
                if x.get("agent_id") == agent_id]
    filas = []
    for x in llamadas:
        a = x.get("call_analysis") or {}
        estado = ('<span class="tag ok">resuelta</span>' if a.get("call_successful")
                  else '<span class="tag no">no resuelta</span>' if a else "-")
        if (a.get("custom_analysis_data") or {}).get("es_urgencia"):
            estado += ' <span class="tag urg">urgencia</span>'
        dur = round((x.get("end_timestamp", 0) - x.get("start_timestamp", 0)) / 1000) if x.get("end_timestamp") else 0
        link = f"/llamadas/{x['call_id']}?" + urlencode({"location": location, "token": token})
        filas.append(f"<tr><td><a href='{link}'>{_fecha(x.get('start_timestamp'), tz)}</a></td><td>{dur} s</td>"
                     f"<td>{html.escape(a.get('call_summary') or '(sin resumen todavía)')}</td><td>{estado}</td></tr>")
    tabla = ("<table><tr><th>Fecha</th><th>Duración</th><th>Resumen</th><th>Estado</th></tr>"
             + "".join(filas) + "</table>") if filas else "<p>Todavía no hay llamadas.</p>"
    return _pagina("Llamadas", f"<h1>Llamadas del agente de voz - {html.escape(c['clinica'])}</h1>{tabla}")


@router.get("/llamadas/{call_id}", response_class=HTMLResponse)
def detalle(call_id: str, location: str, token: str = None):
    c = _acceso(location, token)
    x = _retell("GET", f"/v2/get-call/{call_id}")
    if x.get("agent_id") != c.get("retell", {}).get("agent_id"):
        raise HTTPException(404, "la llamada no es de esta clínica")
    a = x.get("call_analysis") or {}
    audio = (f"<audio controls preload='none' src='{html.escape(x['recording_url'])}'></audio>"
             if x.get("recording_url") else "<p>Sin grabación.</p>")
    mensajes = "".join(
        f"<div class='msg {'agent' if m.get('role') == 'agent' else 'user'}'>"
        f"<div class='quien'>{'Agente' if m.get('role') == 'agent' else 'Paciente'}</div>{html.escape(m.get('content', ''))}</div>"
        for m in x.get("transcript_object") or [])
    volver = "/llamadas?" + urlencode({"location": location, "token": token})
    return _pagina("Llamada", f"""<p><a href="{volver}">&larr; Todas las llamadas</a></p>
<h1>Llamada del {_fecha(x.get('start_timestamp'), c['timezone'])}</h1>
<div class="caja"><b>Resumen:</b> {html.escape(a.get('call_summary') or '(sin resumen)')}</div>
<div class="caja"><b>Grabación</b>{audio}</div>
<div class="caja"><b>Transcripción</b>{mensajes or '<p>Sin transcripción.</p>'}</div>""")
