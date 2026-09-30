# DualRoute

DualRoute est une interface web auto-hébergée pour administrer deux sorties Ethernet sur un NAS ZimaOS. Elle découvre uniquement `eth0`, `eth1` et `tailscale0`, ainsi que les applications Docker, associe des règles de routage aux conteneurs et à SMB, puis affiche le trafic reçu et envoyé par interface.

![Tableau de bord](docs/mockups/01-dashboard.png)

## Fonctions disponibles

- découverte automatique de toutes les interfaces, avec une séparation explicite entre les interfaces gérées (`eth0`, `eth1`, Tailscale) et celles affichées en observation seule ;
- règles par application : interface forcée, préférence avec bascule, équilibrage par connexion et marquage QoS DSCP ;
- conservation SQLite des règles, événements et mesures ;
- surveillance temps réel et historique d'`eth0` et `eth1` ;
- affichage des débits au choix en `Mb/s` ou `Mo/s`, et option pour limiter le tableau de bord aux interfaces gérées ;
- tri par clic sur les en-têtes, filtres par colonne et redimensionnement des colonnes de tous les tableaux ;
- connexions IPv4 suivies par `conntrack`, avec attribution aux IP Docker, aux ports publiés et au service SMB local ;
- synthèse du trafic par application et détail des IP/ports, protocoles, états et interfaces identifiées ;
- routage symétrique des connexions entrantes grâce aux marques de connexion ;
- prise en charge de SMB comme service système ;
- prévisualisation complète des commandes avant activation ;
- surveillance des passerelles et bascule automatique toutes les quinze secondes en mode actif.

Les limites de débit configurées sont affichées dans les règles et conservées, mais ne créent pas encore de classes `tc` locales. Les priorités QoS vont de `1` (minimale) à `5` (maximale) et sont appliquées par marquage DSCP. Le routeur ou le fournisseur d'accès doit respecter ces marques pour qu'elles influencent la file WAN.

## Topologie recommandée

Utiliser deux sous-réseaux distincts évite les ambiguïtés ARP et permet au routage par politiques de sélectionner une passerelle sans toucher à la route principale de ZimaOS.

| Liaison | Adresse du NAS | Passerelle/routeur | Sous-réseau |
|---|---:|---:|---:|
| `eth0` | `192.168.1.131/24` | `192.168.1.1` | `192.168.1.0/24` |
| `eth1` | `192.168.2.131/24` | `192.168.2.1` | `192.168.2.0/24` |

Le second routeur doit réellement être configuré avec l'adresse LAN `192.168.2.1`. Attribuer seulement une adresse `192.168.2.x` au NAS ne suffit pas. Pour le trafic entrant, configurez également les redirections de ports sur le routeur correspondant.

## Installation sur ZimaOS

Créer un dossier contenant le fichier [`compose.yml`](compose.yml), puis lancer depuis ce dossier :

```bash
docker compose pull
docker compose up -d
```

Ouvrir ensuite `http://ADRESSE_DU_NAS:9080`.

Pour mesurer le débit de chaque connexion, activez la comptabilité Linux sur le NAS depuis SSH :

```bash
sudo sysctl -w net.netfilter.nf_conntrack_acct=1
```

Les compteurs sont ajoutés aux nouvelles connexions. Les connexions déjà ouvertes peuvent rester sans compteurs jusqu’à leur renouvellement. La page Trafic affiche « — » quand une mesure est indisponible et indique ce prérequis si nécessaire. Le réglage doit être réactivé après un redémarrage du NAS ou rendu persistant via la configuration système de ZimaOS.

Dans Trafic, les volumes par application correspondent aux connexions encore présentes dans la table de suivi ; les connexions terminées entre deux mesures peuvent échapper au relevé. Les marques DualRoute identifient l’interface de routage ; pour les flux sans marque, un sous-réseau connecté donne une indication signalée comme estimée. Une interface non déterminable reste « Non identifiée ». Le trafic hôte sans IP Docker ou port publié identifiable apparaît comme « Hôte / non attribué ». L’encapsulation Tailscale peut apparaître comme trafic hôte ; les totaux des interfaces ne sont donc pas une somme exacte des flux attribués.

Le rafraîchissement conserve le tri, les filtres, la largeur des colonnes et la position dans les tableaux. Les pages Paramètres et Réseau restent stables pendant la saisie. Un bouton permet de suspendre l’actualisation de Trafic.

L'image publiée est `ghcr.io/mkdevtests/dualroute:latest`. Une image est également publiée avec le numéro de chaque tag Git, par exemple `ghcr.io/mkdevtests/dualroute:v0.1.0`.

Le fichier Compose utilise :

- `network_mode: host`, nécessaire pour voir et administrer les interfaces du NAS ;
- les capacités `NET_ADMIN` et `NET_RAW` pour `ip`, `nftables` et les tests de passerelle ;
- `/var/run/docker.sock` en lecture seule pour découvrir les applications ;
- un volume `dualroute-data` pour la base SQLite.

Au premier démarrage, DualRoute reste en mode prévisualisation. Configurez et testez `eth1`, créez les règles, ouvrez **Paramètres**, examinez le plan technique, puis activez le mode actif. L'application demande encore la saisie de `APPLIQUER` avant la première modification des règles Linux.

Par défaut, l'API refuse de reconfigurer l'interface qui porte la route principale du NAS. Cette protection évite de couper la session d'administration. Elle peut être levée volontairement avec `DUALROUTE_ALLOW_PRIMARY_RECONFIGURE=1`, mais ce réglage ne doit pas être utilisé pendant l'installation initiale.

## Modèle de routage

DualRoute crée uniquement une table `nftables` nommée `inet dualroute` et les tables de routage Linux `101` et `102`. Les nouvelles connexions d'un conteneur sont marquées selon sa règle. La marque est conservée dans `conntrack`, ce qui maintient les réponses entrantes sur l'interface d'origine. L'équilibrage sélectionne une sortie par connexion afin qu'une même session TCP ne change pas de lien.

Les destinations privées RFC1918 ne sont pas forcées par les règles des conteneurs. Elles continuent d'utiliser la table principale du NAS. Cette protection empêche une règle Internet d'envoyer le trafic LAN vers une mauvaise passerelle.

## Développement et vérification

```bash
docker build -t dualroute:local .
docker run --rm -v "$PWD:/work" -w /work dualroute:local python -m unittest discover -s tests -v
```

Le Compose de développement construit l'image depuis les sources :

```bash
docker compose -f compose.dev.yml up -d --build
```

Pour lancer l'interface sans modifier le réseau, conservez le mode actif désactivé. Sur Windows ou macOS, la découverte reflète la machine virtuelle Docker ; la validation réelle doit se faire sur le NAS Linux.

## Limites de la première version

- IPv4 uniquement pour l'application des politiques ;
- les limites de débit sont enregistrées mais pas encore façonnées localement avec `tc` ;
- un conteneur utilisant directement `network_mode: host` ne possède pas d'adresse Docker distincte. Sa règle doit alors être exprimée par ports, comme SMB ;
- les modifications d'adresse effectuées avec `ip address replace` peuvent être réécrites par ZimaOS après redémarrage. DualRoute conserve la configuration, mais la persistance native de ZimaOS devra être validée sur le modèle de NAS cible.

## Maquettes validées

- [Tableau de bord](docs/mockups/01-dashboard.png)
- [Règles](docs/mockups/02-regles.png)
- [Configuration réseau](docs/mockups/03-reseau-v2.png)
- [Analyse du trafic](docs/mockups/04-trafic.png)
