#!/usr/bin/env python3
"""
ActivityMetrics — suivi du temps par projet, en local, sur macOS.

═══════════════════════════════════════════════════════════════════════════════
COMMENT ÇA MARCHE (architecture)
═══════════════════════════════════════════════════════════════════════════════

1. CAPTURE — Un daemon (LaunchAgent macOS) exécute `probe` toutes les 20 s :
     • app active           -> via `lsappinfo` (AUCUNE permission requise)
     • titre de la fenêtre   -> via l'API d'accessibilité (System Events)
     • Chrome : URL + domaine -> via AppleScript Chrome
     • inactivité            -> via ioreg (temps depuis dernière touche/souris)
   Chaque échantillon (app, titre, domaine, url, idle) est écrit BRUT dans
   SQLite (data.db). On ne stocke jamais d'interprétation : que du factuel.

2. CLASSIFICATION — Elle a lieu AU MOMENT DU RAPPORT, pas à la capture. Les
   règles de clients.json rattachent chaque échantillon à un projet. Conséquence
   clé : éditer les règles RECLASSE rétroactivement tout l'historique déjà capté.

   ⚠️  Le NOM du profil Chrome n'est PAS lisible (absent de l'arbre AX). On
   identifie donc le contexte par le COMPTE (email présent dans le titre Gmail),
   le DOMAINE et l'URL — tous fiablement captés. Voir clients.example.json.

3. RESTITUTION — Rapports jour / semaine / mois, en arbre hiérarchique
   (projet > application > onglets Chrome), à l'écran, en HTML, ou poussés sur
   Telegram (manuellement ou via les envois programmés).

Confidentialité : 100 % local. Rien ne quitte la machine, sauf le résumé chiffré
que TU choisis d'envoyer sur ton propre bot Telegram. data.db / telegram.json /
clients.json sont gitignored.

═══════════════════════════════════════════════════════════════════════════════
COMMANDES
═══════════════════════════════════════════════════════════════════════════════
    python3 activitymetrics.py setup       # config auto pour un nouvel utilisateur
    python3 activitymetrics.py install     # installe le daemon d'échantillonnage
    python3 activitymetrics.py schedule     # programme les envois Telegram auto
    python3 activitymetrics.py status       # totaux du jour, vite fait
    python3 activitymetrics.py probe --verbose   # 1 échantillon de test
    python3 activitymetrics.py report --today [--week|--month|--day AAAA-MM-JJ]
    python3 activitymetrics.py report --week --html      # + rapport HTML
    python3 activitymetrics.py report --today --telegram  # + envoi Telegram
    python3 activitymetrics.py uninstall / schedule --off  # tout arrêter

Nouvel utilisateur ? -> voir ONBOARDING.md (ou lance `setup`).
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, date
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "clients.json")
DB_PATH = os.path.join(BASE, "data.db")
REPORTS_DIR = os.path.join(BASE, "reports")
CHROME_LOCAL_STATE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Local State"
)
LAUNCH_AGENT_LABEL = "com.activitymetrics"
LAUNCH_AGENT_PATH = os.path.expanduser(
    f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist"
)


# --------------------------------------------------------------------------- #
# Config & DB
# --------------------------------------------------------------------------- #
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS samples (
            ts       INTEGER NOT NULL,
            app      TEXT,
            profile  TEXT,
            domain   TEXT,
            url      TEXT,
            title    TEXT,
            idle     INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON samples(ts)")
    return conn


# --------------------------------------------------------------------------- #
# Sondes macOS
# --------------------------------------------------------------------------- #
def osa(script, timeout=8):
    """Exécute un AppleScript, renvoie stdout nettoyé ou None."""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip()
    except Exception:
        return None


def get_idle_seconds():
    """Secondes depuis la dernière activité clavier/souris (via ioreg)."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if m:
            return int(m.group(1)) / 1_000_000_000.0
    except Exception:
        pass
    return 0.0


def frontmost_app():
    """App active via lsappinfo — SANS permission TCC (marche sous launchd)."""
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "name", asn],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r'"LSDisplayName"\s*=\s*"([^"]*)"', out)
        return m.group(1) if m else None
    except Exception:
        return None


def frontmost_window_title(app):
    """Titre de la fenêtre active de n'importe quelle app (via AX).
    Contient souvent le sous-contexte : workspace Slack, projet VS Code,
    page Notion, onglet Chrome… Nécessite la permission Accessibilité."""
    if not app:
        return None
    esc = app.replace('"', '\\"')
    out = osa(
        f'tell application "System Events" to tell process "{esc}"\n'
        '  try\n    return value of attribute "AXTitle" of front window\n  end try\n'
        '  try\n    return value of attribute "AXTitle" of window 1\n  end try\n'
        '  return ""\n'
        "end tell"
    )
    return (out or None) if out else None


def chrome_active_tab():
    """(url, title) de l'onglet actif de la fenêtre Chrome au premier plan."""
    out = osa(
        'tell application "Google Chrome"\n'
        "  if (count of windows) = 0 then return \"\"\n"
        "  set u to URL of active tab of front window\n"
        "  set t to title of active tab of front window\n"
        '  return u & "\\n" & t\n'
        "end tell"
    )
    if not out:
        return None, None
    parts = out.split("\n", 1)
    url = parts[0].strip() if parts else None
    title = parts[1].strip() if len(parts) > 1 else None
    return (url or None), (title or None)


def chrome_focused_title():
    """Titre de la fenêtre Chrome RÉELLEMENT active, via AXFocusedWindow.
    Plus fiable que 'front window' (System Events) qui pointe parfois une
    fenêtre auxiliaire sans titre : barre de partage Meet, sélecteur de
    fichier… Nécessite la permission Accessibilité."""
    out = osa(
        'tell application "System Events" to tell process "Google Chrome"\n'
        '  try\n'
        '    return value of attribute "AXTitle" of '
        '(value of attribute "AXFocusedWindow")\n'
        '  end try\n'
        '  return ""\n'
        "end tell"
    )
    return (out or None) if out else None


# Chrome suffixe ses titres par « … - Google Chrome – <prénom> (<profil>) »
# dès que plusieurs profils existent. Le nom entre parenthèses (= nom du
# profil dans Local State) identifie le compte de façon fiable, alors que
# le dictionnaire AppleScript de Chrome ne renvoie plus qu'une fenêtre
# fantôme « about:blank » depuis Chrome 15x.
_CHROME_PROFILE_RE = re.compile(r"Google Chrome\s*[–—-]\s*.*\(([^)]+)\)\s*$")
_CHROME_SUFFIX_RE = re.compile(r"\s*[-–—]\s*Google Chrome\b.*$")


def chrome_profile_from_title(title):
    """Nom du profil Chrome extrait du titre de fenêtre (compte)."""
    if not title:
        return None
    m = _CHROME_PROFILE_RE.search(title)
    return m.group(1).strip() if m else None


def clean_chrome_title(title):
    """Titre de page seul, sans le suffixe « - Google Chrome – <profil> »
    (sert de libellé d'onglet à défaut d'URL)."""
    if not title:
        return None
    t = _CHROME_SUFFIX_RE.sub("", title).strip()
    return t or None


