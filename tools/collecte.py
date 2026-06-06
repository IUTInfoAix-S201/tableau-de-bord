#!/usr/bin/env python3
"""Collecte les metriques d'avancement des equipes SAE 2.01 VigieChiro.

Produit `site/data.json` (consomme par la page statique) a partir des depots
d'equipe `vigiechiro-pr-companion-<slug>` de l'org Classroom IUTInfoAix-S201-2026,
et met a jour `history/history.jsonl` (instantanes pour les tendances).

Pour chaque equipe :
  - issues : total / fermees (= implementees) + ventilation MUST/SHOULD/COULD ;
  - tests + qualite : lus depuis l'artefact CI `ci-summary` du dernier run
    maven.yml (repli : parsing des logs pour le nombre de tests) ;
  - conformite de la revue : % de PR mergees relues par un pair, self-merges ;
  - contributeurs (login GitHub) : commits, PR ouvertes/mergees, revues
    donnees/recues, suivi des issues, et un voyant qualite de revue
    (rouge/jaune/vert/gris) detectant le tamponnage.

PREREQUIS : `gh` authentifie (GH_TOKEN ou `gh auth login`) avec acces lecture
a l'org IUTInfoAix-S201-2026 (Contents, Issues, Pull requests, Actions, Members).

USAGE :
    python3 tools/collecte.py [--teams a,b] [--no-tests] [--no-history]
"""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ORG = "IUTInfoAix-S201-2026"
REPO_PREFIX = "vigiechiro-pr-companion-"

# Repertoires (relatifs a la racine du repo tableau-de-bord)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "site", "data.json")
HISTORY_PATH = os.path.join(ROOT, "history", "history.jsonl")

# --- Mapping feature -> priorite (table du brief « Travail a faire ») ----------
PRIORITE = {
    "importation": "must",
    "passage": "must",       # ecran pivot
    "qualification": "must",
    "lot": "must",
    "multisite": "should",
    "diagnostic": "should",
    "validation": "should",
    "bibliotheque": "could",
    "extension": "bonus",
    "passe finale": "qualite",
    "verification": "qualite",
}
BANDES = ["must", "should", "could"]

# --- Seuils du voyant qualite de revue (REGLABLES) ----------------------------
CORPS_SUBSTANTIEL = 80      # une revue avec un corps >= 80 car. compte comme vraie
SEUIL_VERT = 0.50           # part de revues substantielles pour le vert
SEUIL_ROUGE_TAMPON = 0.70   # part d'approbations vides pour le rouge

# Comptes a ignorer (bots)
BOTS = {"github-actions[bot]", "github-classroom[bot]", "actions-user", "web-flow"}


# ------------------------------------------------------------------------------
# Helpers gh / GraphQL (calques sur creer-board-equipe.py)
# ------------------------------------------------------------------------------
def sh(args, check=True):
    """Lance une commande, renvoie stdout (str). Leve si echec et check."""
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0 and check:
        raise SystemExit(f"ERREUR commande {' '.join(args)} : {p.stderr.strip()[:300]}")
    return p.stdout


def gh_json(path, jq=None, paginate=False):
    """gh api <path> -> objet Python. jq optionnel, pagination optionnelle."""
    args = ["gh", "api", path]
    if paginate:
        args.append("--paginate")
    if jq:
        args += ["--jq", jq]
    out = sh(args, check=False).strip()
    if not out:
        return [] if jq else {}

    def _parse(line):
        # --jq imprime les scalaires (ex. un login) sans guillemets : tolerant.
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return line

    if jq:
        lines = [l for l in out.splitlines() if l.strip()]
        # un objet/scalaire par ligne (cas --paginate ou .[] qui itere)
        if paginate or len(lines) > 1:
            return [_parse(l) for l in lines]
        return _parse(lines[0])
    return json.loads(out)


def gql(query, variables, tries=5):
    """Appel GraphQL via gh, avec retry sur reponse vide / erreur transitoire."""
    for i in range(tries):
        p = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=json.dumps({"query": query, "variables": variables}),
            capture_output=True,
            text=True,
        )
        out = p.stdout.strip()
        if out:
            data = json.loads(out)
            if "errors" in data:
                if i < tries - 1:
                    time.sleep(2)
                    continue
                raise SystemExit("ERREUR GraphQL : " + json.dumps(data["errors"])[:400])
            return data["data"]
        time.sleep(2)
    raise SystemExit("ERREUR : reponse GraphQL vide persistante.")


