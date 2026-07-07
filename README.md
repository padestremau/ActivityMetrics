# 📊 ActivityMetrics

Suivi du temps par projet, **100 % en local**, sur macOS. Un petit daemon
échantillonne discrètement l'application active et — pour Chrome — le domaine et
l'onglet au premier plan, puis produit des rapports **par projet › application ›
onglet**, à l'écran, en HTML, ou poussés sur **Telegram** (bilan quotidien /
hebdo / mensuel automatique).

Aucune donnée ne quitte ta machine, sauf le résumé chiffré que **tu** choisis
d'envoyer sur **ton** bot Telegram.

## Ce qu'il sait faire

- **Par application** : Slack, navigateur, éditeur de code, mail natif, etc.
- **Par onglet / outil** dans Chrome : Gmail, Agenda, un CRM, un outil métier…
- **Plusieurs sessions Chrome** : si tu utilises plusieurs profils Chrome (perso,
  pro, un compte par contexte), ActivityMetrics distingue les contextes grâce au
  compte connecté et au domaine — utile quand un même site (mail, CRM…) sert
  plusieurs casquettes.
- **Détection d'inactivité** : le temps sans clavier/souris n'est pas compté.
- **Classification rétroactive** : tu définis des règles (`clients.json`) qui
  rattachent l'activité à des projets ; comme le classement se fait au moment du
  rapport, **modifier une règle reclasse tout l'historique déjà capté**.

## Installation (macOS)

Prérequis : macOS, Python 3, et Google Chrome pour le détail des onglets.

```bash
git clone https://github.com/padestremau/ActivityMetrics.git
cd ActivityMetrics
python3 activitymetrics.py setup      # détecte tes profils Chrome + prépare la config
```

`setup` génère un `clients.json` de départ et t'indique les 3 étapes manuelles :

1. **Permission Accessibilité** — Réglages Système › Confidentialité et sécurité ›
   Accessibilité › ajouter le binaire Python affiché par `setup`. C'est ce qui
   autorise la lecture du titre des fenêtres (comme tout tracker de ce type).
2. **Lancer la capture** : `python3 activitymetrics.py install`
3. *(optionnel)* **Envois Telegram** : voir plus bas.

## Utilisation

```bash
python3 activitymetrics.py status              # coup d'œil sur la journée
python3 activitymetrics.py report --today
python3 activitymetrics.py report --week
python3 activitymetrics.py report --month
python3 activitymetrics.py report --day 2026-01-15
python3 activitymetrics.py report --week --html      # + rapport HTML
python3 activitymetrics.py report --today --telegram # + envoi Telegram
```

## Configuration — `clients.json`

Copie `clients.example.json` en `clients.json` (ou lance `setup`) puis adapte les
règles. Une règle rattache une activité à un projet ; elle matche si **toutes**
ses conditions sont vraies, et la `priority` la plus haute l'emporte.

| Clé de `match`   | Effet                                                  |
|------------------|--------------------------------------------------------|
| `title_contains` | Sous-chaîne du titre de la fenêtre (email, workspace…) |
| `domain`         | Domaine exact ou sous-domaine de l'onglet Chrome       |
| `url_contains`   | Sous-chaîne de l'URL (ex. `/nom-du-client/`)           |
| `app`            | Sous-chaîne du nom de l'application                    |

> Note technique : le **nom** d'un profil Chrome n'est pas exposé par macOS. On
> identifie donc le contexte via le **compte** (email présent dans le titre), le
> **domaine** et l'**URL** — tous fiablement captés.

## Envois Telegram automatiques

1. Crée un bot avec [@BotFather](https://t.me/BotFather) → récupère le **token**.
2. Colle le token dans `telegram.json`, écris « hello » à ton bot, puis :
   ```bash
   python3 activitymetrics.py setup-telegram   # récupère ton chat_id
   python3 activitymetrics.py report --today --telegram   # test
   python3 activitymetrics.py schedule          # quotidien 18h, hebdo, mensuel
   ```

Couper les envois : `python3 activitymetrics.py schedule --off`.

## Confidentialité

Tout est local. `data.db` (les mesures), `clients.json` (tes projets) et
`telegram.json` (ton bot) sont **gitignored** : rien de personnel n'est versionné.

## Désinstallation

```bash
python3 activitymetrics.py uninstall       # arrête la capture
python3 activitymetrics.py schedule --off  # arrête les envois
```

## Sous le capot

- Capture : `lsappinfo` (app active, sans permission) + API d'accessibilité
  (titre de fenêtre) + AppleScript Chrome (URL) + `ioreg` (inactivité).
- Stockage : SQLite, données brutes uniquement.
- Automatisation : LaunchAgents macOS (`launchd`).
- Zéro dépendance : Python 3 standard.
