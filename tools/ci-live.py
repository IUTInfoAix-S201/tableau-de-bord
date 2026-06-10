#!/usr/bin/env python3
"""
Moniteur CLI temps reel des runs CI de la SAE 2.01.

Affiche, en se rafraichissant, ce qui tourne / attend / vient de finir sur TOUS
les depots SAE : les 21 forks d'equipe (org IUTInfoAix-S201-2026), le repo
canonique, le tableau de bord et classroom-sync. Pratique quand un seul runner
self-hosted est partage : on voit le goulot (file d'attente) d'un coup d'oeil.

PREREQUIS : `gh` authentifie (`gh auth login`) avec lecture sur les orgs S201.

Usage :
    python3 tools/ci-live.py                 # boucle, rafraichi toutes les 20 s
    python3 tools/ci-live.py --interval 30   # autre cadence
    python3 tools/ci-live.py --once          # un seul affichage (scripts/cron)
    python3 tools/ci-live.py --recent 20     # garde les runs finis depuis < 20 min

Cout API : ~1 appel par repo et par rafraichissement (+1 pour le runner). A 20 s
sur ~24 repos -> ~4500 appels/h, sous la limite de 5000/h du token. Allonger
`--interval` si le token sert aussi a autre chose en parallele.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

FORKS_ORG = "IUTInfoAix-S201-2026"
FORK_PREFIX = "vigiechiro-pr-companion-"
RUNNER_ORG = "IUTInfoAix-S201-2026"

# Repos hors forks, avec un alias court pour l'affichage.
EXTRA_REPOS = {
    "IUTInfoAix-S201/vigiechiro-pr-companion": "· canonique",
    "IUTInfoAix-S201/tableau-de-bord": "· dashboard",
    "IUTInfoAix-R203/classroom-sync": "· classroom-sync",
}

PARALLELISME = 12
REDECOUVERTE_CYCLES = 15   # re-liste les forks tous les N cycles (capte les nouveaux)

# --- ANSI -------------------------------------------------------------------
RESET = "\033[0m"
def col(s, code):
    return f"\033[{code}m{s}{RESET}"
GREEN, RED, YELLOW, GRAY, BOLD, CYAN, BLUE = "32", "31", "33", "90", "1", "36", "34"
CLEAR = "\033[2J\033[H"   # efface l'ecran + curseur en haut


def gh(args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    return p.stdout.strip(), p.returncode


def gh_json(args):
    out, code = gh(args)
    if code != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def decouvrir_forks():
    names = gh_json(["repo", "list", FORKS_ORG, "--no-archived", "--limit", "300",
                     "--json", "name", "--jq", "[.[].name]"]) or []
    return sorted(f"{FORKS_ORG}/{n}" for n in names if n.startswith(FORK_PREFIX))


def alias(full):
    if full in EXTRA_REPOS:
        return EXTRA_REPOS[full]
    nom = full.split("/", 1)[1]
    return nom[len(FORK_PREFIX):] if nom.startswith(FORK_PREFIX) else nom


def runs_du_repo(full):
    data = gh_json(["run", "list", "--repo", full, "--limit", "8", "--json",
                    "workflowName,status,conclusion,headBranch,event,startedAt,createdAt,updatedAt,url"])
    return full, (data if isinstance(data, list) else [])


def runner_status():
    data = gh_json(["api", f"orgs/{RUNNER_ORG}/actions/runners",
                    "--jq", "[.runners[] | {name, status, busy}]"])
    return data if isinstance(data, list) else []


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def depuis(s, now):
    d = _dt(s)
    if not d:
        return ""
    sec = int((now - d).total_seconds())
    if sec < 0:
        sec = 0
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


# rang de tri + glyphe/couleur par etat
def classer(r):
    st, cc = r.get("status"), r.get("conclusion")
    if st == "in_progress":
        return 0, col("● en cours", YELLOW)
    if st == "queued":
        return 1, col("○ en file", GRAY)
    if st in ("requested", "waiting", "pending"):
        return 1, col("○ " + st, GRAY)
    # completed
    glyph = {"success": col("✓ ok", GREEN), "failure": col("✗ echec", RED),
             "cancelled": col("⊘ annule", GRAY), "skipped": col("• skip", GRAY)}.get(
                 cc, col(f"• {cc}", GRAY))
    return 2, glyph


def construire_lignes(resultats, now, recent_min):
    lignes = []
    for full, runs in resultats:
        for r in runs:
            st = r.get("status")
            if st != "completed":
                rang, etat = classer(r)
                quand = depuis(r.get("startedAt") or r.get("createdAt"), now) if st == "in_progress" else ""
            else:
                fin = _dt(r.get("updatedAt"))
                if not fin or (now - fin).total_seconds() > recent_min * 60:
                    continue   # trop ancien -> on ne garde que l'actif + le tout recent
                rang, etat = classer(r)
                quand = "il y a " + depuis(r.get("updatedAt"), now)
            lignes.append({
                "rang": rang, "repo": alias(full), "etat": etat,
                "wf": r.get("workflowName") or "?", "branche": r.get("headBranch") or "?",
                "quand": quand, "url": r.get("url") or "",
                "tri2": r.get("startedAt") or r.get("createdAt") or "",
            })
    lignes.sort(key=lambda x: (x["rang"], x["repo"]) if x["rang"] < 2 else (x["rang"], _inv(x["tri2"])))
    return lignes


def _inv(s):
    # pour trier les "finis" du plus recent au plus ancien
    return tuple(-b for b in s.encode()) if s else ()


def largeur_visible(s):
    # longueur sans les codes ANSI (pour l'alignement)
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def pad(s, n):
    return s + " " * max(0, n - largeur_visible(s))


def afficher(now, runner, lignes, interval, once):
    out = [] if once else [CLEAR]
    # bandeau runner
    if runner:
        etats = []
        for rn in runner:
            if rn.get("status") != "online":
                etats.append(col(f"{rn['name']} HORS LIGNE", RED))
            elif rn.get("busy"):
                etats.append(col(f"{rn['name']} OCCUPE", YELLOW))
            else:
                etats.append(col(f"{rn['name']} libre", GREEN))
        bandeau = "RUNNER : " + "  ".join(etats)
    else:
        bandeau = col("RUNNER : n/d (pas de runner self-hosted au niveau org ?)", GRAY)
    en_cours = sum(1 for l in lignes if l["rang"] == 0)
    en_file = sum(1 for l in lignes if l["rang"] == 1)
    out.append(col("SAE 2.01 - CI en direct", BOLD) + "   " + now.astimezone().strftime("%H:%M:%S"))
    out.append(bandeau)
    out.append(f"  {col(str(en_cours)+' en cours', YELLOW)} · {col(str(en_file)+' en file', GRAY)} · "
               f"{len(lignes)} lignes")
    out.append("")
    if not lignes:
        out.append(col("  (rien d'actif ni de recemment termine)", GRAY))
    else:
        wrepo = max((largeur_visible(l["repo"]) for l in lignes), default=4)
        wetat = max((largeur_visible(l["etat"]) for l in lignes), default=8)
        wwf = max((len(l["wf"]) for l in lignes), default=8)
        for l in lignes:
            out.append("  " + pad(l["repo"], wrepo) + "  " + pad(l["etat"], wetat) + "  "
                       + l["wf"].ljust(wwf) + "  " + l["branche"].ljust(10) + "  "
                       + col(l["quand"], GRAY))
    if not once:
        out.append("")
        out.append(col(f"  rafraichi toutes les {interval}s · Ctrl-C pour quitter", GRAY))
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description="Moniteur CLI temps reel des runs CI SAE.")
    ap.add_argument("--interval", type=int, default=20, help="secondes entre rafraichissements")
    ap.add_argument("--once", action="store_true", help="un seul affichage puis quitte")
    ap.add_argument("--recent", type=int, default=10, help="garder les runs finis depuis < N minutes")
    args = ap.parse_args()

    if gh(["auth", "status"])[1] != 0:
        print("ERREUR : `gh` non authentifie (lancer `gh auth login`).", file=sys.stderr)
        return 1

    repos, cycle = [], 0
    try:
        while True:
            if cycle % REDECOUVERTE_CYCLES == 0:
                forks = decouvrir_forks()
                repos = forks + list(EXTRA_REPOS)
            now = datetime.now(timezone.utc)
            with ThreadPoolExecutor(max_workers=PARALLELISME) as ex:
                resultats = list(ex.map(runs_du_repo, repos))
            runner = runner_status()
            lignes = construire_lignes(resultats, now, args.recent)
            afficher(now, runner, lignes, args.interval, args.once)
            if args.once:
                break
            cycle += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nArret.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
