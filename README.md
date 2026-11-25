# NOC Monitoring

Bienvenue ! Ce projet sert à garder un œil sur un routeur Nokia 7705 SAR (le **SAR-8_Tour_87**) :
est-ce que ses ports sont up, combien de trafic passe, avec qui il est connecté. Le tout s'affiche
dans Grafana, et une alerte nous prévient quand quelque chose cloche.

Derrière, il y a une petite stack Docker (InfluxDB, Telegraf, Grafana) et quelques scripts Python
qui ne font que **lire** le routeur, jamais le modifier.

> Un mot sur les secrets : mots de passe, token et community SNMP vivent uniquement dans le
> fichier `.env`, qui n'est jamais versionné. On ne les écrit ni dans le code, ni dans ce README,
> ni dans un message.

## Comment ça marche

```
Routeur 172.17.0.87 ──SNMP──▶ Telegraf ──▶ InfluxDB ──▶ Grafana
        │                                      ▲
        └──SSH (commandes "show")──▶ scripts Python
```

- **Telegraf** interroge le routeur en SNMP toutes les minutes (débits, états des ports).
- **InfluxDB** range tout ça dans le bucket `network`.
- **Grafana** en fait des graphiques et surveille les valeurs.
- Les **scripts Python** se connectent en SSH pour des infos que le SNMP ne donne pas, comme les
  voisins IS-IS qui servent à dessiner la topologie.

| Service | Version | Port (uniquement en local, sur 127.0.0.1) |
|---|---|---|
| InfluxDB | 2.7 | 18086 |
| Telegraf | 1.32 | 18186 |
| Grafana | 11.2.0 | 13000 |

Quelques choix à connaître, parce qu'ils nous ont déjà fait perdre du temps :

- Le réseau Docker `noc-thz_default` est fixé en `192.168.130.0/24`. Le routeur est en
  172.17.0.x, et sans ce réglage Docker prenait la même plage et plus rien ne passait.
  **Quand on recopie le `docker-compose.yml`, on garde le bloc `networks`.**
- Les trois conteneurs ont `restart: unless-stopped` : si le serveur redémarre, ils remontent seuls.
- Le nom d'hôte de Telegraf est fixé à `noc-thz`. Avant, il changeait à chaque recréation du
  conteneur et les courbes Grafana se dédoublaient.
- Cette stack est indépendante de celle de l'entreprise : on ne touche pas à ses conteneurs.

## Où est quoi

```
noc-monitoring/
├── docker-compose.yml
├── .env                    # les secrets (jamais versionné)
├── .gitignore
├── requirement.txt         # les dépendances Python
├── telegraf/telegraf.conf  # ce qu'on collecte en SNMP
├── grafana/
│   ├── dashboards/noc-daily.json
│   └── provisioning/{dashboards,datasources}/
├── inventory/pops.yml      # la liste des routeurs à superviser
├── scripts/
│   ├── common.py           # la boîte à outils partagée
│   ├── topology.py         # lit les adjacences IS-IS
│   ├── scan_pop.py         # à venir
│   └── backup.py           # à venir
├── backups/
└── logs/
```

## Le fichier `.env`

Il faut le créer sur le serveur, avec ces variables (les valeurs, elles, restent secrètes) :

| Variable | À quoi elle sert |
|---|---|
| `INFLUX_USER`, `INFLUX_PASSWORD` | Compte administrateur d'InfluxDB |
| `INFLUX_ORG`, `INFLUX_BUCKET` | L'organisation et le bucket (`network`) |
| `INFLUX_TOKEN` | Token en lecture/écriture, limité au bucket `network` |
| `ROUTER_IP` | L'adresse du routeur |
| `SNMP_COMMUNITY` | La community SNMP v2c |
| `ROUTER_SSH_USER`, `ROUTER_SSH_PASSWORD` | Le compte SSH en lecture seule |
| `INFLUX_URL` (facultatif) | `http://127.0.0.1:18086` par défaut pour les scripts |

