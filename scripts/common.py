import os
import time
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from netmiko import ConnectHandler

# Le .env est dans /home/thz/noc-monitoring (dossier parent de scripts/ si ce fichier est dans scripts/)
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE / ".env", _HERE.parent / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate)
        break


def get_env(name, default=None):
    """Lit une variable d'environnement ; erreur claire si elle manque."""
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Variable manquante dans le .env : {name}")
    return value


# ---------------------------------------------------------------------------
# SSH routeur (lecture seule)
# ---------------------------------------------------------------------------

# Seules les commandes qui commencent par "show" sont autorisées.
ALLOWED_PREFIXES = ("show ",)


def _check_read_only(command):
    cmd = command.strip().lower()
    # Refuse aussi les retours à la ligne et les ";" (pour enchaîner une 2e commande)
    if not cmd.startswith(ALLOWED_PREFIXES) or any(c in command for c in "\n\r;"):
        raise PermissionError(
            f"Commande refusée (lecture seule, 'show' uniquement) : {command!r}"
        )


@contextmanager
def router_connection(host=None):
    """
    Ouvre une session SSH vers un routeur Nokia SR OS et la ferme à la fin.

    Utilisation :
        with router_connection() as conn:
            print(run_show(conn, "show router isis adjacency"))
    """
    host = host or get_env("ROUTER_IP")
    device = {
        "device_type": "nokia_sros",
        "host": host,
        "username": get_env("ROUTER_SSH_USER"),
        "password": get_env("ROUTER_SSH_PASSWORD"),
        "timeout": 30,
        "conn_timeout": 15,
        "auth_timeout": 15,
    }
    conn = ConnectHandler(**device)
    try:
        yield conn
    finally:
        conn.disconnect()


def run_show(conn, command):
    """Exécute UNE commande 'show' et renvoie sa sortie (texte)."""
    _check_read_only(command)
    return conn.send_command(command, read_timeout=60)


# ---------------------------------------------------------------------------
# InfluxDB
# ---------------------------------------------------------------------------

def influx_client():
    """
    Client InfluxDB. Les scripts tournent sur le serveur, donc on passe par le
    port local 127.0.0.1:18086 (celui publié par le compose).
    """
    url = os.getenv("INFLUX_URL", "http://127.0.0.1:18086")
    return InfluxDBClient(
        url=url,
        token=get_env("INFLUX_TOKEN"),
        org=get_env("INFLUX_ORG"),
        timeout=10_000,
    )


def write_points(points):
    """Écrit une liste de Point dans le bucket INFLUX_BUCKET."""
    bucket = get_env("INFLUX_BUCKET")
    with influx_client() as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=bucket, record=points)


# ---------------------------------------------------------------------------
# Suivi des scripts (mesure script_run, utilisée par le dashboard 4)
# ---------------------------------------------------------------------------

def log_script_run(script, ok, duration_s, message="", test=False):
    """
    Écrit un point script_run dans InfluxDB.
    Ne lève jamais d'exception : le suivi ne doit pas casser le script lui-même.
    Le message doit rester court et sans secret (il s'affiche dans Grafana).
    """
    try:
        message = " ".join(str(message).split())[:200]
        p = (
            Point("script_run")
            .tag("script", script)
            .tag("status", "ok" if ok else "fail")
            .field("duration_s", float(duration_s))
            .field("message", message)
        )
        if test:
            p = p.tag("test", "true")
        write_points([p])
    except Exception as exc:
        print(f"[script_run] écriture impossible : {type(exc).__name__}")


@contextmanager
def tracked_run(script, test=False, enabled=True):
    """
    Mesure la durée d'un script et enregistre son résultat, même en cas d'erreur.
    enabled=False : rien n'est écrit (à utiliser quand le script tourne sans --write).

    Utilisation :
        with tracked_run("backup.py", test=args.test, enabled=args.write) as run:
            ...
            run["message"] = "2/2 routeurs sauvegardés"
            if problème:
                run["ok"] = False
    """
    start = time.time()
    run = {"ok": True, "message": ""}
    try:
        yield run
    except Exception as exc:
        run["ok"] = False
        # Seulement le type d'erreur : le détail reste dans logs/<script>.log
        run["message"] = f"{type(exc).__name__} (voir logs)"
        raise
    finally:
        if enabled:
            log_script_run(script, run["ok"], time.time() - start, run["message"], test)


__all__ = [
    "get_env",
    "router_connection",
    "run_show",
    "influx_client",
    "write_points",
    "log_script_run",
    "tracked_run",
    "Point",
]


# ---------------------------------------------------------------------------
# Auto-test : ne modifie RIEN (pas d'écriture, une seule commande show)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("1/2 Test InfluxDB (ping)...")
    with influx_client() as c:
        print("   InfluxDB joignable :", c.ping())

    print("2/2 Test SSH routeur (show router isis adjacency)...")
    with router_connection() as conn:
        out = run_show(conn, "show router isis adjacency")
        print("\n".join(out.splitlines()[:15]))
    print("Terminé.")