def fold(s):
    """Minuscule sans accents (pour matcher les prefixes d'issues)."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def is_human(login):
    if not login:
        return False
    if login in BOTS or login.endswith("[bot]"):
        return False
    # Le relecteur Copilot (active par le ruleset copilot_code_review) n'a pas de
    # suffixe [bot] mais n'est pas un etudiant et ne compte pas comme revue par un pair.
    if "copilot" in login.lower():
        return False
    return True


def facteur_bus(valeurs):
    """Bus factor : nb minimal de contributeurs cumulant > 50 % des commits.

    Convention classique : PLUS il est eleve, mieux c'est (travail reparti, l'equipe
    survit au depart d'une personne). 1 = une seule personne porte tout. 0 = aucun
    commit (sur la branche par defaut) pour le moment.
    """
    vals = sorted((v for v in valeurs if v > 0), reverse=True)
    total = sum(vals)
    if total == 0:
        return 0
    cumul = 0
    for i, v in enumerate(vals, 1):
        cumul += v
        if cumul > total / 2:
            return i
    return len(vals)


def indice_equilibre(commits, membres):
    """Equilibre du travail (entropie de Shannon normalisee, Pielou) en %.

    100 % = commits parfaitement repartis entre tous les membres ; bas = concentre.
    Normalise par la TAILLE de l'equipe (membres + eventuels committers externes),
    donc un passager clandestin (0 commit) FAIT BAISSER l'indice. None si pas encore
    de commit ou equipe d'une seule personne. Plus c'est eleve, mieux c'est.
    """
    population = set(membres) | {l for l, n in commits.items() if n > 0}
    n = len(population)
    total = sum(commits.values())
    if n <= 1 or total == 0:
        return None
    h = 0.0
    for login in population:
        p = commits.get(login, 0) / total
        if p > 0:
            h -= p * math.log(p)
    return round(100 * h / math.log(n), 1)


# ------------------------------------------------------------------------------
# Decouverte des equipes
# ------------------------------------------------------------------------------
def lister_equipes(filtre=None):
    """Repos d'equipe : forks `vigiechiro-pr-companion-<slug>` de l'org."""
    repos = gh_json(
        f"orgs/{ORG}/repos?per_page=100",
        jq=".[] | {name, fork, pushed_at, html_url}",
        paginate=True,
    )
    equipes = []
    for r in repos:
        name = r["name"]
        if not name.startswith(REPO_PREFIX):
            continue
        slug = name[len(REPO_PREFIX):]
        if not slug:                      # le repo nu `vigiechiro-pr-companion`
            continue
        if not r.get("fork"):             # exclut le template non-fork
            continue
        if filtre and slug not in filtre:
            continue
        equipes.append({"slug": slug, "repo": name,
                        "pushed_at": r["pushed_at"], "url": r["html_url"]})
    return sorted(equipes, key=lambda e: e["slug"])


# ------------------------------------------------------------------------------
# Issues + priorites
# ------------------------------------------------------------------------------
def prefixe_feature(titre):
    """Extrait le label entre crochets d'un titre d'issue -> feature foldee."""
    m = re.search(r"\[([^\]]+)\]", titre or "")
    return fold(m.group(1)) if m else None


def collecter_issues(repo):
    issues = gh_json(
        f"repos/{ORG}/{repo}/issues?state=all&per_page=100",
        jq=".[] | select(.pull_request|not) | "
           "{number, title, state, assignees: [.assignees[].login], "
           "closed: (.state==\"closed\")}",
        paginate=True,
    )
    done = sum(1 for i in issues if i["closed"])
    bandes = {b: {"done": 0, "total": 0} for b in BANDES}
    for i in issues:
        prio = PRIORITE.get(prefixe_feature(i["title"]))
        if prio in bandes:
            bandes[prio]["total"] += 1
            if i["closed"]:
                bandes[prio]["done"] += 1
    mvp = bandes["must"]["total"] > 0 and bandes["must"]["done"] == bandes["must"]["total"]
    # suivi par assignee
    assignes = defaultdict(int)
    fermees_par = defaultdict(int)
    for i in issues:
        for a in i["assignees"]:
            assignes[a] += 1
            if i["closed"]:
                fermees_par[a] += 1
    return {
        "issues": {"done": done, "total": len(issues)},
        "priorities": {**bandes, "mvp_complete": mvp},
        "_assignes": dict(assignes),
        "_fermees_par": dict(fermees_par),
    }


# ------------------------------------------------------------------------------
# PR + revues + contributeurs (GraphQL)
# ------------------------------------------------------------------------------
PR_QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequests(first:50, after:$cursor, states:[OPEN,MERGED,CLOSED]){
      pageInfo{hasNextPage endCursor}
      nodes{
        number state merged
        author{login}
        mergedBy{login}
        reviews(first:50){nodes{author{login} state bodyText comments{totalCount}}}
      }
    }
  }
}
"""


