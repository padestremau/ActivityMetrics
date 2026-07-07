# 🚀 Installer ActivityMetrics (5 minutes)

Guide pas-à-pas pour suivre ton temps par projet, en local, sur ton Mac.
Aucune donnée ne sort de ta machine.

---

## 1. Récupérer le projet

```bash
git clone https://github.com/padestremau/ActivityMetrics.git
cd ActivityMetrics
```

*(Pas de Git ? Télécharge le ZIP depuis GitHub, dézippe, et ouvre le dossier
dans le Terminal.)*

## 2. Lancer la configuration

```bash
python3 activitymetrics.py setup
```

Ça détecte tes profils Chrome, crée un `clients.json` de départ, et affiche le
**chemin du binaire Python** à autoriser à l'étape suivante. Note-le.

## 3. Autoriser l'Accessibilité (une fois)

Réglages Système › **Confidentialité et sécurité** › **Accessibilité** › bouton
`+` › `Cmd+Shift+G` › colle le chemin Python affiché par `setup` › active
l'interrupteur.

> Sans ça, l'app active est captée mais pas le détail des onglets/titres. C'est
> la même permission que demandent tous les trackers de temps.

## 4. Démarrer la capture

```bash
python3 activitymetrics.py install
```

C'est parti : le daemon échantillonne toutes les 20 s en arrière-plan (et se
relance automatiquement au redémarrage). Le temps d'inactivité n'est pas compté.

## 5. Voir ton premier bilan

Bosse normalement quelques heures, puis :

```bash
python3 activitymetrics.py report --today
```

## 6. Adapter à TES projets

Ouvre `clients.json` et remplace les règles d'exemple par les tiennes. Le plus
simple et le plus fiable : une règle par **compte email** (il apparaît dans le
titre des fenêtres Gmail), plus quelques **domaines** d'outils.

```json
{ "project": "Mon Job",   "priority": 50, "match": { "title_contains": "moi@monjob.com" } },
{ "project": "Perso",     "priority": 50, "match": { "title_contains": "moi.perso@gmail.com" } },
{ "project": "Client X",  "priority": 45, "match": { "domain": "clientx.com" } }
```

Pas besoin de tout capturer à nouveau : la classification se fait à la lecture,
donc **modifier une règle recalcule tout l'historique**. Regarde la section
« Par onglet / outil » du rapport pour repérer les domaines à classer.

## 7. (Optionnel) Recevoir tes bilans sur Telegram

1. Sur Telegram, ouvre [@BotFather](https://t.me/BotFather), envoie `/newbot`,
   suis les étapes → tu obtiens un **token**.
2. Colle le token dans `telegram.json` (clé `"token"`).
3. Envoie « hello » à ton nouveau bot.
4. Puis :
   ```bash
   python3 activitymetrics.py setup-telegram   # récupère ton chat_id
   python3 activitymetrics.py report --today --telegram   # test d'envoi
   python3 activitymetrics.py schedule          # bilans auto : 18h / vendredi / fin de mois
   ```

---

## Aide-mémoire

| Commande | Effet |
|----------|-------|
| `report --today` / `--week` / `--month` | Bilan à l'écran |
| `report --week --html` | Rapport HTML dans `reports/` |
| `report --today --telegram` | Envoi Telegram immédiat |
| `status` | Totaux du jour, vite fait |
| `schedule` / `schedule --off` | Activer / couper les envois auto |
| `uninstall` | Arrêter la capture |

## Tout arrêter / désinstaller

```bash
python3 activitymetrics.py uninstall
python3 activitymetrics.py schedule --off
```

Des questions ? Reprends ce guide dans l'ordre — 99 % des soucis viennent de
l'étape 3 (Accessibilité pas accordée au bon binaire Python).
