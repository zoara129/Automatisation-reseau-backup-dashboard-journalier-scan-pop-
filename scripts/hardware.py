import argparse
import re
import sys
from pathlib import Path

import yaml

from common import Point, router_connection, run_show, tracked_run, write_points

INVENTORY = Path(__file__).resolve().parent.parent / "inventory" / "pops.yml"

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
# Analyse de "show system cpu" et "show system memory-pools"
# ---------------------------------------------------------------------------

def to_float(text):
    """'1,234' -> 1234.0 ; '~0.00%' -> 0.0 (le routeur met un ~ pour les valeurs très faibles)."""
    text = text.strip().replace(",", "").replace("%", "").replace("~", "")
    return float(text) if text else 0.0


# On ne garde que les 3 lignes du bas du tableau, pas le détail par processus :
#   Idle                                5,809,371          97.41%
#   Usage                                 154,103           2.58%
#   Busiest Core Utilization               46,576           4.68%
CPU_RES = {
    "cpu_idle_pct": re.compile(r"^\s*Idle\s+[\d,]+\s+([~\d.]+)%", re.M),
    "cpu_usage_pct": re.compile(r"^\s*Usage\s+[\d,]+\s+([~\d.]+)%", re.M),
    "cpu_busiest_core_pct": re.compile(r"^\s*Busiest Core Utilization\s+[\d,]+\s+([~\d.]+)%", re.M),
}


def parse_cpu(output):
    """Retourne l'usage CPU global en % (dict vide si le format n'est pas reconnu)."""
    fields = {}
    for name, regex in CPU_RES.items():
        m = regex.search(output)
        if m:
            fields[name] = to_float(m.group(1))
    return fields


MEM_RES = {
    "mem_total_bytes": re.compile(r"Current Total Size\s*:\s*([\d,]+)\s*bytes"),
    "mem_used_bytes": re.compile(r"Total In Use\s*:\s*([\d,]+)\s*bytes"),
    "mem_available_bytes": re.compile(r"Available Memory\s*:\s*([\d,]+)\s*bytes"),
}


def parse_memory(output):
    """Retourne la mémoire en octets + un pourcentage d'utilisation."""
    fields = {}
    for name, regex in MEM_RES.items():
        m = regex.search(output)
        if m:
            fields[name] = to_float(m.group(1))

    # Le pourcentage n'a de sens que si on a les 3 valeurs.
    # "Available Memory" = mémoire pas encore donnée aux pools, donc on l'ajoute au total.
    if len(fields) == 3:
        capacity = fields["mem_total_bytes"] + fields["mem_available_bytes"]
        if capacity > 0:
            fields["mem_used_pct"] = round(100 * fields["mem_used_bytes"] / capacity, 2)
    return fields


# ---------------------------------------------------------------------------
# Analyse de "show card state"
# ---------------------------------------------------------------------------

# Ligne type (le "(not equipped)" est sur la ligne d'après) :
# B      csmv2-10g                         up    down                    Standby
#            (not equipped)
CARD_RE = re.compile(r"^(?P<slot>\S+)\s+(?P<type>\S+)\s+(?P<admin>up|down)\s+(?P<oper>\S+)")
PROMPT_LINE_RE = re.compile(r"^[*!]?[AB]:")


def parse_cards(output):
    """Retourne une liste de dicts {slot, type, fields} avec admin_up, oper_up, equipped."""
    cards = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        # on saute les lignes vides, les indentées (dont "(not equipped)"),
        # les séparateurs, les en-têtes et l'invite du routeur
        if not line.strip() or line[0].isspace() or line[0] in "=-":
            continue
        if line.startswith(("Slot", "Id ", "Card")) or PROMPT_LINE_RE.match(line):
            continue
        m = CARD_RE.match(line)
        if not m:
            continue
        suite = lines[i + 1] if i + 1 < len(lines) else ""
        cards.append({
            "slot": m.group("slot"),
            "type": m.group("type"),
            "fields": {
                "admin_up": 1 if m.group("admin") == "up" else 0,
                # "provisioned" (carte prévue mais absente) compte comme pas up
                "oper_up": 1 if m.group("oper") == "up" else 0,
                "equipped": 0 if "not equipped" in suite else 1,
            },
        })
    return cards


# ---------------------------------------------------------------------------
# Inventaire et collecte
# ---------------------------------------------------------------------------