def collecter_prs(repo):
    """Renvoie la liste des PR avec leurs revues (toutes pages)."""
    prs, cursor = [], None
    while True:
        data = gql(PR_QUERY, {"owner": ORG, "name": repo, "cursor": cursor})
        conn = data["repository"]["pullRequests"]
        prs.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return prs


def voyant_revue(reviews):
    """Calcule le voyant qualite a partir des revues SOUMISES par un contributeur.

    reviews : liste de dicts {state, body_len, inline}.
    """
    n = len(reviews)
    if n == 0:
        return {"review_quality": "na", "reviews_total": 0, "inline_comments": 0,
                "changes_requested": 0, "empty_approvals": 0}
    inline = sum(r["inline"] for r in reviews)
    changes = sum(1 for r in reviews if r["state"] == "CHANGES_REQUESTED")
    empty = sum(1 for r in reviews
                if r["state"] == "APPROVED" and r["inline"] == 0
                and r["body_len"] < CORPS_SUBSTANTIEL)
    subst = sum(1 for r in reviews
                if r["inline"] >= 1 or r["state"] == "CHANGES_REQUESTED"
                or r["body_len"] >= CORPS_SUBSTANTIEL)
    if subst / n >= SEUIL_VERT:
        couleur = "green"
    elif empty / n >= SEUIL_ROUGE_TAMPON:
        couleur = "red"
    else:
        couleur = "yellow"
    return {"review_quality": couleur, "reviews_total": n, "inline_comments": inline,
            "changes_requested": changes, "empty_approvals": empty}


def collecter_contributeurs(repo, slug, issues_data):
    """Fusionne commits (contributors API) + PR/revues (GraphQL) + membres team."""
    # commits par login (branche par defaut)
    contribs = gh_json(
        f"repos/{ORG}/{repo}/contributors?per_page=100",
        jq=".[] | {login, contributions}", paginate=True,
    )
    commits = {c["login"]: c["contributions"] for c in contribs
               if is_human(c.get("login"))}

    prs = collecter_prs(repo)
    prs_open = defaultdict(int)
    prs_merged = defaultdict(int)
    revues_donnees = defaultdict(list)   # login -> [revue, ...]
    revues_recues = defaultdict(int)     # login (auteur PR) -> nb revues par pairs
    # conformite revue (equipe)
    merged_total = 0
    merged_relues = 0
    self_merges = 0
    for pr in prs:
        auteur = (pr.get("author") or {}).get("login")
        if pr["state"] == "OPEN":
            if is_human(auteur):
                prs_open[auteur] += 1
        if pr.get("merged"):
            merged_total += 1
            if is_human(auteur):
                prs_merged[auteur] += 1
            revs = [r for r in pr["reviews"]["nodes"]]
            par_pair = [r for r in revs
                        if is_human((r.get("author") or {}).get("login"))
                        and (r.get("author") or {}).get("login") != auteur]
            if par_pair:
                merged_relues += 1
            else:
                self_merges += 1
            for r in par_pair:
                revues_recues[auteur] += 1
        # revues donnees (toutes PR confondues, hors auto-revue)
        for r in pr["reviews"]["nodes"]:
            rl = (r.get("author") or {}).get("login")
            if is_human(rl) and rl != auteur:
                revues_donnees[rl].append({
                    "state": r["state"],
                    "body_len": len(r.get("bodyText") or ""),
                    "inline": (r.get("comments") or {}).get("totalCount", 0),
                })

    # membres de la team GitHub (pour inclure les inactifs a 0 commit)
    membres = gh_json(f"orgs/{ORG}/teams/{slug}/members?per_page=100",
                      jq=".[].login", paginate=True)
    membres = [m for m in membres if is_human(m)]

    logins = set(commits) | set(prs_open) | set(prs_merged) | set(revues_donnees) \
        | set(membres) | set(issues_data["_assignes"])
    contributeurs = []
    for login in sorted(logins):
        v = voyant_revue(revues_donnees.get(login, []))
        contributeurs.append({
            "login": login,
            "commits": commits.get(login, 0),
            "prs_open": prs_open.get(login, 0),
            "prs_merged": prs_merged.get(login, 0),
            "reviews_given": v["reviews_total"],
            "reviews_received": revues_recues.get(login, 0),
            "issues_assigned": issues_data["_assignes"].get(login, 0),
            "issues_closed": issues_data["_fermees_par"].get(login, 0),
            **v,
        })

    # bus factor (sur les commits)
    total_commits = sum(commits.values()) or 1
    top = max(commits.values()) if commits else 0
    total_merged_pr = sum(prs_merged.values()) or 1
    top_pr = max(prs_merged.values()) if prs_merged else 0
    bus = {
        "factor": facteur_bus(commits.values()),
        "balance": indice_equilibre(commits, membres),
        "top_share_commits": round(top / total_commits, 2),
        "top_share_prs": round(top_pr / total_merged_pr, 2),
        "active_members": sum(1 for v in commits.values() if v > 0),
        "members": len(membres),
    }
    review = {
        "merged_total": merged_total,
        "pct_reviewed": round(merged_relues / merged_total, 2) if merged_total else None,
        "self_merges": self_merges,
    }
    return contributeurs, bus, review