def known_profile_names():
    """Noms des profils Chrome déclarés dans Local State."""
    try:
        with open(CHROME_LOCAL_STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        info = data.get("profile", {}).get("info_cache", {})
        return [meta.get("name") for meta in info.values() if meta.get("name")]
    except Exception:
        return []


def chrome_profile(known):
    """
    Nom du profil de la fenêtre Chrome au premier plan.
    Lit les descriptions des boutons de la barre d'outils (nécessite la
    permission Accessibilité) et repère lequel des profils connus y apparaît.
    """
    if not known:
        return None
    blob = osa(
        'tell application "System Events"\n'
        '  tell process "Google Chrome"\n'
        '    set txt to ""\n'
        "    try\n"
        "      set w to front window\n"
        "      repeat with b in (buttons of w)\n"
        '        try\n          set txt to txt & (description of b) & "\\n"\n        end try\n'
        "      end repeat\n"
        "      repeat with g in (groups of w)\n"
        "        try\n          repeat with b in (buttons of g)\n"
        '            try\n              set txt to txt & (description of b) & "\\n"\n            end try\n'
        "          end repeat\n        end try\n"
        "      end repeat\n"
        "    end try\n"
        "    return txt\n"
        "  end tell\n"
        "end tell"
    )
    if not blob:
        return None
    # match le plus long d'abord (évite qu'un nom court masque un plus précis)
    for name in sorted(known, key=len, reverse=True):
        if name and name in blob:
            return name
    return None


def chrome_url_via_ax():
    """URL/domaine depuis l'omnibox (barre d'adresse) via l'arbre AX.
    Ne pilote pas Chrome par Apple events — plus robuste sous launchd que
    l'AppleScript Chrome, une fois l'Accessibilité accordée."""
    blob = osa(
        'tell application "System Events"\n'
        '  tell process "Google Chrome"\n'
        '    set txt to ""\n'
        "    try\n"
        "      set w to front window\n"
        "      repeat with tf in (text fields of w)\n"
        '        try\n          set txt to txt & (value of tf) & "\\n"\n        end try\n'
        "      end repeat\n"
        "      repeat with g in (groups of w)\n"
        "        try\n          repeat with tf in (text fields of g)\n"
        '            try\n              set txt to txt & (value of tf) & "\\n"\n            end try\n'
        "          end repeat\n        end try\n"
        "      end repeat\n"
        "    end try\n"
        "    return txt\n"
        "  end tell\n"
        "end tell"
    )
    if not blob:
        return None
    for line in blob.splitlines():
        s = line.strip()
        # une valeur d'omnibox ressemble à un domaine / une URL
        if s and ("." in s) and (" " not in s or s.startswith("http")):
            return s if s.startswith("http") else "https://" + s
    return None


def domain_of(url):
    if not url:
        return None
    try:
        net = urlparse(url).netloc.lower()
        return net[4:] if net.startswith("www.") else (net or None)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #
def cmd_probe(args):
    cfg = load_config()
    idle_threshold = cfg.get("idle_threshold_seconds", 120)
    idle = get_idle_seconds()
    is_idle = 1 if idle >= idle_threshold else 0

    app = frontmost_app()
    profile = url = title = domain = None

    if not is_idle and app:
        # titre de fenêtre générique (workspace Slack, projet VS Code, page…)
        title = frontmost_window_title(app)
        if "chrome" in app.lower():
            # Titre fiable via AXFocusedWindow (le 'front window' peut pointer
            # une fenêtre auxiliaire sans titre : partage Meet, sélecteur…).
            ax_title = chrome_focused_title()
            if ax_title:
                title = ax_title
            # URL : depuis Chrome 15x le dictionnaire AppleScript ne renvoie
            # plus qu'une fenêtre fantôme « about:blank ». On la traite comme
            # absente et on retombe sur l'omnibox AX.
            u, t = chrome_active_tab()
            if (u or "").lower() in ("", "about:blank"):
                u = chrome_url_via_ax()
            url = u
            if t and t.lower() not in ("", "about:blank"):
                title = t
            domain = domain_of(url)
            # Profil (= compte) lu dans le titre — le dictionnaire AppleScript
            # étant mort, c'est le signal de classification principal.
            profile = (chrome_profile_from_title(title)
                       or chrome_profile(known_profile_names()))
            # Libellé propre pour la section « Par onglet / outil » à défaut d'URL.
            title = clean_chrome_title(title) or title

    conn = db()
    conn.execute(
        "INSERT INTO samples (ts, app, profile, domain, url, title, idle) "
        "VALUES (?,?,?,?,?,?,?)",
        (int(time.time()), app, profile, domain, url, title, is_idle),
    )
    conn.commit()
    conn.close()

    if args.verbose:
        state = "IDLE" if is_idle else "ACTIF"
        print(f"[{state}] app={app} profil={profile} domaine={domain}")
        if url:
            print(f"        url={url}")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _match_domain(sample_domain, rule_domain):
    if not sample_domain:
        return False
    sd, rd = sample_domain.lower(), rule_domain.lower()
    return sd == rd or sd.endswith("." + rd)


def label_domain(domain, cfg):
    """Nom lisible d'un domaine via cfg['domain_labels'] (ex: mail.google.com -> Gmail)."""
    if not domain:
        return None
    labels = cfg.get("domain_labels", {})
    if domain in labels:
        return labels[domain]
    for pat, name in labels.items():
        if domain == pat or domain.endswith("." + pat):
            return name
    return domain


def classify(sample, cfg):
    """sample = dict(app, profile, domain, url, title). -> nom de projet."""
    best_project = cfg.get("default_project", "Non classé")
    best_prio = -1
    for rule in cfg.get("rules", []):
        match = rule.get("match", {})
        prio = rule.get("priority", 0)
        ok = True
        for key, val in match.items():
            if key == "app":
                ok = bool(sample.get("app")) and val.lower() in sample["app"].lower()
            elif key == "profile":
                ok = sample.get("profile") == val
            elif key == "domain":
                ok = _match_domain(sample.get("domain"), val)
            elif key == "title_contains":
                ok = bool(sample.get("title")) and val.lower() in sample["title"].lower()
            elif key == "url_contains":
                ok = bool(sample.get("url")) and val.lower() in sample["url"].lower()
            else:
                ok = False
            if not ok:
                break
        if ok and prio > best_prio:
            best_prio = prio
            best_project = rule["project"]
    return best_project


# --------------------------------------------------------------------------- #
# Rapports
# --------------------------------------------------------------------------- #
_FR_MOIS_ABBR = ["janv", "févr", "mars", "avr", "mai", "juin",
                 "juil", "août", "sept", "oct", "nov", "déc"]
_FR_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre"]


def _fr_date(d):
    jour = "1er" if d.day == 1 else str(d.day)
    return f"{jour} {_FR_MOIS_ABBR[d.month - 1]} {d.year}"


def _period_range(args):
    today = date.today()
    if args.day:
        d = datetime.strptime(args.day, "%Y-%m-%d").date()
        return d, d + timedelta(days=1), _fr_date(d)
    if args.week:
        start = today - timedelta(days=today.weekday())  # lundi
        return start, today + timedelta(days=1), f"Semaine du {_fr_date(start)}"
    if args.month:
        start = today.replace(day=1)
        return start, today + timedelta(days=1), f"{_FR_MOIS[start.month - 1].capitalize()} {start.year}"
    # défaut: aujourd'hui
    return today, today + timedelta(days=1), _fr_date(today)


def _period_slug(args, start):
    """Slug d'URL du rapport (segment après /activityMetrics/)."""
    if getattr(args, "week", False):
        return f"semaine-{start.isoformat()}"
    if getattr(args, "month", False):
        return f"{start.year}-{start.month:02d}"
    # jour précis ou aujourd'hui → date ISO
    return start.isoformat()


def _fetch(start, end):
    conn = db()
    cur = conn.execute(
        "SELECT app, profile, domain, url, title FROM samples "
        "WHERE idle = 0 AND ts >= ? AND ts < ?",
        (int(time.mktime(start.timetuple())), int(time.mktime(end.timetuple()))),
    )
    rows = [
        {"app": a, "profile": p, "domain": d, "url": u, "title": t}
        for (a, p, d, u, t) in cur.fetchall()
    ]
    conn.close()
    return rows


def _fmt_h(seconds):
    # Temps négligeable (< 1 min) → « - » plutôt que « 0h00 » (évite la
    # confusion et allège l'affichage).
    if seconds < 60:
        return "-"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m:02d}"


