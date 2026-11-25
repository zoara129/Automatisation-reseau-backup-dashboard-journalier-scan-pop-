import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import yaml

from common import Point, get_env, router_connection, tracked_run, write_points

BASE = Path(__file__).resolve().parent.parent
INVENTORY = BASE / "inventory" / "pops.yml"
BACKUP_DIR = BASE / "backups"

DEFAULT_REMOTE_PATH = "cf3:/config.cfg"

# Chemin distant autorisé : un fichier sur cf1:, cf2: ou cf3: uniquement
REMOTE_PATH_RE = re.compile(r"^cf[123]:/[A-Za-z0-9_./-]+$")

# Invite type : "A:SAR-8_Tour_87#" ou "*A:SAR-8_Tour_87#"
PROMPT_RE = re.compile(r"[*!]?[AB]:(?P<name>[^#>\s]+)")

# Ligne type : "# Generated MON OCT 26 13:34:34 2026 UTC"
GENERATED_RE = re.compile(r"^# Generated (?P<date>.+? UTC)\s*$", re.M)


# ---------------------------------------------------------------------------
# Inventaire
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


def resolve_name(router):
    """Même nom que topology.py et hardware.py, pour que les tags 'router' concordent."""
    fallback = router.get("name", router["ip"])
    try:
        with router_connection(host=router["ip"]) as conn:
            return local_name(conn, fallback)
    except Exception:
        return fallback


def safe_name(name):
    """Nom utilisable dans un nom de fichier ou de dossier."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    # Évite les noms dangereux pour un dossier ("", ".", "..")
    if cleaned.strip(".") == "":
        raise ValueError(f"nom de routeur inutilisable pour un dossier : {name!r}")
    return cleaned


# ---------------------------------------------------------------------------
# Téléchargement SFTP (lecture seule : on ouvre le fichier en "rb", jamais de put/remove)
# ---------------------------------------------------------------------------

def fetch_config(ip, remote_path):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    # Clé inconnue ou changée -> refus (protège contre un mauvais équipement)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        ip,
        username=get_env("ROUTER_SSH_USER"),
        password=get_env("ROUTER_SSH_PASSWORD"),
        timeout=15,
        auth_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "rb") as f:
                return f.read()
        finally:
            sftp.close()
    finally:
        client.close()


def validate(data, name):
    """Refuse un fichier vide, tronqué, ou qui ne vient pas du bon routeur."""
    text = data.decode("utf-8", errors="replace")
    if len(data) < 1000 or "# TiMOS" not in text or "configure" not in text:
        raise ValueError(f"fichier suspect ({len(data)} octets) : backup refusé")
    if not re.search(rf'^\s*name\s+"?{re.escape(name)}"?\s*$', text, re.M):
        raise ValueError(f"le fichier ne semble pas venir de {name} : backup refusé")
    return text


def config_age_hours(text):
    """Âge (en heures) de la dernière sauvegarde de config sur le routeur."""
    m = GENERATED_RE.search(text)
    if not m:
        return None
    try:
        t = datetime.strptime(m.group("date"), "%a %b %d %H:%M:%S %Y UTC")
    except ValueError:
        return None
    t = t.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)


# ---------------------------------------------------------------------------
# Fichiers locaux : un sous-dossier par routeur (backups/<routeur>/)
# ---------------------------------------------------------------------------

def router_dir(name):
    """Dossier des backups d'un routeur (ne le crée pas)."""
    return BACKUP_DIR / safe_name(name)


def ensure_router_dir(name):
    """Crée backups/ et backups/<routeur>/ en droits 700 si besoin."""
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    d = router_dir(name)
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)  # mkdir est limité par le umask : on force les droits
    return d


def backups_of(name):
    """Backups existants d'un routeur, du plus ancien au plus récent."""
    d = router_dir(name)
    if not d.is_dir():
        return []
    pat = re.compile(rf"^{re.escape(safe_name(name))}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{4}}\.cfg$")
    return sorted(p for p in d.iterdir() if pat.match(p.name))