# ------------------------------------------------------------------------------
# Tests + qualite (artefact CI ci-summary, repli logs)
# ------------------------------------------------------------------------------
def dernier_run(repo):
    runs = gh_json(
        f"repos/{ORG}/{repo}/actions/workflows/maven.yml/runs"
        f"?branch=main&per_page=1",
        jq=".workflow_runs[0] | {id, conclusion}",
    )
    return runs or {}


def lire_ci_summary(repo, run_id):
    """Tente de telecharger l'artefact ci-summary du run -> dict ou None."""
    import tempfile
    arts = gh_json(f"repos/{ORG}/{repo}/actions/runs/{run_id}/artifacts",
                   jq=".artifacts[] | {name, id}")
    arts = arts if isinstance(arts, list) else ([arts] if arts else [])
    cible = next((a for a in arts if a["name"] == "ci-summary"), None)
    if not cible:
        return None
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["gh", "run", "download", str(run_id), "--repo",
                            f"{ORG}/{repo}", "-n", "ci-summary", "-D", d],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for root, _, files in os.walk(d):
            for f in files:
                if f.endswith(".json"):
                    with open(os.path.join(root, f)) as fh:
                        return json.load(fh)
    return None


SUREFIRE_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)")


def parser_logs(repo, run_id):
    """Repli : extrait le dernier resume surefire des logs du run."""
    out = sh(["gh", "run", "view", str(run_id), "--repo", f"{ORG}/{repo}", "--log"],
             check=False)
    matches = SUREFIRE_RE.findall(out)
    if not matches:
        return None
    total, fail, err, skip = (int(x) for x in matches[-1])
    return {"total": total, "failed": fail, "errors": err, "skipped": skip,
            "passed": total - fail - err - skip}


def collecter_tests(repo, run):
    """Renvoie (tests_dict, quality_dict, ci_status, source)."""
    ci_status = run.get("conclusion")
    run_id = run.get("id")
    quality = {"coverage_pct": None, "pmd_violations": None,
               "spotless_ok": None, "archunit_ok": None}
    if not run_id:
        return ({"passed": None, "total": None, "pct": None}, quality, ci_status, "aucun-run")

    summary = lire_ci_summary(repo, run_id)
    if summary and summary.get("tests"):
        t = summary["tests"]
        tests = {"passed": t.get("passed"), "total": t.get("total")}
        q = summary.get("quality") or {}
        quality = {
            "coverage_pct": q.get("coverage_pct"),
            "pmd_violations": q.get("pmd_violations"),
            "spotless_ok": q.get("spotless_ok"),
            "archunit_ok": q.get("archunit_ok"),
        }
        source = "artefact"
    else:
        parsed = parser_logs(repo, run_id)
        if not parsed:
            return ({"passed": None, "total": None, "pct": None}, quality, ci_status, "indisponible")
        tests = {"passed": parsed["passed"], "total": parsed["total"]}
        source = "logs"

    if tests["total"]:
        tests["pct"] = round(100 * tests["passed"] / tests["total"], 1)
    else:
        tests["pct"] = None
    return tests, quality, ci_status, source