def _fmt_row(label, secs, pct, width, sub=False):
    """Ligne à colonnes fixes: label pad, temps aligné, puis %.
    Le % d'un groupe (projet) est légèrement à gauche ; celui d'un
    sous-élément (app/onglet) est décalé à droite, tous alignés ensemble —
    ce qui matérialise la hiérarchie en monospace. Le % est masqué quand le
    temps est négligeable (« - »)."""
    gap = "    " if sub else " "
    t = _fmt_h(secs)
    pstr = "    " if t == "-" else f"{round(pct):>3}%"
    return f"{label[:width]:<{width}} {t:>5}{gap}{pstr}"


def _aggregate(rows, cfg, keyfn):
    sample_s = cfg.get("sample_seconds", 20)
    totals = {}
    for r in rows:
        k = keyfn(r) or "—"
        totals[k] = totals.get(k, 0) + sample_s
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


TELEGRAM_PATH = os.path.join(BASE, "telegram.json")


def _telegram_conf():
    try:
        with open(TELEGRAM_PATH, "r", encoding="utf-8") as f:
            c = json.load(f)
        if c.get("token") and c.get("chat_id"):
            return c
    except Exception:
        pass
    return None


def send_telegram(text, button_url=None, button_text="📄 Voir le détail"):
    """Envoie un message Markdown au bot Telegram configuré.
    Si button_url est fourni, ajoute un bouton inline (lien) sous le message."""
    conf = _telegram_conf()
    if not conf:
        print("Telegram non configuré (token/chat_id manquants dans telegram.json).")
        return False
    import urllib.request
    import urllib.parse
    params = {
        "chat_id": conf["chat_id"],
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }
    if button_url:
        params["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": button_text, "url": button_url}]]}
        )
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{conf['token']}/sendMessage"
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as r:
            ok = json.loads(r.read()).get("ok", False)
        print("Envoyé sur Telegram." if ok else "Échec envoi Telegram.")
        return ok
    except Exception as e:
        print("Erreur Telegram:", e)
        return False


def _telegram_text(label, total_s, tree, recon=None):
    # En-tête en gras (hors bloc), puis l'arbre en MONOSPACE (```) pour que
    # les colonnes temps/% restent alignées à droite sur mobile.
    W = 16
    head = f"📊 *ActivityMetrics* — {label}\n⏱ *{_fmt_h(total_s)}* de temps actif"
    if recon:
        gap = recon["gap"]
        head += (f"\n🗓 Timesheet *{recon['ts']['total']['realDays']:.1f} j* "
                 f"facturés · travaillés *{recon['am']['fde']:.1f} j* "
                 f"(écart {'+' if gap >= 0 else ''}{gap:.1f} j)")
    body = []
    for proj, node in _sorted_projects(tree):
        ppct = (100 * node["total"] / total_s) if total_s else 0
        body.append(_fmt_row(f"▸ {proj}", node["total"], ppct, W))
        for app, a in _sorted_apps(node["apps"]):
            apct = (100 * a["total"] / node["total"]) if node["total"] else 0
            body.append(_fmt_row(f"  {app}", a["total"], apct, W))
            for tab, s in sorted(a["tabs"].items(), key=lambda kv: kv[1],
                                 reverse=True):
                if tab == "—" or s < 60:
                    continue
                tpct = (100 * s / a["total"]) if a["total"] else 0
                body.append(_fmt_row(f"   - {tab}", s, tpct, W))
        body.append("")
    return head + "\n```\n" + "\n".join(body).rstrip() + "\n```"


def build_tree(rows, cfg):
    """Arbre projet -> apps -> (Chrome) onglets, en secondes."""
    ss = cfg.get("sample_seconds", 20)
    tree = {}
    for r in rows:
        p = classify(r, cfg)
        app = r.get("app") or "—"
        node = tree.setdefault(p, {"total": 0, "apps": {}})
        node["total"] += ss
        a = node["apps"].setdefault(app, {"total": 0, "tabs": {}})
        a["total"] += ss
        if "chrome" in app.lower():
            # Libellé d'onglet : domaine si dispo, sinon titre de page (l'URL
            # n'est plus captable via AppleScript sur Chrome 15x).
            tab = label_domain(r.get("domain"), cfg) or r.get("title") or "—"
            a["tabs"][tab] = a["tabs"].get(tab, 0) + ss
    return tree


def _sorted_apps(apps):
    """Apps triées par temps décroissant, mais Chrome toujours en dernier."""
    items = sorted(apps.items(), key=lambda kv: kv[1]["total"], reverse=True)
    non_chrome = [x for x in items if "chrome" not in x[0].lower()]
    chrome = [x for x in items if "chrome" in x[0].lower()]
    return non_chrome + chrome


def _sorted_projects(tree):
    return sorted(tree.items(), key=lambda kv: kv[1]["total"], reverse=True)


def cmd_report(args):
    # garde-fou "dernier jour du mois" pour le bilan mensuel auto
    if getattr(args, "if_month_end", False):
        if (date.today() + timedelta(days=1)).day != 1:
            return
    cfg = load_config()
    start, end, label = _period_range(args)
    rows = _fetch(start, end)

    by_project = _aggregate(rows, cfg, lambda r: classify(r, cfg))
    by_app = _aggregate(rows, cfg, lambda r: r.get("app"))
    by_profile = _aggregate(rows, cfg, lambda r: r.get("profile"))
    by_domain = _aggregate(rows, cfg, lambda r: label_domain(r.get("domain"), cfg))
    total_s = sum(v for _, v in by_project)

    tree = build_tree(rows, cfg)

    W = 24
    print(f"\n  📊  ActivityMetrics — {label}")
    print(f"  Temps actif total : {_fmt_h(total_s)}\n")
    for proj, node in _sorted_projects(tree):
        ppct = (100 * node["total"] / total_s) if total_s else 0
        print("  " + _fmt_row(f"▸ {proj}", node["total"], ppct, W))
        for app, a in _sorted_apps(node["apps"]):
            apct = (100 * a["total"] / node["total"]) if node["total"] else 0
            print("  " + _fmt_row(f"   {app}", a["total"], apct, W, sub=True))
            for tab, s in sorted(a["tabs"].items(), key=lambda kv: kv[1],
                                 reverse=True):
                if tab == "—" or s < 60:
                    continue
                tpct = (100 * s / a["total"]) if a["total"] else 0
                print("  " + _fmt_row(f"     - {tab}", s, tpct, W, sub=True))
        print()

    cur_slug = _period_slug(args, start)
    want_pub = getattr(args, "html", False) or getattr(args, "telegram", False)
    # Menu latéral : liste des rapports déjà publiés (uniquement si on publie).
    nav = []
    if want_pub and cfg.get("publish"):
        nav = _list_remote_slugs(cfg)
        if cur_slug not in nav:
            nav = [cur_slug] + nav
    # Rapprochement Timesheet (jours facturés vs jours travaillés).
    recon = None
    if want_pub and cfg.get("timesheet"):
        ts = fetch_timesheet_days(cfg, start.isoformat(),
                                  (end - timedelta(days=1)).isoformat())
        if ts:
            am = am_day_equivalents(start, end, cfg)
            recon = {"ts": ts, "am": am,
                     "gap": round(am["fde"] - ts["total"]["realDays"], 1)}
    html_str = _render_html(label, total_s, tree, by_app, _gate_hash(cfg),
                            nav, cur_slug, recon)

    if getattr(args, "html", False):
        path = _write_html_local(html_str)
        print(f"  → Rapport HTML : {path}\n")

    if getattr(args, "telegram", False):
        url = publish_report(html_str, cur_slug, cfg)
        send_telegram(_telegram_text(label, total_s, tree, recon), button_url=url)
        if url:
            print(f"  → Détail publié : {url}\n")


