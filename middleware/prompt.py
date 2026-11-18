"""Arma el prompt del agente de Retell a partir de los datos de la clínica (formato clinicas.example.json)."""

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _hora(t):
    h, m = t.split(":")
    return f"{int(h)}" if m == "00" else f"{int(h)}:{m}"


def _horario(horario):
    """{"lunes": [["09:30","13:30"],["16:30","20:30"]], ...} -> texto por día; agrupa días seguidos iguales."""
    grupos = []  # [[dias], tramos]
    for d in DIAS:
        tramos = horario.get(d)
        if not tramos:
            continue
        if grupos and grupos[-1][1] == tramos and DIAS.index(grupos[-1][0][-1]) == DIAS.index(d) - 1:
            grupos[-1][0].append(d)
        else:
            grupos.append([[d], tramos])
    lineas = []
    for dias, tramos in grupos:
        nombre = dias[0] if len(dias) == 1 else f"{dias[0]} {'y' if len(dias) == 2 else 'a'} {dias[-1]}"
        lineas.append(f"  - {nombre}: " + " y ".join(f"de {_hora(a)} a {_hora(b)}" for a, b in tramos))
    cerrados = [d for d in DIAS if not horario.get(d)]
    if cerrados:
        lineas.append(f"  - cerrado: {', '.join(cerrados)}")
    return "\n".join(lineas)


def armar_prompt(c):
    a, cal, p = c["agente"], c["calendario"], c["perfil"]
    tz = p["timezone"]
    servicios = "\n".join(f"- {s['nombre']}: {s['precio']}" for s in c.get("servicios", []))
    faq = "\n".join(f"- {f['pregunta']} {f['respuesta']}" for f in c.get("faq", []))
    urg = c.get("urgencias", {})
    if urg.get("accion") == "derivar":
        regla_urgencia = f"Decile exactamente esto: \"{urg['texto']}\" y no agendes un turno comun."
    else:
        regla_urgencia = (f"Decile: \"{urg.get('texto', 'Tomamos sus datos y lo llamamos.')}\". "
                          "Toma nombre completo y telefono con la herramienta buscar_o_crear_cliente "
                          "y, al final, indica en la conversacion que es una urgencia.")
    trato = "de usted" if a["idioma"] == "es-ES" else "de vos (espanol rioplatense neutro)"

    # Campos opcionales (los completa extraer_web.py): si no están, el prompt queda como antes
    extra = c.get("contacto_extra", {})
    contacto = "".join(f"\n- {k}: {v}" for k, v in (
        ("WhatsApp", extra.get("whatsapp")), ("Otros telefonos", ", ".join(extra.get("telefonos", []))),
        ("Correo de la clinica", p.get("email") if extra else None)) if v)
    equipo = "\n".join("- " + " ".join(x for x in (m.get("titulo"), m["nombre"], m.get("apellido")) if x)
                       + (f": {m['especialidad']}" if m.get("especialidad") else "") for m in c.get("equipo", []))
    seccion_equipo = (f"""
## Equipo
{equipo}
Si preguntan por un profesional o una especialidad, contesta con esta lista. Los turnos se agendan en la agenda
general de la clinica: si el paciente quiere un profesional en particular, anotalo y avisa que la clinica se lo
confirma.
""" if equipo else "")
    info = "\n".join(f"- {x}" for x in c.get("informacion_util", []))
    seccion_info = f"\n## Informacion util\n{info}\n" if info else ""

    return f"""## Rol
Sos {a['nombre_asistente']}, la recepcionista virtual de {c['clinica']}, una clinica dental.
Atendes llamadas telefonicas: respondes dudas simples y agendas turnos. Hablale {trato}, con frases cortas,
calidas y naturales, como en una llamada real. Nunca digas que sos una inteligencia artificial salvo que te lo pregunten.

## Fecha y hora actual
Ahora es {{{{current_time_{tz}}}}} (hora de la clinica). Usala para entender "manana", "el jueves", etc.
Las fechas que pases a las herramientas van en formato AAAA-MM-DD y las horas en HH:MM (24 h), siempre en hora de la clinica.

## Datos de la clinica
- Direccion: {p['direccion']}, {p['ciudad']}
- Telefono: {p['telefono']}
- Horario de atencion:
{_horario(cal['horario'])}
- Profesional: {c['custom_values'].get('Profesional', c['profesional']['nombre'] + ' ' + c['profesional']['apellido'])}{contacto}
- Los turnos de "{cal['nombre']}" duran {cal['duracion_min']} minutos.
{seccion_equipo}{seccion_info}
## Servicios y precios
{servicios or '- Consultar en la clinica.'}

## Preguntas frecuentes
{faq or '- (sin preguntas cargadas)'}

## Como agendar un turno
1. Pregunta que dia y horario prefiere.
2. Usa consultar_disponibilidad con esa fecha y hora. Si no esta libre, ofrece como maximo dos o tres de las
   alternativas que te devuelve, dichas de forma natural (por ejemplo "el miercoles a las diez y media").
3. Cuando el paciente elija, pedi su nombre completo, su telefono y su correo electronico. Repeti el correo
   deletreando lo dudoso para confirmarlo.
4. Usa agendar_cita con fecha, hora, nombre completo, telefono y correo.
5. Confirma dia y hora del turno y avisa que le llega un correo de confirmacion.

## Reglas
- Responde solo con la informacion de esta clinica. Si no sabes algo (por ejemplo un precio que no esta en la lista),
  decilo y ofrece agendar una consulta o que la clinica lo contacte.
- No des diagnosticos ni consejos medicos.
- Urgencias (dolor fuerte, sangrado, golpe o inflamacion importante): {regla_urgencia}
- Si una herramienta falla, no inventes resultados: pedi disculpas, toma nombre y telefono, y deci que la clinica
  lo va a llamar para confirmar.
- No agendes fuera del horario de atencion ni en el pasado.
- Cuando la persona no necesite nada mas, despedite y usa end_call.
"""