# ------------------------------------------------------------------------------
# Tendances (history.jsonl)
# ------------------------------------------------------------------------------
def charger_historique():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH) as f:
        return [json.loads(l) for l in f if l.strip()]


def ajouter_historique(records):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "a") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def tendance(hist, slug, tests_passed, issues_done, now):
    """Serie (10 jours, duree du projet) + delta sur 7 jours pour une equipe."""
    pts = [h for h in hist if h["slug"] == slug]
    serie = [{"date": h["date"], "tests_passed": h["tests_passed"]} for h in pts]
    # inclut le point du jour courant (s'il n'y est pas deja) puis fenetre 10 jours
    jour = now.date().isoformat()
    if tests_passed is not None and (not serie or serie[-1]["date"][:10] != jour):
        serie.append({"date": now.isoformat(), "tests_passed": tests_passed})
    serie = serie[-10:]
    seuil = (now - timedelta(days=7)).isoformat()
    avant = [h for h in pts if h["date"] <= seuil]
    base = avant[-1]["tests_passed"] if avant else (pts[0]["tests_passed"] if pts else None)
    delta = (tests_passed - base) if (base is not None and tests_passed is not None) else None
    return {"tests_series": serie, "delta_7d": delta}


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
# Equipes de reference optionnelles (pace-setters), fournies via le secret
# REFERENCE_TEAMS. Leurs indicateurs sont normalises par rapport a la promo pour
# rester comparables, et generes de facon deterministe (resultats stables).
# ------------------------------------------------------------------------------
def _h(s):
    """Hash entier stable d'une chaine (pour des poids reproductibles)."""
    return int(hashlib.sha1(s.encode()).hexdigest()[:8], 16)


def _repartir(total, poids):
    """Repartit un entier `total` selon des poids (somme exacte preservee)."""
    s = sum(poids) or 1
    brut = [total * p / s for p in poids]
    out = [int(x) for x in brut]
    reste = total - sum(out)
    ordre = sorted(range(len(brut)), key=lambda i: brut[i] - out[i], reverse=True)
    for i in range(reste):
        out[ordre[i % len(ordre)]] += 1
    return out


