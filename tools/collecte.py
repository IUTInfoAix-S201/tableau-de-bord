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
import json
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
    return bool(login) and login not in BOTS and not login.endswith("[bot]")


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
    """Serie + delta sur 7 jours pour une equipe."""
    pts = [h for h in hist if h["slug"] == slug]
    serie = [{"date": h["date"], "tests_passed": h["tests_passed"]} for h in pts][-30:]
    seuil = (now - timedelta(days=7)).isoformat()
    avant = [h for h in pts if h["date"] <= seuil]
    base = avant[-1]["tests_passed"] if avant else (pts[0]["tests_passed"] if pts else None)
    delta = (tests_passed - base) if (base is not None and tests_passed is not None) else None
    return {"tests_series": serie, "delta_7d": delta}


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
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
        ajouter_historique(snapshots)
        print(f"Historique : +{len(snapshots)} enregistrement(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