def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;") \
        .replace("<", "&lt;").replace(">", "&gt;")


def _gate_hash(cfg):
    return (cfg.get("publish") or {}).get("password_sha256") or None


def _gate_assets(gate_hash):
    """(style, overlay, script) de la modale mot de passe (façon LP admin :
    champ unique, pas de nom d'utilisateur, pas de popup navigateur).
    gate_hash absent -> chaînes vides (aucune modale)."""
    if not gate_hash:
        return "", "", ""
    style = (
        " #am-gate{position:fixed;inset:0;z-index:999;background:#14141f;"
        "display:flex;align-items:center;justify-content:center;padding:24px}"
        " #am-gate.hide{display:none}"
        " .am-card{max-width:340px;width:100%;text-align:center;color:#e8e8f0;"
        "font:16px/1.5 -apple-system,system-ui,sans-serif}"
        " .am-card h1{font-size:1.5rem;margin:0 0 .3rem}"
        " .am-card p{color:#a0a0c0;font-size:.9rem;margin:0 0 1.2rem}"
        " .am-card input{width:100%;box-sizing:border-box;padding:.7rem .9rem;"
        "border-radius:10px;border:1px solid #2a2a3a;background:#1c1c28;"
        "color:#e8e8f0;font-size:1rem;text-align:center}"
        " .am-card input:focus{outline:none;border-color:#8b83ff}"
        " .am-card button{width:100%;margin-top:.7rem;padding:.7rem .9rem;"
        "border:none;border-radius:10px;background:#3f37c9;color:#fff;"
        "font-size:1rem;font-weight:700;cursor:pointer}"
        " .am-card button:hover{background:#8b83ff}"
        " .am-err{color:#e5484d;font-size:.85rem;min-height:1.1rem;margin-top:.6rem}"
        " #am-app{display:none} #am-app.show{display:block}"
    )
    overlay = (
        '<div id="am-gate"><div class="am-card">'
        "<h1>📊 ActivityMetrics</h1><p>Accès protégé</p>"
        '<input id="am-pw" type="password" placeholder="mot de passe" '
        'autocomplete="off" autofocus>'
        '<button id="am-go">Accéder</button>'
        '<div class="am-err" id="am-err"></div></div></div>'
    )
    script = (
        "<script>(function(){var H=\"" + gate_hash + "\";"
        "var g=document.getElementById('am-gate'),a=document.getElementById('am-app');"
        "function reveal(){g.classList.add('hide');a.classList.add('show');}"
        "async function sha(s){var b=await crypto.subtle.digest('SHA-256',"
        "new TextEncoder().encode(s));return Array.from(new Uint8Array(b))"
        ".map(function(x){return x.toString(16).padStart(2,'0');}).join('');}"
        "var i=document.getElementById('am-pw');"
        "function t(){sha(i.value.trim()).then(function(h){if(h===H){"
        "try{sessionStorage.setItem('am_ok','1');}catch(e){}reveal();}"
        "else{document.getElementById('am-err').textContent='Mot de passe incorrect.';i.select();}});}"
        "i.addEventListener('keydown',function(e){if(e.key==='Enter')t();});"
        "document.getElementById('am-go').addEventListener('click',t);"
        "try{if(sessionStorage.getItem('am_ok')==='1'){reveal();}else{i.focus();}}"
        "catch(e){i.focus();}})();</script>"
    )
    return style, overlay, script


def _nav_html(nav, current):
    """Menu latéral gauche : liens vers les derniers rapports."""
    if not nav:
        return ""
    groups = {"Jours": [], "Semaines": [], "Mois": [], "Autres": []}
    for s in nav:
        cat, lbl, sk = _index_label(s)
        groups[cat].append((sk, lbl, s))
    secs = []
    for cat in ("Jours", "Semaines", "Mois", "Autres"):
        items = sorted(groups[cat], reverse=True)[:8]
        if not items:
            continue
        links = "".join(
            f"<a class='{'cur' if s == current else ''}' "
            f"href='/activityMetrics/{_esc(s)}'>{_esc(lbl)}</a>"
            for _, lbl, s in items
        )
        secs.append(f"<h3>{cat}</h3>{links}")
    return ("<nav id='am-side'>"
            "<a class='am-home' href='/activityMetrics'>← Tous les rapports</a>"
            f"{''.join(secs)}</nav>")


# Script Node exécuté sur le VPS (lecture seule) : jours travaillés Timesheet
# sur [from,to]. Timesheet stocke des demi-journées (entries am/pm) ; cat='plan'
# = prévisionnel (non facturé), sinon réalisé.
_TS_NODE_JS = r"""
const nodeDir=process.argv[4]||'/opt/timesheet-api';
const Database=require(nodeDir+'/node_modules/better-sqlite3');
const db=new Database(nodeDir+'/data/timesheet.sqlite',{readonly:true});
const from=process.argv[2],to=process.argv[3];
const rows=db.prepare(`SELECT c.name AS client,
  SUM(CASE WHEN e.cat!='plan' THEN 1 ELSE 0 END) AS realHalf,
  SUM(CASE WHEN e.cat='plan' THEN 1 ELSE 0 END) AS planHalf
  FROM entries e JOIN clients c ON c.id=e.client_id
  WHERE e.date>=? AND e.date<=? AND c.is_demo=0 AND c.archived=0
  GROUP BY c.name HAVING realHalf>0 OR planHalf>0 ORDER BY realHalf DESC`).all(from,to);
const t=db.prepare(`SELECT
  SUM(CASE WHEN cat!='plan' THEN 1 ELSE 0 END) AS realHalf,
  SUM(CASE WHEN cat='plan' THEN 1 ELSE 0 END) AS planHalf,
  COUNT(DISTINCT CASE WHEN cat!='plan' THEN date END) AS dates
  FROM entries e JOIN clients c ON c.id=e.client_id
  WHERE e.date>=? AND e.date<=? AND c.is_demo=0 AND c.archived=0`).get(from,to);
console.log(JSON.stringify({clients:rows.map(r=>({client:r.client,realDays:r.realHalf/2,planDays:r.planHalf/2})),
  total:{realDays:(t.realHalf||0)/2,planDays:(t.planHalf||0)/2,dates:t.dates||0}}));
"""