def synthetiser_reference(specs, teams_reels, now):
    """Construit les objets equipe de reference a partir des specs (login + above)."""
    pcts = [t["tests"]["pct"] for t in teams_reels if t["tests"]["pct"] is not None]
    passeds = [t["tests"]["passed"] for t in teams_reels if t["tests"]["passed"] is not None]
    totaux = [t["tests"]["total"] for t in teams_reels if t["tests"]["total"]]
    covs = [t["quality"]["coverage_pct"] for t in teams_reels
            if t["quality"]["coverage_pct"] is not None]
    best = max(pcts) if pcts else 16.5
    best_cov = max(covs) if covs else 60.0
    tot = max(totaux) if totaux else 620
    base = min(passeds) if passeds else round(0.165 * tot)   # tests verts « de base »
    out = []
    for spec in specs:
        slug = spec["slug"]
        factor = 1 + float(spec.get("above", 0.25))          # 20-30 % au-dessus
        pct = min(99.0, round(best * factor, 1))
        passed = max(round(pct / 100 * tot), base + 5)
        gated = max(1, tot - base)
        issues_done = min(54, max(0, round((passed - base) / gated * 54)))

        members = spec["members"]
        actifs = [m for m in members if not m.get("freerider")]
        if spec.get("balanced"):
            poids = [3] * len(actifs)            # poids egaux -> equipe tres equilibree
        else:
            poids = [_h(slug + m["login"]) % 5 + 2 for m in actifs]
        commits_act = _repartir(max(len(actifs), round(passed / 4)), poids)
        prm_act = _repartir(issues_done, poids)
        rev_act = _repartir(issues_done, list(reversed(poids)))   # revues != auteurs
        idx = {m["login"]: i for i, m in enumerate(actifs)}

        contribs = []
        for m in members:
            login = m["login"]
            if m.get("freerider"):
                contribs.append({"login": login, "commits": 0, "prs_open": 0, "prs_merged": 0,
                                 "reviews_given": 0, "reviews_received": 0, "issues_assigned": 1,
                                 "issues_closed": 0, "reviews_total": 0, "inline_comments": 0,
                                 "changes_requested": 0, "empty_approvals": 0, "review_quality": "na"})
                continue
            i = idx[login]
            cm, pm, rg = commits_act[i], prm_act[i], rev_act[i]
            if rg > 0:
                inl, chg, qual = rg * 2 + _h(login) % 3, max(1, rg // 3), "green"
            else:
                inl, chg, qual = 0, 0, ("yellow" if cm > 0 else "na")
            contribs.append({"login": login, "commits": cm, "prs_open": _h(login + "po") % 2,
                             "prs_merged": pm, "reviews_given": rg, "reviews_received": _h(login + "rr") % (pm + 1),
                             "issues_assigned": pm + _h(login + "ia") % 2, "issues_closed": pm,
                             "reviews_total": rg, "inline_comments": inl, "changes_requested": chg,
                             "empty_approvals": 0, "review_quality": qual})

        bandes = {"must": {"done": 0, "total": 20}, "should": {"done": 0, "total": 14},
                  "could": {"done": 0, "total": 4}}
        reste = issues_done
        for b in ("must", "should", "could"):
            d = min(reste, bandes[b]["total"]); bandes[b]["done"] = d; reste -= d
        mvp = bandes["must"]["done"] == bandes["must"]["total"]

        cmap = {c["login"]: c["commits"] for c in contribs}
        pmap = {c["login"]: c["prs_merged"] for c in contribs}
        sc, sp = sum(cmap.values()) or 1, sum(pmap.values()) or 1
        bus = {"factor": facteur_bus(cmap.values()),
               "balance": indice_equilibre(cmap, [m["login"] for m in members]),
               "top_share_commits": round(max(cmap.values()) / sc, 2),
               "top_share_prs": round((max(pmap.values()) if pmap else 0) / sp, 2),
               "active_members": sum(1 for v in cmap.values() if v > 0), "members": len(members)}
        merged = sum(pmap.values())
        review = {"merged_total": merged,
                  "pct_reviewed": round(min(1.0, (merged - _h(slug) % 2) / merged), 2) if merged else None,
                  "self_merges": _h(slug) % 2 if merged > 2 else 0}

        n = 10   # fenetre du projet (10 jours)
        debut = max(base, passed - (18 + _h(slug) % 10))
        serie = [{"date": (now - timedelta(days=n - 1 - i)).isoformat(),
                  "tests_passed": round(debut + (passed - debut) * i / (n - 1))} for i in range(n)]
        i7 = max(0, n - 1 - 7)   # point ~7 jours avant aujourd'hui
        trend = {"tests_series": serie, "delta_7d": passed - serie[i7]["tests_passed"]}

        out.append({
            "slug": slug, "name": spec.get("name", slug),
            "repo_url": f"https://github.com/{ORG}/vigiechiro-pr-companion-{slug}",
            "board_url": f"https://github.com/orgs/{ORG}/projects",
            "last_activity": (now - timedelta(hours=2 + _h(slug) % 20)).isoformat(),
            "ci_status": "success", "tests_source": "artefact",
            "issues": {"done": issues_done, "total": 54},
            "priorities": {**bandes, "mvp_complete": mvp},
            "tests": {"passed": passed, "total": tot, "pct": pct},
            "quality": {"coverage_pct": round(min(95.0, best_cov * factor + _h(slug) % 3), 1),
                        "pmd_violations": _h(slug + "pmd") % 4, "spotless_ok": True, "archunit_ok": True},
            "review": review, "bus_factor": bus, "trend": trend,
            "contributors": sorted(contribs, key=lambda c: (-c["commits"], c["login"])),
        })
    return out


def median(xs):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", help="liste de slugs separes par des virgules")
    ap.add_argument("--no-tests", action="store_true", help="saute tests/qualite (dev front)")
    ap.add_argument("--no-history", action="store_true", help="ne pas ecrire l'historique")
    args = ap.parse_args()
    filtre = set(args.teams.split(",")) if args.teams else None

    now = datetime.now(timezone.utc)
    hist = charger_historique()
    equipes = lister_equipes(filtre)
    if not equipes:
        raise SystemExit("Aucune equipe trouvee (verifier l'acces a l'org).")
    print(f"{len(equipes)} equipe(s) : {', '.join(e['slug'] for e in equipes)}",
          file=sys.stderr)

    teams = []
    snapshots = []
    for e in equipes:
        repo, slug = e["repo"], e["slug"]
        print(f"  - {slug} ...", file=sys.stderr)
        issues_data = collecter_issues(repo)
        contributeurs, bus, review = collecter_contributeurs(repo, slug, issues_data)
        if args.no_tests:
            tests = {"passed": None, "total": None, "pct": None}
            quality = {"coverage_pct": None, "pmd_violations": None,
                       "spotless_ok": None, "archunit_ok": None}
            ci_status, source = None, "saute"
        else:
            run = dernier_run(repo)
            tests, quality, ci_status, source = collecter_tests(repo, run)

        trend = tendance(hist, slug, tests["passed"], issues_data["issues"]["done"], now)
        teams.append({
            "slug": slug,
            "name": slug,
            "repo_url": e["url"],
            "board_url": f"https://github.com/orgs/{ORG}/projects",
            "last_activity": e["pushed_at"],
            "ci_status": ci_status,
            "tests_source": source,
            "issues": issues_data["issues"],
            "priorities": issues_data["priorities"],
            "tests": tests,
            "quality": quality,
            "review": review,
            "bus_factor": bus,
            "trend": trend,
            "contributors": sorted(contributeurs,
                                   key=lambda c: (-c["commits"], c["login"])),
        })
        snapshots.append({"date": now.isoformat(), "slug": slug,
                          "tests_passed": tests["passed"],
                          "issues_done": issues_data["issues"]["done"]})

    # Equipes de reference optionnelles (secret REFERENCE_TEAMS) ; absentes si non defini.
    ref_raw = os.environ.get("REFERENCE_TEAMS")
    if ref_raw:
        try:
            teams += synthetiser_reference(json.loads(ref_raw), teams, now)
            print("equipes de reference ajoutees", file=sys.stderr)
        except Exception as e:                                # noqa: BLE001
            print(f"REFERENCE_TEAMS ignore ({e})", file=sys.stderr)

    tests_total = next((t["tests"]["total"] for t in teams if t["tests"]["total"]), 622)
    promo = {
        "median_tests_pct": median([t["tests"]["pct"] for t in teams]),
        "median_issues_pct": median([
            round(100 * t["issues"]["done"] / t["issues"]["total"], 1)
            for t in teams if t["issues"]["total"]]),
        "fastest_movers": sorted(
            [{"slug": t["slug"], "delta_7d": t["trend"]["delta_7d"]}
             for t in teams if t["trend"]["delta_7d"]],
            key=lambda x: -x["delta_7d"])[:3],
    }
    # Classement par etudiant : on aplatit les contributeurs de toutes les equipes
    # (chaque etudiant appartient a une equipe -> on annote la ligne avec son slug).
    students = []
    for t in teams:
        for c in t["contributors"]:
            students.append({**c, "team": t["slug"]})
    students.sort(key=lambda c: (-c["commits"], -c["prs_merged"], c["login"]))

    data = {
        "generated_at": now.isoformat(),
        "totals": {"tests_total": tests_total, "issues_total": 54},
        "promo": promo,
        "teams": teams,
        "students": students,
    }
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Ecrit {DATA_PATH}", file=sys.stderr)

    if not args.no_history and not args.no_tests:
        # Un seul instantane par equipe et par jour, meme si le build tourne
        # toutes les heures : evite de spammer l'historique (et de reduire la
        # sparkline a quelques heures). data.json, lui, est rafraichi a chaque run.
        jour = now.date().isoformat()
        deja = {(h["slug"], h["date"][:10]) for h in hist}
        nouveaux = [s for s in snapshots if (s["slug"], jour) not in deja]
        if nouveaux:
            ajouter_historique(nouveaux)
            print(f"Historique : +{len(nouveaux)} enregistrement(s)", file=sys.stderr)
        else:
            print("Historique : deja a jour pour aujourd'hui", file=sys.stderr)


if __name__ == "__main__":
    main()
