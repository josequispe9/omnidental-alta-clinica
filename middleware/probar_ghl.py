"""Prueba las funciones de ghl.py contra una clínica y al final borra lo que creó.

Uso: python probar_ghl.py <locationId> <correo_de_prueba>
"""
import sys
from datetime import date, timedelta

import ghl

loc, correo = sys.argv[1], sys.argv[2]
dia = date.today() + timedelta(days=1)
while dia.isoweekday() > 5:  # buscar un día hábil
    dia += timedelta(days=1)

contact_id = cita = None
try:
    contact_id, nuevo = ghl.buscar_o_crear_cliente(loc, "Paciente Prueba Middleware", "+5492615550000", correo)
    print("1. cliente:", contact_id, "(nuevo)" if nuevo else "(ya existía)")
    again, nuevo2 = ghl.buscar_o_crear_cliente(loc, "Paciente Prueba Middleware", "+5492615550000", correo)
    print("   segunda vez:", "mismo contacto" if again == contact_id else f"OTRO contacto {again}", "| nuevo:", nuevo2)

    d = ghl.consultar_disponibilidad(loc, dia.isoformat(), "10:00")
    print(f"2. disponibilidad {dia} 10:00:", d)
    hora = "10:00" if d["disponible"] else d["alternativas"][0][11:16]
    d2 = ghl.consultar_disponibilidad(loc, dia.isoformat(), "23:00")
    print("   a las 23:00 (fuera de horario):", d2)

    r = ghl.agendar_cita(loc, contact_id, dia.isoformat(), hora)
    cita = r["cita_id"]
    print("3. cita:", r)
    d3 = ghl.consultar_disponibilidad(loc, dia.isoformat(), hora)
    print(f"   {hora} después de agendar:", "ocupado" if not d3["disponible"] else "SIGUE LIBRE", d3["alternativas"])

    op2 = ghl.mover_oportunidad(loc, contact_id, "Turno agendado")
    print("4. oportunidad:", "reutilizada" if op2 == r["oportunidad_id"] else f"DUPLICADA {op2}")

    print("5. nota:", ghl.agregar_nota(loc, contact_id, "Prueba del middleware: resumen de llamada de ejemplo."))
    print("6. correo:", ghl.enviar_correo(loc, contact_id, "Prueba del middleware",
                                         "<p>Este es un correo de prueba del middleware.</p>"))
finally:
    if cita:
        ghl.cancelar_cita(loc, cita)
        print("limpieza: cita borrada")
    if contact_id:
        ghl._pedir(loc, "DELETE", f"/contacts/{contact_id}")
        print("limpieza: contacto borrado (con su oportunidad y nota)")
