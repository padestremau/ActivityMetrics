#!/usr/bin/env bash
# Bootstrap ActivityMetrics sur une nouvelle machine macOS.
# Usage : ./install.sh
set -e
cd "$(dirname "$0")"

echo "📊 ActivityMetrics — installation"
command -v python3 >/dev/null || { echo "❌ python3 requis (Xcode CLT : xcode-select --install)"; exit 1; }

# 1. Config de classification
if [ -f clients.json ]; then
  echo "✓ clients.json présent"
else
  echo "→ pas de clients.json : génération guidée (setup)"
  python3 activitymetrics.py setup
fi

# 2. Capture en tâche de fond (launchd)
echo "→ installation du daemon de capture"
python3 activitymetrics.py install

# 3. Envois Telegram (si configuré)
if [ -f telegram.json ]; then
  echo "→ programmation des envois Telegram"
  python3 activitymetrics.py schedule
else
  echo "⚠️  telegram.json absent : lance ensuite"
  echo "     python3 activitymetrics.py setup-telegram && python3 activitymetrics.py schedule"
fi

cat <<'EOF'

────────────────────────────────────────────────────────────
⚠️  ÉTAPES MANUELLES (permissions macOS — sinon titres/URL vides)
   Réglages Système › Confidentialité et sécurité :
   • Accessibilité : ajouter le binaire python3 affiché ci-dessus
   • Automatisation : autoriser « Google Chrome » pour System Events
────────────────────────────────────────────────────────────
Publication VPS + rapprochement Timesheet + modale mot de passe :
   voir REINSTALL.md (config perso : publish/timesheet, clé SSH).
EOF
