import argparse
import re
import sys
from pathlib import Path

import yaml

from common import Point, router_connection, run_show, tracked_run, write_points

INVENTORY = Path(__file__).resolve().parent.parent / "inventory" / "pops.yml"
DEFAULT_PORT = "1/6/8"

# Un nom de port ne contient que des lettres, chiffres et "/" (ex. 1/6/8, A/1)
PORT_RE = re.compile(r"^[A-Za-z0-9]+(/[A-Za-z0-9]+)*$")

# Invite type : "A:SAR-8_Tour_87#" ou "*A:SAR-8_Tour_87#"
PROMPT_RE = re.compile(r"[*!]?[AB]:(?P<name>[^#>\s]+)")

# ---------------------------------------------------------------------------
# Analyse de "show port <port> optical"
# ---------------------------------------------------------------------------

# Ligne type :
# Temperature (C)               +44.9    +110.0      +93.0      -30.0      -40.0
DDM_RE = re.compile(r"^(?P<label>.+?)\s{2,}(?P<value>[+-]?\d+(?:\.\d+)?)(?:\s|$)")

DDM_FIELDS = {
    "Temperature (C)": "sfp_temp_c",
    "Supply Voltage (V)": "sfp_voltage_v",
    "Tx Bias Current (mA)": "sfp_tx_bias_ma",
    "Tx Output Power (dBm)": "sfp_tx_power_dbm",
    "Rx Optical Power (avg dBm)": "sfp_rx_power_dbm",
}

SFP_STATUS_RE = re.compile(r"Transceiver Status\s*:\s*(\S+)")


def parse_sfp(output):
    """Retourne un dict de champs. Dict vide si le port n'a pas de SFP/DDM."""
    fields = {}
    m = SFP_STATUS_RE.search(output)
    if m:
        fields["sfp_ok"] = 1 if m.group(1).lower() == "operational" else 0
    for line in output.splitlines():
        m = DDM_RE.match(line.strip())
        if not m:
            continue
        name = DDM_FIELDS.get(m.group("label").strip())
        if name:
            # Toujours des float : InfluxDB refuse de mélanger int et float
            fields[name] = float(m.group("value"))
    return fields


# ---------------------------------------------------------------------------
# Analyse de "show chassis detail"
# ---------------------------------------------------------------------------

OVERTEMP_RE = re.compile(r"Over Temperature state\s*:\s*(\S+)")

FAN_RE = re.compile(
    r"# of on-board fans\s*:\s*(?P<n>\d+)\s+"
    r"Status\s*:\s*(?P<status>\S+)\s+"
    r"Speed\s*:\s*(?P<speed>\S+)"
)

FEED_RE = re.compile(
    r"Input power feed\s*:\s*(?P<feed>\S+)\s+"
    r"Type\s*:\s*(?P<type>\S+)\s+"
    r"Status\s*:\s*(?P<status>\S+)"
)


def parse_chassis(output):
    """Retourne un dict de champs entiers (1 = OK, 0 = problème)."""
    fields = {}

    m = OVERTEMP_RE.search(output)
    if m:
        fields["over_temp_ok"] = 1 if m.group(1).upper() == "OK" else 0

    m = FAN_RE.search(output)
    if m:
        fields["fan_count"] = int(m.group("n"))
        fields["fan_up"] = 1 if m.group("status").lower() == "up" else 0
        fields["fan_speed_normal"] = 1 if m.group("speed").lower() == "normal" else 0

    for m in FEED_RE.finditer(output):
        name = f"power_feed_{m.group('feed').lower()}_up"
        fields[name] = 1 if m.group("status").lower() == "up" else 0

    return fields


# ---------------------------------------------------------------------------
# Inventaire et collecte
# ---------------------------------------------------------------------------

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


def collect(router, port):
    ip = router["ip"]
    with router_connection(host=ip) as conn:
        name = local_name(conn, router.get("name", ip))
        sfp_out = run_show(conn, f"show port {port} optical")
        chassis_out = run_show(conn, "show chassis detail")
    return name, parse_sfp(sfp_out), parse_chassis(chassis_out)


def build_point(measurement, tags, fields, test):
    p = Point(measurement)
    for key, value in tags.items():
        p = p.tag(key, value)
    for key, value in fields.items():
        p = p.field(key, value)
    if test:
        p = p.tag("test", "true")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="écrire dans InfluxDB (sinon affichage seul)")
    ap.add_argument("--test", action="store_true", help="ajoute le tag test=true (pour nettoyer ensuite)")
    ap.add_argument("--port", default=DEFAULT_PORT, help=f"port à lire (défaut {DEFAULT_PORT})")
    args = ap.parse_args()

    if not PORT_RE.match(args.port):
        sys.exit(f"Nom de port invalide : {args.port!r}")

    routers = load_routers()
    points = []
    failed = []
    with tracked_run("hardware.py", test=args.test, enabled=args.write) as run:
        for router in routers:
            label = router.get("name", router["ip"])
            try:
                name, sfp, chassis = collect(router, args.port)
            except Exception as exc:  # un routeur en panne ne doit pas bloquer les autres
                print(f"[ERREUR] {label} : {exc}")
                failed.append(label)
                continue

            print(f"{name} ({router['ip']})")

            if sfp:
                print(f"  SFP {args.port} :")
                for key, value in sfp.items():
                    print(f"    {key} = {value}")
                points.append(
                    build_point("hardware_sfp", {"router": name, "port": args.port}, sfp, args.test)
                )
            else:
                print(f"  SFP {args.port} : aucune donnée DDM (port sans module ou sans DDM)")

            if chassis:
                print("  Châssis :")
                for key, value in chassis.items():
                    print(f"    {key} = {value}")
                points.append(build_point("hardware_chassis", {"router": name}, chassis, args.test))
            else:
                print("  Châssis : aucune donnée reconnue (format de sortie inattendu ?)")

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