Un petit `chmod 600 .env` pour que personne d'autre ne puisse le lire.

## Démarrer la stack

```bash
cd /home/thz/noc-monitoring
docker compose config -q      # vérifie que le fichier est correct (rien d'affiché = tout va bien)
docker compose up -d          # démarre ou met à jour
docker compose ps             # les 3 services doivent être "Up"
```

Pour appliquer un changement dans la config de Telegraf, sans toucher au reste :

```bash
docker compose up -d --force-recreate telegraf
docker compose logs --tail 15 telegraf
```

Ce qu'on évite absolument : `docker compose down -v` (il efface les volumes, donc les données et
le dashboard), `docker system prune`, et toute commande qui s'applique à tous les conteneurs du
serveur, car on partage la machine.

## Les scripts Python

Sur le serveur, on travaille dans un environnement virtuel. Il a été créé sans `ensurepip`, parce
que le paquet `python3-venv` n'est pas installé et qu'on n'a pas à modifier le système.

```bash
cd /home/thz/noc-monitoring
source venv/bin/activate
pip install -r requirement.txt
```

| Script | Ce qu'il fait | Pour le lancer |
|---|---|---|
| `common.py` | Regroupe le code commun. Lancé seul, il fait un test : ping d'InfluxDB et un `show` sur le routeur | `python scripts/common.py` |
| `topology.py` | Lit `show router isis adjacency` et enregistre les liens dans InfluxDB | `python scripts/topology.py` |

`topology.py` est prudent par défaut :

- sans option, il affiche ce qu'il trouve et **n'écrit rien** ;
- avec `--write`, il enregistre dans InfluxDB ;
- avec `--test`, il marque les données `test=true`, pour pouvoir les retrouver et les effacer à la fin.

**Lecture seule, toujours.** La fonction `run_show()` de `common.py` refuse toute commande qui ne
commence pas par `show`. Pas de `configure`, pas de `admin`, pas de `clear`, et on ne contourne
pas ce garde-fou.

## Le dashboard et l'alerte

Le dashboard **NOC Daily** se rafraîchit toutes les minutes et montre :

- la capacité et l'état du port 1/6/8 ;
- le débit entrant et sortant ;
- l'état de tous les liens (la couleur est sur la colonne `etat`) ;
- la topologie IS-IS, sous forme de Node Graph.

Une alerte, **Port active mais down**, surveille les ports que quelqu'un a activés mais qui n'ont
pas de lien (`ifAdminStatus = 1` et `ifOperStatus = 2`). Elle attend 5 minutes avant de se
déclencher, pour ne pas s'affoler sur une micro-coupure. Pour le moment elle n'est visible que
dans Grafana : aucune notification externe n'est configurée.

Au passage, `A/1` et `B/1` la déclenchent en permanence. On attend de savoir s'ils sont
volontairement inutilisés avant de les exclure.

## Ce qu'on stocke dans InfluxDB

- `interface` (via SNMP) : débits, capacité, états, alias et erreurs de chaque port.
- `topology_link` (via `topology.py`) : qui est connecté à qui (`router`, `neighbor`, `interface`,
  `level`), avec `up` et `hold`.

Dans les requêtes Flux des panneaux, on retire le tag `host`
(`drop(columns: ["host"])`) pour que les anciennes données et les nouvelles ne forment qu'une courbe.

## Où on en est

Déjà fait : la stack Docker, la collecte SNMP, la sécurité (token limité, mots de passe changés),
`common.py`, `topology.py`, le dashboard, le Node Graph et l'alerte « port activé mais down ».

Reste à faire :

- `scan_pop.py`, qui attend la liste des autres routeurs ;
- `backup.py`, qui attend le droit d'afficher la configuration ;
- une alerte d'usage au-dessus de 80 % et une alerte de sauvegarde échouée ;
- des panneaux supplémentaires (sauvegardes, topologie) ;
- l'exécution régulière de `topology.py`, dont la fréquence reste à valider ;
- le nettoyage des données `test=true`, tout à la fin.