def fetch_timesheet_days(cfg, from_iso, to_iso):
    """Jours travaillés Timesheet sur [from,to] via Node (ssh+sudo sur le VPS).
    None si config absente ou échec. Aucun mot de passe : lecture directe de la
    base en read-only via sudo."""
    ts = cfg.get("timesheet") or {}
    pub = cfg.get("publish") or {}
    target = ts.get("ssh_target") or pub.get("ssh_target")
    if not target:
        return None
    key = os.path.expanduser(ts.get("ssh_key") or pub.get("ssh_key",
                                                          "~/.ssh/id_ed25519"))
    node_dir = ts.get("node_dir", "/opt/timesheet-api")
    opts = ["-i", key, "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    os.makedirs(REPORTS_DIR, exist_ok=True)
    localjs = os.path.join(REPORTS_DIR, "_ts_days.js")
    with open(localjs, "w", encoding="utf-8") as f:
        f.write(_TS_NODE_JS)
    try:
        subprocess.run(["scp", *opts, localjs, f"{target}:/tmp/am_ts_days.js"],
                       check=True, timeout=30, capture_output=True)
        r = subprocess.run(
            ["ssh", *opts, target,
             f"sudo node /tmp/am_ts_days.js {from_iso} {to_iso} '{node_dir}'"],
            check=True, timeout=30, capture_output=True, text=True,
        )
        return json.loads(r.stdout)
    except Exception as e:
        print("Timesheet: lecture échouée:", e)
        return None


def am_day_equivalents(start, end, cfg):
    """Éq. jours travaillés côté ActivityMetrics : par jour calendaire,
    ≥ hours_full_day = 1 j, ≥ hours_half_day = ½ j, sinon 0."""
    ts = cfg.get("timesheet") or {}
    full = ts.get("hours_full_day", 5)
    half = ts.get("hours_half_day", 2)
    ss = cfg.get("sample_seconds", 20)
    conn = db()
    s = int(time.mktime(start.timetuple()))
    e = int(time.mktime(end.timetuple()))
    rows = conn.execute(
        "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') d, COUNT(*) n "
        "FROM samples WHERE idle=0 AND ts>=? AND ts<? GROUP BY d", (s, e),
    ).fetchall()
    conn.close()
    fde = 0.0
    active = 0
    secs = 0
    for _, n in rows:
        h = n * ss / 3600.0
        secs += n * ss
        if h > 0:
            active += 1
        if h >= full:
            fde += 1
        elif h >= half:
            fde += 0.5
    return {"fde": round(fde, 1), "active_days": active,
            "hours": round(secs / 3600.0, 1), "full_h": full, "half_h": half}


def _recon_assets(recon):
    """(bouton, encadré-modale, script) du rapprochement Timesheet.
    Bouton en haut à droite → ouvre un encadré au-dessus de tout le contenu.
    recon absent → chaînes vides."""
    if not recon:
        return "", "", ""
    ts, am = recon["ts"], recon["am"]
    gap = recon["gap"]
    cli = "".join(
        f"<tr class='sub'><td>↳ {_esc(c['client'])}</td>"
        f"<td class='n'>{c['realDays']:.1f} j</td><td></td></tr>"
        for c in ts.get("clients", [])
    )
    plan = ts["total"].get("planDays", 0)
    plan_row = (f"<tr><td>dont prévisionnel (non facturé)</td>"
                f"<td class='n'>{plan:.1f} j</td><td></td></tr>" if plan else "")
    gap_sign = "+" if gap >= 0 else ""
    rows = (
        f"<tr><td><strong>Jours facturés (Timesheet)</strong></td>"
        f"<td class='n'><strong>{ts['total']['realDays']:.1f} j</strong></td><td></td></tr>"
        f"{cli}{plan_row}"
        f"<tr><td><strong>Jours travaillés (ActivityMetrics)</strong><br>"
        f"<small>≥{int(am['full_h'])}h = 1 j, {int(am['half_h'])}–{int(am['full_h'])}h = ½ j"
        f" · {am['hours']:.0f}h actives</small></td>"
        f"<td class='n'><strong>{am['fde']:.1f} j</strong></td><td></td></tr>"
        f"<tr class='sub'><td>↳ jours calendaires avec activité</td>"
        f"<td class='n'>{am['active_days']} j</td><td></td></tr>"
        f"<tr><td><strong>Écart (activité non facturée)</strong></td>"
        f"<td class='n'><strong>{gap_sign}{gap:.1f} j</strong></td><td></td></tr>"
    )
    button = ("<button id='am-recon-btn'>🗓 Rapprochement "
              f"<b>{gap_sign}{gap:.1f} j</b></button>")
    overlay = (
        "<div id='am-recon' class='am-modal hide'><div class='am-modal-card'>"
        "<button class='am-modal-x' id='am-recon-x' aria-label='Fermer'>&times;</button>"
        "<h2 class='am-modal-h'>Rapprochement Timesheet</h2>"
        f"<table>{rows}</table></div></div>"
    )
    script = (
        "<script>(function(){var b=document.getElementById('am-recon-btn'),"
        "m=document.getElementById('am-recon'),x=document.getElementById('am-recon-x');"
        "function o(){m.classList.remove('hide');}function c(){m.classList.add('hide');}"
        "b.addEventListener('click',o);x.addEventListener('click',c);"
        "m.addEventListener('click',function(e){if(e.target===m)c();});"
        "document.addEventListener('keydown',function(e){if(e.key==='Escape')c();});"
        "})();</script>"
    )
    return button, overlay, script


def _render_html(label, total_s, tree, by_app, gate_hash=None, nav=None,
                 current=None, recon=None):
    """Rapport HTML complet : arbre projet › app › onglets (le « détail »)."""
    def pct(part, whole):
        return f"{round(100 * part / whole)}%" if whole else ""

    blocks = []
    for proj, node in _sorted_projects(tree):
        rows = []
        for app, a in _sorted_apps(node["apps"]):
            rows.append(
                f"<tr><td>{_esc(app)}</td><td class='n'>{_fmt_h(a['total'])}</td>"
                f"<td class='n'>{pct(a['total'], node['total'])}</td></tr>"
            )
            for tab, s in sorted(a["tabs"].items(), key=lambda kv: kv[1],
                                 reverse=True):
                if tab == "—" or s < 60:
                    continue
                rows.append(
                    f"<tr class='sub'><td>↳ {_esc(tab)}</td>"
                    f"<td class='n'>{_fmt_h(s)}</td>"
                    f"<td class='n'>{pct(s, a['total'])}</td></tr>"
                )
        blocks.append(
            f"<details><summary><span class='lbl'>"
            f"<span class='chev'>▸</span> {_esc(proj)}</span>"
            f"<span class='pt'>{_fmt_h(node['total'])} · "
            f"{pct(node['total'], total_s)}</span></summary>"
            f"<table>{''.join(rows)}</table></details>"
        )

    apps_rows = "".join(
        f"<tr><td>{_esc(app)}</td><td class='n'>{_fmt_h(s)}</td></tr>"
        for app, s in by_app[:12] if s >= 60
    )
    recon_btn, recon_overlay, recon_script = _recon_assets(recon)
    gstyle, goverlay, gscript = _gate_assets(gate_hash)
    nav_html = _nav_html(nav, current)

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ActivityMetrics — {_esc(label)}</title>
<style>
 body{{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;color:#1a1a2e}}
 .am-wrap{{display:flex;gap:2.5rem;max-width:1040px;margin:40px auto;padding:0 20px}}
 .am-main{{flex:1;min-width:0;max-width:760px}}
 #am-side{{width:190px;flex-shrink:0;position:sticky;top:40px;align-self:flex-start;font-size:.9rem;line-height:1.4}}
 #am-side .am-home{{display:block;font-weight:700;margin-bottom:.6rem}}
 #am-side h3{{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:#8a8aa0;margin:1rem 0 .25rem;border:0}}
 #am-side a{{display:block;padding:.2rem 0;color:inherit;text-decoration:none}}
 #am-side a:hover,#am-side a.cur{{color:#3f37c9;font-weight:700}}
 @media(max-width:720px){{.am-wrap{{flex-direction:column;gap:1rem}}#am-side{{width:auto;position:static}}#am-side h3{{margin-top:.5rem}}}}
 h1{{font-size:1.4rem;margin-bottom:.2rem}}
 h2,summary{{font-size:1.05rem;color:#1a1a2e;display:flex;justify-content:space-between;align-items:baseline;border-bottom:2px solid #3f37c9;padding-bottom:.2rem}}
 h2,details{{margin-top:1.8rem}}
 summary{{cursor:pointer;list-style:none;-webkit-tap-highlight-color:transparent}}
 summary::-webkit-details-marker{{display:none}}
 .chev{{display:inline-block;transition:transform .15s;font-size:.8em;color:#3f37c9}}
 details[open] summary .chev{{transform:rotate(90deg)}}
 .pt{{font-size:.9rem;font-weight:600;color:#3f37c9}}
 .total{{font-size:2rem;font-weight:700;color:#3f37c9;margin:.2rem 0 0}}
 table{{width:100%;border-collapse:collapse;margin-top:.3rem}}
 td{{padding:.35rem .2rem;border-bottom:1px solid #ececf5}}
 tr.sub td{{color:#6b6b80;font-size:.92rem;padding-left:1.2rem}}
 .n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
 small{{color:#6b6b80}}
 #am-recon-btn{{position:fixed;top:16px;right:16px;z-index:60;padding:.5rem .85rem;border:1px solid #3f37c9;border-radius:999px;background:#fff;color:#3f37c9;font-size:.85rem;font-weight:600;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
 #am-recon-btn b{{font-weight:800}}
 #am-recon-btn:hover{{background:#3f37c9;color:#fff}}
 .am-modal{{position:fixed;inset:0;z-index:200;background:rgba(20,20,31,.55);display:flex;align-items:center;justify-content:center;padding:20px}}
 .am-modal.hide{{display:none}}
 .am-modal-card{{position:relative;background:#fff;color:#1a1a2e;max-width:440px;width:100%;border-radius:14px;padding:24px;box-shadow:0 12px 48px rgba(0,0,0,.35)}}
 .am-modal-x{{position:absolute;top:8px;right:14px;border:0;background:none;font-size:1.7rem;line-height:1;color:#8a8aa0;cursor:pointer}}
 .am-modal-h{{border:0;display:block;margin:0 0 .6rem;font-size:1.15rem;font-weight:700}}
 @media(prefers-color-scheme:dark){{body{{background:#14141f;color:#e8e8f0}}h2,summary{{color:#e8e8f0;border-color:#8b83ff}}td{{border-color:#2a2a3a}}tr.sub td{{color:#a0a0c0}}.pt,.total,.chev{{color:#8b83ff}}#am-side a:hover,#am-side a.cur{{color:#8b83ff}}#am-recon-btn{{background:#1c1c28;color:#8b83ff;border-color:#8b83ff}}#am-recon-btn:hover{{background:#8b83ff;color:#14141f}}.am-modal-card{{background:#1c1c28;color:#e8e8f0}}}}
{gstyle}
</style></head><body>
{goverlay}
<div id="am-app">
{recon_btn}
{recon_overlay}
<div class="am-wrap">
{nav_html}
<main class="am-main">
<h1>📊 ActivityMetrics</h1><small>{_esc(label)}</small>
<p class="total">{_fmt_h(total_s)}</p><small>de temps actif</small>
{''.join(blocks)}
<details><summary><span class='lbl'><span class='chev'>▸</span> Par application (global)</span></summary><table>{apps_rows}</table></details>
</main>
</div></div>
{gscript}
{recon_script}
</body></html>"""


def _write_html_local(html_str):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = os.path.join(REPORTS_DIR, f"report-{stamp}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_str)
    return path


def publish_report(html_str, slug, cfg):
    """Publie le rapport HTML sur le VPS via scp et renvoie l'URL publique.
    Config dans clients.json > 'publish' (ssh_target, remote_dir, base_url,
    ssh_key). Sans config, ne publie pas (renvoie None)."""
    pub = cfg.get("publish") or {}
    target = pub.get("ssh_target")
    remote = (pub.get("remote_dir") or "").rstrip("/")
    base = (pub.get("base_url") or "").rstrip("/")
    if not (target and remote and base):
        return None
    key = os.path.expanduser(pub.get("ssh_key", "~/.ssh/id_ed25519"))
    os.makedirs(REPORTS_DIR, exist_ok=True)
    local = os.path.join(REPORTS_DIR, f"{slug}.html")
    with open(local, "w", encoding="utf-8") as f:
        f.write(html_str)
    opts = ["-i", key, "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    rdir = f"{remote}/{slug}"
    try:
        subprocess.run(["ssh", *opts, target, f"mkdir -p '{rdir}'"],
                       check=True, timeout=30, capture_output=True)
        subprocess.run(["scp", *opts, local, f"{target}:{rdir}/index.html"],
                       check=True, timeout=30, capture_output=True)
    except Exception as e:
        print("Publication VPS échouée:", e)
        return None
    _publish_index(cfg, opts, target, remote, base)
    return f"{base}/{slug}"


def _index_label(slug):
    """(catégorie, libellé lisible) d'un slug de rapport."""
    import re as _re
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", slug):
        d = datetime.strptime(slug, "%Y-%m-%d").date()
        return "Jours", _fr_date(d), d.isoformat()
    m = _re.fullmatch(r"semaine-(\d{4}-\d{2}-\d{2})", slug)
    if m:
        d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        return "Semaines", f"Semaine du {_fr_date(d)}", d.isoformat()
    if _re.fullmatch(r"\d{4}-\d{2}", slug):
        d = datetime.strptime(slug, "%Y-%m").date()
        return "Mois", f"{_FR_MOIS[d.month - 1].capitalize()} {d.year}", slug
    return "Autres", slug, slug


_INDEX_CSS = """
 body{font:16px/1.6 -apple-system,system-ui,sans-serif;margin:0;color:#1a1a2e;background:#fff}
 .am-cal{max-width:600px;margin:36px auto;padding:0 20px;text-align:center}
 h1{font-size:1.4rem;margin:.2rem 0}
 .am-sub{color:#6b6b80;font-size:.9rem;margin:0 0 1.4rem}
 .am-cal-head{display:flex;align-items:center;justify-content:center;gap:1rem;margin:.6rem 0}
 .am-cal-head button{border:1px solid #e0e0ea;background:#fff;border-radius:10px;width:42px;height:42px;font-size:1.1rem;cursor:pointer;color:#3f37c9}
 .am-cal-head button:hover{background:#f2f2fb}
 #mlabel{font-size:1.2rem;font-weight:700;min-width:220px;text-transform:capitalize}
 .am-mbtn{display:inline-block;margin:.2rem 0 1.2rem;padding:.55rem 1rem;border-radius:999px;background:#3f37c9;color:#fff;text-decoration:none;font-weight:700;font-size:.9rem}
 .am-mbtn:hover{background:#5b52e0}
 .am-mbtn.off{background:#d8d8e2;color:#9a9aab;pointer-events:none}
 .am-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
 .am-wd{font-size:.68rem;text-transform:uppercase;letter-spacing:.04em;color:#8a8aa0;font-weight:700;padding:2px 0}
 .am-cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;border-radius:10px;font-size:1rem;color:#c4c4d0;background:#f5f5fa}
 .am-cell.am-empty{background:none}
 a.am-cell.am-has{color:#fff;background:#3f37c9;font-weight:700;text-decoration:none}
 a.am-cell.am-has:hover{background:#5b52e0}
 .am-cell.am-today{outline:2px solid #ff8c42;outline-offset:-2px}
 #weeks{margin-top:1.6rem;text-align:left}
 #weeks h3{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:#8a8aa0;margin:0 0 .3rem}
 #weeks a{display:inline-block;margin:.15rem .5rem .15rem 0;color:#3f37c9;text-decoration:none;font-weight:600}
 @media(prefers-color-scheme:dark){body{background:#14141f;color:#e8e8f0}
  .am-cal-head button{background:#1c1c28;border-color:#2a2a3a;color:#8b83ff}
  .am-cal-head button:hover{background:#25253a}
  .am-cell{background:#1c1c28;color:#54546a}
  a.am-cell.am-has{background:#8b83ff;color:#14141f}
  a.am-cell.am-has:hover{background:#a49dff}
  .am-mbtn{background:#8b83ff;color:#14141f}.am-mbtn.off{background:#2a2a3a;color:#54546a}
  #weeks a{color:#8b83ff}}
"""

_INDEX_JS = """
<script>(function(){
 var DAYS=new Set(window.AM_DAYS||[]),MONTHS=new Set(window.AM_MONTHS||[]);
 var FM=["janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"];
 var WD=["Lun","Mar","Mer","Jeu","Ven","Sam","Dim"];
 var now=new Date();
 function pad(n){return(n<10?'0':'')+n;}
 var today=now.getFullYear()+'-'+pad(now.getMonth()+1)+'-'+pad(now.getDate());
 var latest=(window.AM_DAYS||[]).slice().sort().pop();
 var y,m;
 if(latest){var p=latest.split('-');y=+p[0];m=+p[1]-1;}else{y=now.getFullYear();m=now.getMonth();}
 function render(){
  var mkey=y+'-'+pad(m+1);
  document.getElementById('mlabel').textContent=FM[m]+' '+y;
  var mb=document.getElementById('mbtn');
  if(MONTHS.has(mkey)){mb.href='/activityMetrics/'+mkey;mb.classList.remove('off');}
  else{mb.removeAttribute('href');mb.classList.add('off');}
  var start=(new Date(y,m,1).getDay()+6)%7,nd=new Date(y,m+1,0).getDate(),h='';
  WD.forEach(function(d){h+='<div class="am-wd">'+d+'</div>';});
  for(var i=0;i<start;i++)h+='<div class="am-cell am-empty"></div>';
  for(var d=1;d<=nd;d++){var k=y+'-'+pad(m+1)+'-'+pad(d),t=(k===today?' am-today':'');
   if(DAYS.has(k))h+='<a class="am-cell am-has'+t+'" href="/activityMetrics/'+k+'">'+d+'</a>';
   else h+='<div class="am-cell'+t+'">'+d+'</div>';}
  document.getElementById('grid').innerHTML=h;
 }
 document.getElementById('prev').onclick=function(){if(--m<0){m=11;y--;}render();};
 document.getElementById('next').onclick=function(){if(++m>11){m=0;y++;}render();};
 render();
 var w=window.AM_WEEKS||[];
 if(w.length){var wh='<h3>Semaines</h3>';w.forEach(function(s){wh+='<a href="/activityMetrics/'+s+'">'+s.replace('semaine-','Semaine du ')+'</a>';});document.getElementById('weeks').innerHTML=wh;}
})();</script>
"""


def _render_index(slugs, gate_hash=None):
    days = sorted(s for s in slugs if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))
    months = sorted(s for s in slugs if re.fullmatch(r"\d{4}-\d{2}", s))
    weeks = sorted((s for s in slugs if s.startswith("semaine-")), reverse=True)
    data = ("<script>window.AM_DAYS=" + json.dumps(days)
            + ";window.AM_MONTHS=" + json.dumps(months)
            + ";window.AM_WEEKS=" + json.dumps(weeks) + ";</script>")
    gstyle, goverlay, gscript = _gate_assets(gate_hash)
    body = (
        "<div id='am-app'><div class='am-cal'>"
        "<h1>📊 ActivityMetrics</h1>"
        "<p class='am-sub'>Choisissez un jour, ou le bilan du mois.</p>"
        "<div class='am-cal-head'><button id='prev' aria-label='Mois précédent'>◀</button>"
        "<div id='mlabel'></div>"
        "<button id='next' aria-label='Mois suivant'>▶</button></div>"
        "<a id='mbtn' class='am-mbtn off'>Bilan du mois →</a>"
        "<div class='am-grid' id='grid'></div>"
        "<div id='weeks'></div>"
        "</div></div>"
    )
    return ("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>ActivityMetrics — rapports</title><style>"
            + _INDEX_CSS + gstyle + "</style></head><body>"
            + goverlay + body + data + _INDEX_JS + gscript
            + "</body></html>")


def _list_remote_slugs(cfg):
    """Slugs des rapports déjà publiés sur le VPS (dossiers). [] si échec."""
    pub = cfg.get("publish") or {}
    target = pub.get("ssh_target")
    remote = (pub.get("remote_dir") or "").rstrip("/")
    if not (target and remote):
        return []
    key = os.path.expanduser(pub.get("ssh_key", "~/.ssh/id_ed25519"))
    opts = ["-i", key, "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    try:
        r = subprocess.run(
            ["ssh", *opts, target, f"cd '{remote}' && ls -1d */ 2>/dev/null"],
            check=True, timeout=30, capture_output=True, text=True,
        )
        return [l.strip().rstrip("/") for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def _publish_index(cfg, opts, target, remote, base):
    """Régénère la page index (liste de tous les rapports) sur le VPS."""
    try:
        slugs = _list_remote_slugs(cfg)
        html = _render_index(slugs, _gate_hash(cfg))
        local = os.path.join(REPORTS_DIR, "index.html")
        with open(local, "w", encoding="utf-8") as f:
            f.write(html)
        subprocess.run(["scp", *opts, local, f"{target}:{remote}/index.html"],
                       check=True, timeout=30, capture_output=True)
    except Exception as e:
        print("Index: mise à jour échouée:", e)


def cmd_status(args):
    cfg = load_config()
    today = date.today()
    rows = _fetch(today, today + timedelta(days=1))
    by_project = _aggregate(rows, cfg, lambda r: classify(r, cfg))
    total_s = sum(v for _, v in by_project)
    print(f"Aujourd'hui : {_fmt_h(total_s)} actif")
    for name, s in by_project:
        print(f"  {name[:24]:24} {_fmt_h(s):>7}")


# --------------------------------------------------------------------------- #
# LaunchAgent (auto-run)
# --------------------------------------------------------------------------- #
def cmd_install(args):
    cfg = load_config()
    interval = cfg.get("sample_seconds", 20)
    py = sys.executable
    script = os.path.join(BASE, "activitymetrics.py")
    log = os.path.join(BASE, "probe.log")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>{script}</string><string>probe</string></array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
    with open(LAUNCH_AGENT_PATH, "w", encoding="utf-8") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", LAUNCH_AGENT_PATH],
                   capture_output=True)
    r = subprocess.run(["launchctl", "load", LAUNCH_AGENT_PATH],
                       capture_output=True, text=True)
    print(f"LaunchAgent installé : {LAUNCH_AGENT_PATH}")
    print(f"Échantillonnage toutes les {interval}s.")
    if r.returncode != 0:
        print("launchctl:", r.stderr.strip())


def cmd_uninstall(args):
    subprocess.run(["launchctl", "unload", LAUNCH_AGENT_PATH], capture_output=True)
    if os.path.exists(LAUNCH_AGENT_PATH):
        os.remove(LAUNCH_AGENT_PATH)
    print("LaunchAgent retiré.")


def _agent_path(suffix):
    return os.path.expanduser(
        f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.{suffix}.plist"
    )


def _write_calendar_agent(suffix, report_args, cal):
    """Installe un LaunchAgent déclenché à un horaire (StartCalendarInterval)."""
    py = sys.executable
    script = os.path.join(BASE, "activitymetrics.py")
    log = os.path.join(BASE, "report.log")
    cal_xml = "".join(
        f"<key>{k}</key><integer>{v}</integer>" for k, v in cal.items()
    )
    prog = "".join(f"<string>{a}</string>" for a in [py, script] + report_args)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCH_AGENT_LABEL}.{suffix}</string>
  <key>ProgramArguments</key><array>{prog}</array>
  <key>StartCalendarInterval</key><dict>{cal_xml}</dict>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    path = _agent_path(suffix)
    with open(path, "w", encoding="utf-8") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    subprocess.run(["launchctl", "load", path], capture_output=True)
    return path


def cmd_schedule(args):
    if args.off:
        for suffix in ("daily", "weekly", "monthly"):
            p = _agent_path(suffix)
            subprocess.run(["launchctl", "unload", p], capture_output=True)
            if os.path.exists(p):
                os.remove(p)
        print("Envois automatiques Telegram désactivés.")
        return
    if not _telegram_conf():
        print("⚠️  Configure d'abord telegram.json (token + chat_id).")
        return
    # bilan quotidien à 18h00
    _write_calendar_agent("daily", ["report", "--today", "--telegram"],
                          {"Hour": 18, "Minute": 0})
    # bilan hebdo le vendredi (Weekday 5) à 18h05
    _write_calendar_agent("weekly", ["report", "--week", "--telegram"],
                          {"Weekday": 5, "Hour": 18, "Minute": 5})
    # bilan mensuel : tourne chaque jour à 18h10 mais ne s'envoie
    # que le DERNIER jour du mois (garde-fou --if-month-end)
    _write_calendar_agent(
        "monthly", ["report", "--month", "--telegram", "--if-month-end"],
        {"Hour": 18, "Minute": 10})
    print("✅ Envois Telegram programmés :")
    print("   • Bilan quotidien — tous les jours à 18h00")
    print("   • Bilan hebdo — vendredi à 18h05")
    print("   • Bilan mensuel — dernier jour du mois à 18h10")


# --------------------------------------------------------------------------- #
# Setup — configuration guidée pour un nouvel utilisateur
# --------------------------------------------------------------------------- #
DEFAULT_DOMAIN_LABELS = {
    "mail.google.com": "Gmail", "calendar.google.com": "Agenda",
    "app.ringover.com": "Ringover", "app.hubspot.com": "HubSpot",
    "app.pennylane.com": "Pennylane", "app.claap.io": "Claap",
    "notion.so": "Notion", "linkedin.com": "LinkedIn",
    "docs.google.com": "Google Docs", "drive.google.com": "Google Drive",
    "meet.google.com": "Google Meet", "chatgpt.com": "ChatGPT",
    "claude.ai": "Claude",
}


def _detected_profiles():
    """[(nom_profil, email)] déclarés dans le Local State de Chrome."""
    out = []
    try:
        with open(CHROME_LOCAL_STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for folder, meta in data.get("profile", {}).get("info_cache", {}).items():
            out.append((meta.get("name") or folder, meta.get("user_name") or ""))
    except Exception:
        pass
    return out


def cmd_setup(args):
    """Prépare la config d'un nouvel utilisateur : détecte ses profils Chrome,
    génère un clients.json de départ (une règle par compte email), crée un
    telegram.json vierge, et affiche les étapes manuelles restantes."""
    profs = _detected_profiles()
    created = []

    # 1) clients.json — échafaudé depuis les comptes Chrome détectés
    if os.path.exists(CONFIG_PATH):
        print("• clients.json existe déjà — inchangé.")
    else:
        rules = []
        for name, email in profs:
            if email:  # une règle par compte : le contexte se lit dans le titre
                rules.append({"project": name, "priority": 50,
                              "match": {"title_contains": email}})
        cfg = {
            "sample_seconds": 20,
            "idle_threshold_seconds": 120,
            "default_project": profs[0][0] if profs else "Perso",
            "_doc": "Le profil Chrome n'est pas lisible ; on classe par compte "
                    "(title_contains email), domaine et url. Priorité la + haute "
                    "gagne. Édite librement puis relance un report (reclasse tout).",
            "rules": rules,
            "domain_labels": DEFAULT_DOMAIN_LABELS,
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        created.append("clients.json")

    # 2) telegram.json — gabarit vierge
    if os.path.exists(TELEGRAM_PATH):
        print("• telegram.json existe déjà — inchangé.")
    else:
        with open(TELEGRAM_PATH, "w", encoding="utf-8") as f:
            json.dump({"token": "", "chat_id": ""}, f, indent=2)
        created.append("telegram.json")

    print("\n📊 ActivityMetrics — configuration")
    print("=" * 50)
    if created:
        print("Fichiers créés :", ", ".join(created))
    print("\nProfils Chrome détectés :")
    for name, email in profs:
        print(f"  • {name}  ({email or 'sans compte Google'})")

    print("\nÉtapes restantes (manuelles) :")
    print("  1. Accessibilité : Réglages Système › Confidentialité et sécurité")
    print("     › Accessibilité › ajouter le binaire Python :")
    print(f"       {sys.executable}")
    print("  2. Telegram : crée un bot via @BotFather, colle le token dans")
    print("     telegram.json, envoie-lui 'hello', puis :")
    print("       python3 activitymetrics.py setup-telegram   # récupère le chat_id")
    print("  3. Lancer la capture :   python3 activitymetrics.py install")
    print("  4. Envois auto :         python3 activitymetrics.py schedule")
    print("\nAdapte ensuite clients.json à tes projets/clients. Détails : ONBOARDING.md")


def cmd_setup_telegram(args):
    """Récupère automatiquement le chat_id après que l'utilisateur a écrit au bot."""
    try:
        with open(TELEGRAM_PATH, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        print("telegram.json introuvable — lance d'abord `setup`.")
        return
    token = conf.get("token")
    if not token:
        print("Ajoute d'abord ton token de bot dans telegram.json.")
        return
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getUpdates", timeout=15
        ) as r:
            res = json.loads(r.read()).get("result", [])
    except Exception as e:
        print("Erreur Telegram:", e)
        return
    cid = None
    for u in res:
        m = u.get("message") or u.get("edited_message") or {}
        if m.get("chat", {}).get("id"):
            cid = m["chat"]["id"]
    if cid is None:
        print("Aucun message reçu. Envoie 'hello' à ton bot puis relance.")
        return
    conf["chat_id"] = str(cid)
    with open(TELEGRAM_PATH, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)
    print(f"✅ chat_id enregistré ({cid}). Teste : report --today --telegram")


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="ActivityMetrics — suivi du temps.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe"); sp.add_argument("--verbose", action="store_true")
    sub.add_parser("status")
    rp = sub.add_parser("report")
    rp.add_argument("--today", action="store_true")
    rp.add_argument("--week", action="store_true")
    rp.add_argument("--month", action="store_true")
    rp.add_argument("--day", metavar="YYYY-MM-DD")
    rp.add_argument("--html", action="store_true")
    rp.add_argument("--telegram", action="store_true")
    rp.add_argument("--if-month-end", action="store_true", dest="if_month_end")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sc = sub.add_parser("schedule")
    sc.add_argument("--off", action="store_true")
    sub.add_parser("setup")
    sub.add_parser("setup-telegram")

    args = p.parse_args()
    {
        "probe": cmd_probe,
        "status": cmd_status,
        "report": cmd_report,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "schedule": cmd_schedule,
        "setup": cmd_setup,
        "setup-telegram": cmd_setup_telegram,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
