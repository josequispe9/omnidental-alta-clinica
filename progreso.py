"""Eventos de avance de las automatizaciones (extracción, subcuenta, GHL, Retell).

En la terminal se imprime el texto de siempre. Si el proceso corre desde el panel de administración
(variable ALTA_EVENTOS=json), cada evento sale como una línea JSON que el panel guarda y muestra.

  evento("ghl", "2a", "Igualar el horario de trabajo del profesional", texto="[2a] Igualar ...")
  resultado(location_id="abc")   # datos que el panel necesita al terminar (solo en modo JSON)
"""
import json
import os
import time

JSON = os.getenv("ALTA_EVENTOS") == "json"


def _emitir(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def evento(etapa, paso, mensaje, nivel="info", texto=None, **datos):
    """etapa: extraccion | subcuenta | ghl | retell | fin. nivel: info | aviso | error."""
    if JSON:
        _emitir({"tipo": "evento", "ts": time.time(), "etapa": etapa, "paso": str(paso), "nivel": nivel,
                 "mensaje": mensaje, **({"datos": datos} if datos else {})})
    else:
        print(texto if texto is not None else mensaje, flush=True)


def resultado(**datos):
    if JSON:
        _emitir({"tipo": "resultado", "ts": time.time(), "datos": datos})
