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
from urllib.parse import quote

ORG = "IUTInfoAix-S201-2026"
REPO_PREFIX = "vigiechiro-pr-companion-"

# Date de mise a disposition des depots (debut du projet, AAAA-MM-JJ). Borne la
# longueur des courbes des equipes de reference (lievres) : elles ne doivent pas
# afficher d'historique anterieur au projet.
PROJET_DEBUT = "2026-06-04"

# Repertoires (relatifs a la racine du repo tableau-de-bord)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "site", "data.json")
HISTORY_PATH = os.path.join(ROOT, "history", "history.jsonl")
# Cache des comptes de tests par PR mergee (delta de tests par contributeur).
# Immuable une fois une PR mergee -> on ne recalcule que les nouvelles.
CACHE_PR_PATH = os.path.join(ROOT, "history", "pr-tests.json")
# Derniere valeur connue des tests/qualite par equipe : reutilisee si un fetch
# echoue (hoquet API), pour ne pas afficher « n/d » de facon transitoire.
CACHE_TESTS_PATH = os.path.join(ROOT, "history", "last-tests.json")

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
BOTS = {"github-actions[bot]", "github-actions", "github-classroom[bot]", "github-classroom",
        "actions-user", "web-flow", "dependabot[bot]", "dependabot"}


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
    low = login.lower()
    if low.endswith("[bot]") or low in {b.lower() for b in BOTS}:
        return False
    # Certains bots arrivent SANS le suffixe [bot] selon l'API (GraphQL renvoie p.ex.
    # `github-actions` au lieu de `github-actions[bot]`) : on filtre par sous-chaine
    # (github-actions, dependabot, relecteur Copilot active par le ruleset).
    if any(k in low for k in ("github-actions", "dependabot", "copilot")):
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
        number state merged mergedAt
        mergeCommit{oid}
        author{login}
        mergedBy{login}
        reviews(first:50){nodes{author{login} state bodyText comments{totalCount}}}
      }
    }
  }
}
"""


def collecter_branches(repo):
    """Travail EN COURS dans les branches non mergees.

    Renvoie (nb de branches en avance sur main, {login: commits en avance}).
    Les commits en avance sur `main` (compare main...branche) mesurent le travail
    pas encore merge (et meme pas encore en PR). Une branche deja mergee est en
    avance de 0 -> ni comptee, ni double-comptee.
    """
    noms = gh_json(f"repos/{ORG}/{repo}/branches?per_page=100", jq=".[].name", paginate=True)
    if isinstance(noms, str):
        noms = [noms]
    features = [b for b in noms if b not in ("main", "master")]
    vus, par_login, en_cours = set(), defaultdict(int), 0
    for b in features:
        data = gh_json(
            f"repos/{ORG}/{repo}/compare/main...{quote(b, safe='')}",
            jq='{ahead: .ahead_by, commits: [.commits[] | {sha, login: (.author.login // "")}]}',
        )
        if not isinstance(data, dict):
            continue
        if (data.get("ahead") or 0) > 0:
            en_cours += 1
        for c in data.get("commits") or []:
            sha, login = c.get("sha"), c.get("login")
            if sha and sha not in vus and is_human(login):
                vus.add(sha)
                par_login[login] += 1
    return en_cours, dict(par_login)


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
    merged_info = []   # PR mergees : {number, sha, author, mergedAt} pour le delta de tests
    for pr in prs:
        auteur = (pr.get("author") or {}).get("login")
        if pr["state"] == "OPEN":
            if is_human(auteur):
                prs_open[auteur] += 1
        # PR mergees : on EXCLUT celles authored par un bot (ex. captures auto-mergees par
        # github-actions[bot]). Le bot self-merge sans revue par un pair : sans ce filtre, ces
        # PR gonfleraient merged_total / self_merges et plomberaient le voyant qualite de revue.
        if pr.get("merged") and is_human(auteur):
            merged_total += 1
            prs_merged[auteur] += 1
            sha = (pr.get("mergeCommit") or {}).get("oid")
            if sha:
                merged_info.append({"number": pr["number"], "sha": sha,
                                    "author": auteur, "mergedAt": pr.get("mergedAt")})
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

    # Travail EN COURS : commits en avance sur main dans les branches non mergees.
    branches_en_cours, wip = collecter_branches(repo)

    logins = set(commits) | set(prs_open) | set(prs_merged) | set(revues_donnees) \
        | set(membres) | set(issues_data["_assignes"]) | set(wip)
    contributeurs = []
    for login in sorted(logins):
        v = voyant_revue(revues_donnees.get(login, []))
        contributeurs.append({
            "login": login,
            "commits": commits.get(login, 0),
            "branch_commits": wip.get(login, 0),
            "prs_open": prs_open.get(login, 0),
            "prs_merged": prs_merged.get(login, 0),
            "reviews_given": v["reviews_total"],
            "reviews_received": revues_recues.get(login, 0),
            "issues_assigned": issues_data["_assignes"].get(login, 0),
            "issues_closed": issues_data["_fermees_par"].get(login, 0),
            "tests_validated": 0,   # rempli apres coup (delta de tests par PR mergee)
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
    return contributeurs, bus, review, merged_info, branches_en_cours


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
# Tests valides par contributeur : delta de tests verts par PR mergee
# ------------------------------------------------------------------------------
def compte_tests_sha(repo, sha):
    """Nb de tests verts du run maven.yml dont head_sha == sha (artefact ou logs)."""
    runs = gh_json(
        f"repos/{ORG}/{repo}/actions/workflows/maven.yml/runs?head_sha={sha}&per_page=5",
        jq=".workflow_runs[] | {id, conclusion}",
    )
    runs = runs if isinstance(runs, list) else ([runs] if runs else [])
    for r in runs:
        summary = lire_ci_summary(repo, r["id"])
        if summary and summary.get("tests") and summary["tests"].get("passed") is not None:
            return summary["tests"]["passed"]
        parsed = parser_logs(repo, r["id"])
        if parsed:
            return parsed["passed"]
    return None


def charger_cache_pr():
    if not os.path.exists(CACHE_PR_PATH):
        return {}
    try:
        with open(CACHE_PR_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def sauver_cache_pr(cache):
    os.makedirs(os.path.dirname(CACHE_PR_PATH), exist_ok=True)
    with open(CACHE_PR_PATH, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


def charger_last_tests():
    if not os.path.exists(CACHE_TESTS_PATH):
        return {}
    try:
        with open(CACHE_TESTS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def sauver_last_tests(d):
    os.makedirs(os.path.dirname(CACHE_TESTS_PATH), exist_ok=True)
    with open(CACHE_TESTS_PATH, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)


def attribuer_tests_pr(repo, merged_info, baseline, cache_team):
    """Delta de tests verts par PR mergee, attribue a l'auteur de la PR.

    Pour chaque PR mergee (ordre de merge) : delta = tests(apres) - tests(avant) ;
    avant = la PR precedente (ou `baseline` pour la 1re). Seuls les deltas positifs
    comptent. Le nb de tests au commit de merge est mis en CACHE (immuable une fois
    mergee) : le cout par build ne porte que sur les nouvelles PR. -> {login: total}.
    """
    merged = sorted([m for m in merged_info if m.get("sha")],
                    key=lambda m: m.get("mergedAt") or "")
    for m in merged:
        key = str(m["number"])
        entry = cache_team.get(key)
        if not entry or entry.get("passed") is None:
            cache_team[key] = {"sha": m["sha"], "author": m["author"],
                               "passed": compte_tests_sha(repo, m["sha"])}
        else:
            cache_team[key]["author"] = m["author"]
    valides = defaultdict(int)
    prev = baseline
    for m in merged:
        info = cache_team[str(m["number"])]
        p, author = info.get("passed"), info.get("author")
        if p is None:
            continue                      # compte indisponible -> on saute
        if prev is not None and author and p > prev:
            valides[author] += p - prev
        prev = p
    return dict(valides)


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


def tendance(hist, slug, tests_passed, issues_done, now, baseline=None):
    """Serie (fenetre 10 jours, duree du projet) + delta depuis le debut.

    Le tableau ne mesure que depuis sa mise en service ; pour qu'une equipe ait une
    sparkline des aujourd'hui, on PREFIXE les jours ecoules depuis PROJET_DEBUT avec
    la valeur de base (`baseline` = nb de tests verts de la version etudiante de
    depart). Tant qu'il n'y a pas de progres, la courbe est plate (honnete).
    """
    pts = [h for h in hist if h["slug"] == slug]
    serie = [{"date": h["date"], "tests_passed": h["tests_passed"]} for h in pts]
    jour = now.date().isoformat()
    # Le point du jour reflete la valeur COURANTE (l'instantane d'historique du
    # matin est fige par la dedup journaliere ; la sparkline, elle, suit le live).
    if tests_passed is not None:
        if serie and serie[-1]["date"][:10] == jour:
            serie[-1] = {"date": now.isoformat(), "tests_passed": tests_passed}
        else:
            serie.append({"date": now.isoformat(), "tests_passed": tests_passed})

    # Backfill des jours du projet anterieurs au premier point mesure.
    if baseline is not None:
        try:
            d = datetime.fromisoformat(PROJET_DEBUT).date()
        except ValueError:
            d = None
        if d is not None:
            premiere = serie[0]["date"][:10] if serie else jour
            prefix = []
            while d.isoformat() < premiere and (now.date() - d).days <= 9:
                prefix.append({"date": d.isoformat() + "T00:00:00+00:00",
                               "tests_passed": baseline})
                d = d + timedelta(days=1)
            serie = prefix + serie

    serie = serie[-10:]
    base = serie[0]["tests_passed"] if serie else None
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


def synthetiser_reference(specs, teams_reels, now, plafond_tv=None):
    """Construit les objets equipe de reference a partir des specs (login + above).

    `plafond_tv` : si fourni, les `tests_validated` des membres des lievres sont cales
    JUSTE en dessous de cette valeur (le meilleur etudiant reel) -> les lievres restent
    devant au niveau equipe mais derriere le meilleur individu reel dans le classement.
    """
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
        members = spec["members"]
        actifs = [m for m in members if not m.get("freerider")]
        # Avance DECELERANTE : la lievre progresse mais par increments de plus en plus
        # petits (courbe concave 1-exp(-age/tau)). Plafonnee a (leader_reel-1) par
        # membre, donc le meilleur etudiant reste TOUJOURS un vrai etudiant, et le
        # gain d'equipe = somme des tests des membres (coherent).
        try:
            age = max(0, (now.date() - datetime.fromisoformat(PROJET_DEBUT).date()).days)
        except ValueError:
            age = 0
        plateau = round(7 * (1 + float(spec.get("above", 0.25))))
        tau = 3.0
        plaf = max(0, (plafond_tv or 0) - 1)
        gain = min(round(plateau * (1 - math.exp(-age / tau))), plaf * len(actifs))
        tv_act = _repartir(gain, [1] * len(actifs))   # reparti ~egalement -> chacun <= leader-1
        prm_act = [1 if tv > 0 else 0 for tv in tv_act]
        issues_done = sum(prm_act)
        passed = min(tot, base + gain)
        pct = round(100 * passed / tot, 1) if tot else None

        poids = [_h(slug + m["login"]) % 5 + 2 for m in actifs]
        commits_act = _repartir(max(len(actifs), gain * 2), poids)
        rev_act = _repartir(issues_done, list(reversed(poids)))   # revues != auteurs
        # Revues RECUES = ces memes revues, reparties sur les auteurs -> recu == donne.
        rev_received = _repartir(sum(rev_act), prm_act) if sum(prm_act) > 0 else [0] * len(actifs)
        idx = {m["login"]: i for i, m in enumerate(actifs)}

        contribs = []
        for m in members:
            login = m["login"]
            if m.get("freerider"):
                contribs.append({"login": login, "commits": 0, "branch_commits": 0,
                                 "prs_open": 0, "prs_merged": 0,
                                 "reviews_given": 0, "reviews_received": 0, "issues_assigned": 1,
                                 "issues_closed": 0, "tests_validated": 0, "reviews_total": 0,
                                 "inline_comments": 0, "changes_requested": 0, "empty_approvals": 0,
                                 "review_quality": "na"})
                continue
            i = idx[login]
            cm, pm, rg = commits_act[i], prm_act[i], rev_act[i]
            if rg > 0:
                inl, chg, qual = rg * 2 + _h(login) % 3, max(1, rg // 3), "green"
            else:
                inl, chg, qual = 0, 0, ("yellow" if cm > 0 else "na")
            contribs.append({"login": login, "commits": cm, "branch_commits": _h(login + "wip") % 5,
                             "prs_open": _h(login + "po") % 2,
                             "prs_merged": pm, "reviews_given": rg, "reviews_received": rev_received[i],
                             "issues_assigned": pm + _h(login + "ia") % 2, "issues_closed": pm,
                             "tests_validated": tv_act[i],
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

        # Sparkline : la meme courbe DECELERANTE, 1 point/jour depuis le debut du
        # projet (borne a 10 jours). Le dernier point vaut `passed`.
        n = max(1, min(10, age + 1))
        serie = []
        for i in range(n):
            elapsed = max(0, age - (n - 1 - i))
            g = min(round(plateau * (1 - math.exp(-elapsed / tau))), plaf * len(actifs))
            serie.append({"date": (now - timedelta(days=n - 1 - i)).isoformat(),
                          "tests_passed": base + g})
        i7 = max(0, n - 1 - 7)
        trend = {"tests_series": serie, "delta_7d": passed - serie[i7]["tests_passed"]}

        out.append({
            "slug": slug, "name": spec.get("name", slug),
            "repo_url": f"https://github.com/{ORG}/vigiechiro-pr-companion-{slug}",
            "board_url": f"https://github.com/orgs/{ORG}/projects",
            "last_activity": (now - timedelta(hours=2 + _h(slug) % 20)).isoformat(),
            "ci_status": "success", "tests_source": "artefact",
            "issues": {"done": issues_done, "total": 54},
            "open_branches": sum(c["prs_open"] for c in contribs) + _h(slug + "br") % 3,
            "priorities": {**bandes, "mvp_complete": mvp},
            "tests": {"passed": passed, "total": tot, "pct": pct},
            "quality": {"coverage_pct": round(min(95.0, best_cov + gain + _h(slug) % 3), 1),
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
    last_tests = charger_last_tests()   # filet : derniere valeur connue par equipe
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
        contributeurs, bus, review, merged_info, open_branches = \
            collecter_contributeurs(repo, slug, issues_data)
        if args.no_tests:
            tests = {"passed": None, "total": None, "pct": None}
            quality = {"coverage_pct": None, "pmd_violations": None,
                       "spotless_ok": None, "archunit_ok": None}
            ci_status, source = None, "saute"
        else:
            run = dernier_run(repo)
            tests, quality, ci_status, source = collecter_tests(repo, run)
            # Filet anti-hoquet : si le fetch echoue, reutiliser la derniere valeur
            # connue (evite un « n/d » transitoire qui casserait tout le tableau).
            if tests["passed"] is None and slug in last_tests:
                lt = last_tests[slug]
                tests, quality = lt["tests"], lt["quality"]
                ci_status, source = lt.get("ci_status"), "cache"
            elif tests["passed"] is not None:
                last_tests[slug] = {"tests": tests, "quality": quality, "ci_status": ci_status}

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
            "open_branches": open_branches,
            "tests": tests,
            "quality": quality,
            "review": review,
            "bus_factor": bus,
            "trend": None,
            "_repo": repo,
            "_merged_prs": merged_info,
            "contributors": sorted(contributeurs,
                                   key=lambda c: (-c["commits"], c["login"])),
        })
        snapshots.append({"date": now.isoformat(), "slug": slug,
                          "tests_passed": tests["passed"],
                          "issues_done": issues_data["issues"]["done"]})

    # Tendances des vraies equipes : on connait maintenant le plancher de la promo
    # (= version etudiante de depart) pour backfiller les courbes depuis le debut.
    passes = [t["tests"]["passed"] for t in teams if t["tests"]["passed"] is not None]
    baseline = min(passes) if passes else None
    for t in teams:
        t["trend"] = tendance(hist, t["slug"], t["tests"]["passed"],
                              t["issues"]["done"], now, baseline)

    # Tests valides par contributeur : delta de tests verts par PR mergee (cache).
    if not args.no_tests:
        cache = charger_cache_pr()
        for t in teams:
            ct = cache.setdefault(t["slug"], {})
            tv = attribuer_tests_pr(t["_repo"], t["_merged_prs"], baseline, ct)
            for c in t["contributors"]:
                c["tests_validated"] = tv.get(c["login"], 0)
        sauver_cache_pr(cache)
        sauver_last_tests(last_tests)
    for t in teams:                       # nettoie les champs temporaires
        t.pop("_repo", None)
        t.pop("_merged_prs", None)

    # Equipes de reference optionnelles (secret REFERENCE_TEAMS) ; absentes si non defini.
    # On cale leurs `tests_validated` juste derriere le meilleur etudiant reel.
    top_real_tv = max((c["tests_validated"] for t in teams for c in t["contributors"]),
                      default=0)
    ref_raw = os.environ.get("REFERENCE_TEAMS")
    if ref_raw:
        try:
            teams += synthetiser_reference(json.loads(ref_raw), teams, now, top_real_tv)
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
    students.sort(key=lambda c: (-c["tests_validated"], -c["prs_merged"], -c["issues_closed"],
                                 -c["branch_commits"], -c["commits"], c["login"]))

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
