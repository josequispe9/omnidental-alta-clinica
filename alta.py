"""Alta completa de una clínica: GHL + agente de Retell + registro del middleware.

Uso:
  .venv/bin/python alta.py <slug> --location <locationId>
  .venv/bin/python alta.py <slug> --location <locationId> --dry-run

Antes: crear la subcuenta a mano en GHL (con el plan actual la API no lo permite).
Es idempotente: si se corre de nuevo, actualiza lo que ya existe.
"""
import argparse
import os
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(RAIZ, "ghl-alta"))
sys.path.insert(0, os.path.join(RAIZ, "middleware"))

import alta_ghl  # noqa: E402
import ghl_oauth  # noqa: E402
import retell_alta  # noqa: E402
from progreso import evento, resultado  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="slug de la clínica en clinicas.json")
    ap.add_argument("--location", required=True, help="ID de la subcuenta de GHL (es el ID de la clínica)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ghl_oauth.cargar_env(() if args.dry_run else alta_ghl.REQUERIDAS + ("RETELL_APIKEY", "MIDDLEWARE_URL"))
    cfg = alta_ghl.leer_clinica(os.path.join(RAIZ, "clinicas.json"), args.slug)

    evento("ghl", "inicio", f"Configurar la subcuenta de GHL de {cfg['clinica']}",
           texto=f"=== GHL: {cfg['clinica']} -> {args.location}")
    estado = alta_ghl.alta_ghl(cfg, args.location, args.dry_run)
    if not args.dry_run:
        alta_ghl.guardar_registro(cfg, estado)

    evento("retell", "inicio", f"Crear o actualizar el agente de Retell de {cfg['clinica']}",
           texto=f"\n=== Retell: agente de {cfg['clinica']}")
    if args.dry_run:
        print("    (dry-run: se omite, necesita el registro de GHL)")
        return
    r = retell_alta.alta_retell(args.slug, args.location)
    evento("fin", "ok", f"Alta lista: {cfg['clinica']}", texto="")
    resultado(location_id=args.location, calendar_id=estado["calendar_id"], pipeline_id=estado["pipeline_id"],
              agent_id=r["agent_id"], version=r["version_publicada"], vista_token=estado.get("vista_token"))

    print(f"""
=== Alta lista: {cfg['clinica']}
  Subcuenta GHL : {args.location}
  Calendario    : {estado['calendar_id']}
  Embudo        : {estado['pipeline_id']}
  Agente Retell : {r['agent_id']} (versión {r['version_publicada']})
  Menú en GHL   : "{alta_ghl.TITULO_MENU}" (recargá GHL para verlo)

Para probar: middleware y ngrok corriendo, y en Retell -> Agents -> "{cfg['clinica']} - {cfg['agente']['nombre_asistente']}" -> Test.""")


if __name__ == "__main__":
    main()
