import argparse
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from common import Point, router_connection, run_show, write_points

DEFAULT_INVENTORY = Path(__file__).resolve().parent.parent / "inventory" / "pops.yml"

# Invite type : "A:SAR-8_Tour_87#" ou "*A:SAR-8_Tour_87#"
PROMPT_RE = re.compile(r"[*!]?[AB]:(?P<name>[^#>\s]+)")
VERSION_RE = re.compile(r"TiMOS-\S+")

SSH_PORT = 22
TCP_TIMEOUT = 5          # secondes
MAX_WORKERS = 5          # plafond de connexions SSH simultanées (ne pas dépasser)


def load_routers(path):
    """Lit pops.yml -> liste de dicts {name, ip}. Ignore les entrées incomplètes."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    routers = []
    for entry in data.get("routers") or []:
        if entry.get("name") and entry.get("ip"):
            routers.append(entry)
        else:
            print(f"[IGNORÉ] entrée incomplète (name et ip requis) : {entry}")
    if not routers:
        sys.exit(f"Aucun routeur valide dans {path}")
    return routers


def scan_router(router):
    """Teste un routeur. Ne lève jamais d'exception : renvoie un dict de résultat."""
    name, ip = router["name"], router["ip"]
    result = {
        "name": name, "ip": ip,
        "reachable": 0, "ssh_ok": 0,
        "latency_ms": None, "cli_name": None, "version": None, "error": None,
    }

    # 1) Le port 22 répond-il ?
    t0 = time.monotonic()
    try:
        with socket.create_connection((ip, SSH_PORT), timeout=TCP_TIMEOUT):
            pass
        result["reachable"] = 1
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
    except OSError as exc:
        result["error"] = f"port {SSH_PORT} injoignable : {exc}"
        return result

    # 2) SSH + une seule commande show
    try:
        with router_connection(host=ip) as conn:
            m = PROMPT_RE.search(conn.find_prompt())
            result["cli_name"] = m.group("name") if m else None
            out = run_show(conn, "show version")
        v = VERSION_RE.search(out)
        result["version"] = v.group(0) if v else None
        result["ssh_ok"] = 1
    except Exception as exc:  # un routeur en panne ne doit pas bloquer les autres
        result["error"] = f"SSH : {exc}"
    return result


def to_point(result, test):
    p = (
        Point("pop_scan")
        .tag("router", result["name"])   # nom de pops.yml : toujours le même, même en cas d'échec
        .field("reachable", result["reachable"])
        .field("ssh_ok", result["ssh_ok"])
    )
    if result["latency_ms"] is not None:
        p = p.field("latency_ms", result["latency_ms"])
    if result["version"]:
        p = p.field("version", result["version"])
    if test:
        p = p.tag("test", "true")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="écrire dans InfluxDB (sinon affichage seul)")
    ap.add_argument("--test", action="store_true", help="ajoute le tag test=true (pour nettoyer ensuite)")
    ap.add_argument("--only", help="ne scanner que ce routeur (nom dans pops.yml)")
    ap.add_argument("--inventory", default=str(DEFAULT_INVENTORY), help="fichier d'inventaire (défaut : inventory/pops.yml)")
    ap.add_argument("--workers", type=int, default=3, help=f"connexions simultanées (défaut 3, max {MAX_WORKERS})")
    args = ap.parse_args()

    routers = load_routers(args.inventory)
    if args.only:
        routers = [r for r in routers if r["name"] == args.only]
        if not routers:
            sys.exit(f"Routeur introuvable dans l'inventaire : {args.only}")

    workers = max(1, min(args.workers, MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(scan_router, routers))

    ok = 0
    for r in results:
        if r["ssh_ok"]:
            ok += 1
            print(f"[OK]     {r['name']} ({r['ip']}) : {r['latency_ms']} ms, {r['version'] or 'version inconnue'}")
            if r["cli_name"] and r["cli_name"] != r["name"]:
                print(f"         attention : nom CLI '{r['cli_name']}' différent du nom dans pops.yml")
        else:
            print(f"[ERREUR] {r['name']} ({r['ip']}) : {r['error']}")
    print(f"\n{ok}/{len(results)} routeur(s) OK")

    if not args.write:
        print("Mode affichage : rien n'a été écrit (ajoute --write pour écrire).")
        return
    try:
        write_points([to_point(r, args.test) for r in results])
        print(f"{len(results)} point(s) écrit(s) dans InfluxDB.")
    except Exception as exc:
        print(f"[ERREUR] écriture InfluxDB impossible : {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()