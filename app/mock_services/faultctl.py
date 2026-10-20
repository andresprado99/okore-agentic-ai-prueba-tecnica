"""Mando para provocar fallos en los servicios simulados (con el servidor ya arrancado).

  python -m app.mock_services.faultctl workshops error_500            # el servicio de talleres falla siempre
  python -m app.mock_services.faultctl workshops error_500 --times 1  # falla solo la primera petición
  python -m app.mock_services.faultctl workshops slow --delay 15      # tarda 15 s en responder
  python -m app.mock_services.faultctl actions execute_then_500       # ejecuta pero pierde la respuesta
  python -m app.mock_services.faultctl show                           # ver fallos activos
  python -m app.mock_services.faultctl clear                          # quitar fallos
  python -m app.mock_services.faultctl reset                          # quitar fallos y restaurar datos
"""
import argparse

import httpx

from app.config import load_settings

parser = argparse.ArgumentParser()
parser.add_argument("target", help="claims | workshops | actions | show | clear | reset")
parser.add_argument("mode", nargs="?", choices=["ok", "error_500", "slow", "execute_then_500"])
parser.add_argument("--delay", type=float, default=10.0, help="segundos (modo slow)")
parser.add_argument("--times", type=int, default=None, help="nº de peticiones afectadas (por defecto, todas)")
args = parser.parse_args()

base = load_settings().services_base_url
try:
    if args.target == "show":
        r = httpx.get(f"{base}/_faults", timeout=5)
    elif args.target == "clear":
        r = httpx.delete(f"{base}/_faults", timeout=5)
    elif args.target == "reset":
        r = httpx.post(f"{base}/_reset", timeout=5)
    else:
        if not args.mode:
            parser.error("indica el modo: ok | error_500 | slow | execute_then_500")
        r = httpx.put(f"{base}/_faults/{args.target}", json={"mode": args.mode, "delay_s": args.delay, "times": args.times}, timeout=5)
    print(r.status_code, r.text)
except httpx.TransportError:
    raise SystemExit(f"No puedo conectar con {base}. ¿Has arrancado 'python -m app.mock_services'?")