def load_routers():
    """Lit inventory/pops.yml -> liste de dicts {name, ip, sfp_ports (optionnel)}."""
    with open(INVENTORY, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    routers = data.get("routers") or []
    if not routers:
        sys.exit(f"Aucun routeur dans {INVENTORY}")
    for r in routers:
        if not r.get("ip"):
            sys.exit(f"Routeur sans ip dans {INVENTORY} : {r}")
    return routers


def sfp_ports_of(router, forced_port):
    """Liste des ports SFP à lire pour ce routeur (--port prend le dessus s'il est donné)."""
    if forced_port:
        return [forced_port]
    ports = router.get("sfp_ports") or []
    if isinstance(ports, str):  # tolère sfp_ports: "1/6/8" au lieu d'une liste
        ports = [ports]
    return [str(p) for p in ports]


def local_name(conn, fallback):
    """Nom du routeur d'après l'invite du CLI (sinon valeur de l'inventaire)."""
    m = PROMPT_RE.search(conn.find_prompt())
    return m.group("name") if m else fallback


def collect(router, ports):
    """Se connecte une fois et lit chaque section séparément.

    Si une commande échoue (droits, modèle différent...), on note l'erreur
    et on continue : les autres sections du routeur sont quand même lues.
    """
    ip = router["ip"]
    errors = []

    with router_connection(host=ip) as conn:
        name = local_name(conn, router.get("name", ip))

        def lire(commande, parseur):
            try:
                return parseur(run_show(conn, commande))
            except Exception as exc:
                errors.append(f"{commande} : {exc}")
                return None

        sfp = {}
        for port in ports:
            sfp[port] = lire(f"show port {port} optical", parse_sfp)

        chassis = lire("show chassis detail", parse_chassis)
        cpu = lire("show system cpu", parse_cpu)
        mem = lire("show system memory-pools", parse_memory)
        cards = lire("show card state", parse_cards)

    # CPU et mémoire vont dans la même mesure (hardware_system)
    system = None
    if cpu is not None or mem is not None:
        system = {**(cpu or {}), **(mem or {})}

    return name, {"sfp": sfp, "chassis": chassis, "system": system, "cards": cards}, errors


def build_point(measurement, tags, fields, test):
    p = Point(measurement)
    for key, value in tags.items():
        p = p.tag(key, value)
    for key, value in fields.items():
        p = p.field(key, value)
    if test:
        p = p.tag("test", "true")
    return p


def show_fields(title, fields):
    print(f"  {title} :")
    for key, value in fields.items():
        print(f"    {key} = {value}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="écrire dans InfluxDB (sinon affichage seul)")
    ap.add_argument("--test", action="store_true", help="ajoute le tag test=true (pour nettoyer ensuite)")
    ap.add_argument("--port", default=None,
                    help="port SFP à lire sur tous les routeurs (sinon sfp_ports de pops.yml)")
    args = ap.parse_args()

    if args.port and not PORT_RE.match(args.port):
        sys.exit(f"Nom de port invalide : {args.port!r}")

    routers = load_routers()

    # On valide les ports de l'inventaire avant de se connecter à quoi que ce soit
    for r in routers:
        for p in sfp_ports_of(r, args.port):
            if not PORT_RE.match(p):
                sys.exit(f"Nom de port invalide dans pops.yml ({r.get('name', r['ip'])}) : {p!r}")

    points = []
    failed = []      # routeurs injoignables
    warnings = 0     # commandes en échec sur des routeurs joignables
    with tracked_run("hardware.py", test=args.test, enabled=args.write) as run:
        for router in routers:
            label = router.get("name", router["ip"])
            ports = sfp_ports_of(router, args.port)
            try:
                name, data, errors = collect(router, ports)
            except Exception as exc:  # un routeur en panne ne doit pas bloquer les autres
                print(f"[ERREUR] {label} : {exc}")
                failed.append(label)
                continue

            print(f"{name} ({router['ip']})")

            for err in errors:
                print(f"  [ATTENTION] {err}")
            warnings += len(errors)

            # --- SFP : un point par port ---
            if not ports:
                print("  SFP : aucun port indiqué dans sfp_ports, section ignorée")
            for port, sfp in data["sfp"].items():
                if sfp:
                    show_fields(f"SFP {port}", sfp)
                    points.append(build_point("hardware_sfp", {"router": name, "port": port}, sfp, args.test))
                elif sfp is not None:
                    print(f"  SFP {port} : aucune donnée DDM (port sans module ou sans DDM)")

            # --- Châssis ---
            if data["chassis"]:
                show_fields("Châssis", data["chassis"])
                points.append(build_point("hardware_chassis", {"router": name}, data["chassis"], args.test))
            elif data["chassis"] is not None:
                print("  Châssis : aucune donnée reconnue (format de sortie inattendu ?)")

            # --- CPU / mémoire ---
            if data["system"]:
                show_fields("CPU / mémoire", data["system"])
                points.append(build_point("hardware_system", {"router": name}, data["system"], args.test))
            elif data["system"] is not None:
                print("  CPU / mémoire : aucune donnée reconnue (format de sortie inattendu ?)")

            # --- Cartes : un point par carte ---
            if data["cards"]:
                print("  Cartes :")
                for card in data["cards"]:
                    f = card["fields"]
                    print(f"    {card['slot']} ({card['type']}) admin_up={f['admin_up']} "
                          f"oper_up={f['oper_up']} equipped={f['equipped']}")
                    points.append(build_point(
                        "hardware_card",
                        {"router": name, "slot": card["slot"], "type": card["type"]},
                        f, args.test,
                    ))
            elif data["cards"] is not None:
                print("  Cartes : aucune donnée reconnue (format de sortie inattendu ?)")

        if not args.write:
            print("\nMode affichage : rien n'a été écrit (ajoute --write pour écrire).")
        else:
            if points:
                write_points(points)
                print(f"\n{len(points)} point(s) écrit(s) dans InfluxDB.")
            else:
                print("\nAucun point à écrire : rien n'est envoyé.")
            message = f"{len(routers) - len(failed)}/{len(routers)} routeur(s) lu(s)"
            if warnings:
                message += f", {warnings} commande(s) en échec"
            run["message"] = message
            if failed:
                run["ok"] = False

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()