def save_backup(name, data):
    d = ensure_router_dir(name)
    path = d / f"{safe_name(name)}_{datetime.now():%Y-%m-%d_%H%M}.cfg"
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    os.chmod(path, 0o444)  # lecture seule pour tous
    return path


def rotate(name, keep):
    """Ne garde que les 'keep' derniers backups de CE routeur (dans son dossier uniquement)."""
    if keep <= 0:
        return
    for old in backups_of(name)[:-keep]:
        old.unlink()


# ---------------------------------------------------------------------------
# Sauvegarde d'un routeur
# ---------------------------------------------------------------------------

def backup_one(router, remote_path, keep, save):
    """Retourne (name, fields, erreur). 'erreur' vaut None si tout s'est bien passé."""
    name = resolve_name(router)
    start = time.time()
    try:
        data = fetch_config(router["ip"], remote_path)
        text = validate(data, name)

        changed = 1
        previous = backups_of(name)
        if previous:
            old = hashlib.sha256(previous[-1].read_bytes()).digest()
            changed = 0 if old == hashlib.sha256(data).digest() else 1

        path = None
        if save:
            path = save_backup(name, data)
            rotate(name, keep)

        fields = {
            "success": 1,
            "size_bytes": len(data),
            "duration_s": float(round(time.time() - start, 2)),
            "changed": changed,
        }
        age = config_age_hours(text)
        if age is not None:
            fields["config_age_h"] = float(age)

        print(f"{name} ({router['ip']})")
        print(f"  OK : {len(data)} octets, config {'modifiée' if changed else 'identique'} depuis le dernier backup")
        if age is not None:
            print(f"  Dernière sauvegarde de config sur le routeur : il y a {age} h")
        if path:
            print(f"  Fichier : {path.relative_to(BACKUP_DIR)}")
        else:
            print("  Fichier : (non enregistré, --no-save)")
        return name, fields, None
    except Exception as exc:
        fields = {
            "success": 0,
            "size_bytes": 0,
            "duration_s": float(round(time.time() - start, 2)),
            "changed": 0,
        }
        print(f"[ERREUR] {name} ({router['ip']}) : {exc}")
        return name, fields, exc


def build_point(name, fields, test):
    p = Point("backup_status").tag("router", name)
    for key, value in fields.items():
        p = p.field(key, value)
    if test:
        p = p.tag("test", "true")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="écrire dans InfluxDB (sinon pas de métrique)")
    ap.add_argument("--test", action="store_true", help="ajoute le tag test=true (pour nettoyer ensuite)")
    ap.add_argument("--no-save", action="store_true", help="télécharge et vérifie, sans enregistrer de fichier")
    args = ap.parse_args()

    remote_path = os.getenv("BACKUP_REMOTE_PATH", DEFAULT_REMOTE_PATH)
    if not REMOTE_PATH_RE.match(remote_path):
        sys.exit(f"Chemin distant invalide : {remote_path!r}")
    keep = int(os.getenv("BACKUP_KEEP", "30"))

    # Avant le with : une erreur d'inventaire n'est pas enregistrée comme un script lancé
    routers = load_routers()
    points = []
    failed = []
    with tracked_run("backup.py", test=args.test, enabled=args.write) as run:
        for router in routers:
            name, fields, error = backup_one(router, remote_path, keep, save=not args.no_save)
            if error is not None:
                failed.append(name)
            # On écrit aussi les échecs : c'est ce qui déclenchera l'alerte "backup échoué"
            points.append(build_point(name, fields, args.test))

        if not args.write:
            print("\nMode affichage : rien n'a été écrit dans InfluxDB (ajoute --write pour écrire).")
        else:
            write_points(points)
            print(f"\n{len(points)} point(s) écrit(s) dans InfluxDB.")

        total = len(routers)
        run["message"] = f"{total - len(failed)}/{total} routeurs sauvegardés"
        if failed:
            run["ok"] = False
            run["message"] += " ; échec : " + ", ".join(failed)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()