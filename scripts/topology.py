import argparse
import re
import sys
from pathlib import Path

import yaml

from common import Point, router_connection, run_show, tracked_run, write_points

INVENTORY = Path(__file__).resolve().parent.parent / "inventory" / "pops.yml"

# Ligne type :
# 7250_IXR_R_Tour_86       L2    Up    22   To_Tour_87                    0
ADJ_RE = re.compile(
    r"^(?P<neighbor>\S+)\s+(?P<level>L1L2|L1|L2)\s+(?P<state>\S+)\s+"
    r"(?P<hold>\d+)\s+(?P<interface>\S+)\s+(?P<mt>\d+)\s*$"
)

# Invite type : "A:SAR-8_Tour_87#" ou "*A:SAR-8_Tour_87#"
PROMPT_RE = re.compile(r"[*!]?[AB]:(?P<name>[^#>\s]+)")


def load_routers():
    """Lit inventory/pops.yml -> liste de dicts {name, ip}."""
    with open(INVENTORY, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    routers = data.get("routers") or []
    if not routers:
        sys.exit(f"Aucun routeur dans {INVENTORY}")
    return routers


def local_name(conn, fallback):
    """Nom du routeur d'après l'invite du CLI (sinon valeur de l'inventaire)."""
    m = PROMPT_RE.search(conn.find_prompt())
    return m.group("name") if m else fallback


def parse_adjacencies(output):
    links = []
    for line in output.splitlines():
        m = ADJ_RE.match(line.strip())
        if m:
            links.append(m.groupdict())
    return links


def collect(router):
    ip = router["ip"]
    with router_connection(host=ip) as conn:
        name = local_name(conn, router.get("name", ip))
        out = run_show(conn, "show router isis adjacency")
    return name, parse_adjacencies(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="écrire dans InfluxDB (sinon affichage seul)")
    ap.add_argument("--test", action="store_true", help="ajoute le tag test=true (pour nettoyer ensuite)")
    args = ap.parse_args()

    routers = load_routers()
    points = []
    failed = []
    with tracked_run("topology.py", test=args.test, enabled=args.write) as run:
        for router in routers:
            label = router.get("name", router["ip"])
            try:
                name, links = collect(router)
            except Exception as exc:  # un routeur en panne ne doit pas bloquer les autres
                print(f"[ERREUR] {label} : {exc}")
                failed.append(label)
                continue

            print(f"{name} ({router['ip']}) : {len(links)} adjacence(s) IS-IS")
            for link in links:
                print(f"  -> {link['neighbor']}  {link['level']}  {link['state']}  via {link['interface']}")
                p = (
                    Point("topology_link")
                    .tag("router", name)
                    .tag("neighbor", link["neighbor"])
                    .tag("interface", link["interface"])
                    .tag("level", link["level"])
                    .field("up", 1 if link["state"].lower() == "up" else 0)
                    .field("hold", int(link["hold"]))
                )
                if args.test:
                    p = p.tag("test", "true")
                points.append(p)

        if not args.write:
            print("\nMode affichage : rien n'a été écrit (ajoute --write pour écrire).")
        else:
            if points:
                write_points(points)
                print(f"\n{len(points)} point(s) écrit(s) dans InfluxDB.")
            else:
                print("\nAucun point à écrire : rien n'est envoyé.")
            run["message"] = f"{len(routers) - len(failed)}/{len(routers)} routeur(s) lu(s)"
            if failed:
                run["ok"] = False

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()