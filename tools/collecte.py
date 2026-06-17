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
from zoneinfo import ZoneInfo

ORG = "IUTInfoAix-S201-2026"
REPO_PREFIX = "vigiechiro-pr-companion-"

# Date de mise a disposition des depots (debut du projet, AAAA-MM-JJ). Borne la
# longueur des courbes des equipes de reference (lievres) : elles ne doivent pas
# afficher d'historique anterieur au projet.
PROJET_DEBUT = "2026-06-04"
# Echeance de rendu (heure de Paris, UTC+2 en juin) : DOIT rester synchronisee
# avec FIN_PROJET dans site/app.js. Sert a reperer les commits postérieurs a la
# fin du projet (point de vigilance).
PROJET_FIN = "2026-06-18T08:15:00+02:00"

# Fuseau des etudiants : les diagrammes d'activite « par heure / par jour » sont
# exprimes en heure locale francaise (heure d'ete geree par la base tz).
PARIS = ZoneInfo("Europe/Paris")

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

# --- Chaine des 8 features a construire (ordre pipeline VigieChiro) ------------
# `sites` + `commun` sont fournis en reference ; ces 8-la sont le travail etudiant.
CHAINE = ["diagnostic", "lot", "bibliotheque", "validation",
          "importation", "qualification", "multisite", "passage"]
# Capture representative par feature (option A : lien vers le blob du repo prive).
# NB : `importation` -> fichiers prefixes `apercu-import-*` (et non `apercu-importation-`).
CAPTURE = {
    "diagnostic": "apercu-diagnostic.png",
    "lot": "apercu-lot-preparer.png",
    "bibliotheque": "apercu-bibliotheque-sons.png",
    "validation": "apercu-validation-revue.png",
    "importation": "apercu-import-assistant.png",
    "qualification": "apercu-qualification.png",
    "multisite": "apercu-multisite.png",
    "passage": "apercu-passage.png",
}

# --- Seuils du voyant qualite de revue (REGLABLES) ----------------------------
CORPS_SUBSTANTIEL = 80      # une revue avec un corps >= 80 car. compte comme vraie
SEUIL_VERT = 0.50           # part de revues substantielles pour le vert
SEUIL_ROUGE_TAMPON = 0.70   # part d'approbations vides pour le rouge

# Comptes a ignorer (bots)
BOTS = {"github-actions[bot]", "github-actions", "github-classroom[bot]", "github-classroom",
        "actions-user", "web-flow", "dependabot[bot]", "dependabot"}

# Comptes a exclure du tableau : encadrants/enseignants (pas des etudiants).
COMPTES_EXCLUS = {"nedseb"}


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


def gql(query, variables, tries=6):
    """Appel GraphQL via gh, robuste aux hoquets : reponse vide, JSON invalide,
    erreurs GraphQL transitoires, et enveloppe d'erreur SANS cle `data` (ex.
    secondary rate limit `{"message": "..."}`) qui faisait planter par KeyError.
    Retry avec backoff progressif ; message clair en cas d'echec final."""
    dernier = ""
    for i in range(tries):
        p = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=json.dumps({"query": query, "variables": variables}),
            capture_output=True,
            text=True,
        )
        out = p.stdout.strip()
        if out:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                dernier = out[:400]
            else:
                if isinstance(data, dict) and data.get("data") is not None and "errors" not in data:
                    return data["data"]
                # erreurs GraphQL OU enveloppe d'erreur REST (rate limit, abuse...)
                dernier = json.dumps(data.get("errors") or data.get("message") or data)[:400]
        else:
            dernier = (p.stderr or "").strip()[:400]
        if i < tries - 1:
            time.sleep(2 * (i + 1))   # backoff 2,4,6,8,10 s (aide sur secondary rate limit)
    raise SystemExit(f"ERREUR GraphQL (apres {tries} essais) : {dernier}")


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
    if low in {c.lower() for c in COMPTES_EXCLUS}:   # encadrants/enseignants
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
    par_feature = {f: {"done": 0, "total": 0} for f in CHAINE}
    for i in issues:
        feat = prefixe_feature(i["title"])
        prio = PRIORITE.get(feat)
        if prio in bandes:
            bandes[prio]["total"] += 1
            if i["closed"]:
                bandes[prio]["done"] += 1
        if feat in par_feature:
            par_feature[feat]["total"] += 1
            if i["closed"]:
                par_feature[feat]["done"] += 1
    mvp = bandes["must"]["total"] > 0 and bandes["must"]["done"] == bandes["must"]["total"]
    # suivi par assignee
    assignes = defaultdict(int)
    fermees_par = defaultdict(int)
    features_par_assigne = defaultdict(set)   # login -> {feature, ...} (issues assignees)
    for i in issues:
        feat = prefixe_feature(i["title"])
        for a in i["assignees"]:
            assignes[a] += 1
            if i["closed"]:
                fermees_par[a] += 1
            if feat in par_feature:
                features_par_assigne[a].add(feat)
    # Issues fermées avec leurs assignés (repli quand aucune PR ne les ferme).
    closed_assignees = {i["number"]: i["assignees"] for i in issues if i["closed"]}
    return {
        "issues": {"done": done, "total": len(issues)},
        "priorities": {**bandes, "mvp_complete": mvp},
        "par_feature": par_feature,
        "_assignes": dict(assignes),
        "_fermees_par": dict(fermees_par),
        "_closed_assignees": closed_assignees,
        "_features_assigne": {k: sorted(v) for k, v in features_par_assigne.items()},
    }


def captures_presentes(repo):
    """Noms des `apercu-*.png` commites dans .github/assets (set ; vide si absent)."""
    out = gh_json(f"repos/{ORG}/{repo}/contents/.github/assets?ref=main", jq=".[].name")
    if isinstance(out, str):
        out = [out]
    return {n for n in out if isinstance(n, str) and n.startswith("apercu-") and n.endswith(".png")}


def capture_vues_verte(repo):
    """Vrai si le dernier run `capture-vues` sur main est vert.

    Sinon la galerie d'apercus peut etre perimee (la capture est tout-ou-rien :
    une seule vue cassee fige tous les PNG) -> on n'affiche pas de lien.
    """
    out = gh_json(f"repos/{ORG}/{repo}/actions/workflows/capture-vues.yml/runs"
                  "?branch=main&per_page=1", jq=".workflow_runs[0].conclusion")
    return out == "success"


def _avancement(key, par_feature, tests_by_feature):
    """(done, total, source) : tests si la donnee existe pour cet ecran, sinon issues."""
    tbf = (tests_by_feature or {}).get(key)
    if tbf and tbf.get("total"):
        return tbf["passed"], tbf["total"], "tests"
    iss = par_feature.get(key, {"done": 0, "total": 0})
    return iss["done"], iss["total"], "issues"


def chaine_features(repo, repo_url, par_feature, tests_by_feature=None):
    """Frise par feature. Jauge = tests passes/total si dispo, sinon issues (repli).

    `complete` (vert + lien) = mesure primaire a 100 %. Le lien capture exige en
    plus que le PNG existe ET que `capture-vues` soit verte (galerie a jour). Les
    appels reseau ne sont faits que s'il y a au moins une feature terminee.
    """
    primaire = {k: _avancement(k, par_feature, tests_by_feature) for k in CHAINE}
    complets = {k for k, (d, t, _) in primaire.items() if t > 0 and d == t}
    caps = captures_presentes(repo) if complets else set()
    fraiche = capture_vues_verte(repo) if complets else False
    frise = []
    for key in CHAINE:
        done, total, source = primaire[key]
        complete = key in complets
        png = CAPTURE.get(key)
        url = (f"{repo_url}/blob/main/.github/assets/{png}"
               if complete and fraiche and png in caps else None)
        iss = par_feature.get(key, {"done": 0, "total": 0})
        tbf = (tests_by_feature or {}).get(key)
        frise.append({"key": key, "priority": PRIORITE.get(key),
                      "done": done, "total": total, "source": source,
                      "complete": complete, "capture_url": url,
                      "issues": {"done": iss["done"], "total": iss["total"]},
                      "tests": ({"passed": tbf["passed"], "total": tbf["total"]} if tbf else None)})
    return frise


# ------------------------------------------------------------------------------
# PR + revues + contributeurs (GraphQL)
# ------------------------------------------------------------------------------
PR_QUERY = """
query($owner:String!,$name:String!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequests(first:50, after:$cursor, states:[OPEN,MERGED,CLOSED]){
      pageInfo{hasNextPage endCursor}
      nodes{
        number state merged mergedAt title url additions deletions
        mergeCommit{oid}
        author{login}
        mergedBy{login}
        closingIssuesReferences(first:20){nodes{number}}
        reviews(first:50){nodes{author{login} state bodyText comments{totalCount}}}
      }
    }
  }
}
"""


# ------------------------------------------------------------------------------
# Activite : commits horodates (diagrammes par jour / jour de semaine / heure)
# ------------------------------------------------------------------------------
def collecter_activite(repo):
    """Commits horodates, toutes branches, dedoublonnes par SHA, bots exclus.

    Source : commits de `main` depuis le debut du projet + commits EN AVANCE des
    branches non mergees (compare main...branche), pour capter aussi le travail
    pas encore integre. Les dates REST (`commit.author.date`) portent l'offset de
    l'auteur ; l'agregation les convertit en Europe/Paris. Un commit sans auteur
    GitHub rattache (`login` vide, email mal configure) est garde pour le total
    collectif mais non attribuable a une personne. -> liste {sha, date, login|None}.
    """
    jq = '{sha, date: .commit.author.date, login: (.author.login // "")}'
    sources = gh_json(
        f"repos/{ORG}/{repo}/commits?sha=main&since={PROJET_DEBUT}T00:00:00Z&per_page=100",
        jq=f".[] | {jq}", paginate=True,
    )
    sources = sources if isinstance(sources, list) else ([sources] if sources else [])
    noms = gh_json(f"repos/{ORG}/{repo}/branches?per_page=100", jq=".[].name", paginate=True)
    if isinstance(noms, str):
        noms = [noms]
    for b in [x for x in (noms or []) if x not in ("main", "master")]:
        data = gh_json(
            f"repos/{ORG}/{repo}/compare/main...{quote(b, safe='')}",
            jq=f"[.commits[] | {jq}]",
        )
        if isinstance(data, list):
            sources.extend(data)
        elif isinstance(data, dict):
            sources.append(data)
    events, vus = [], set()
    for c in sources:
        if not isinstance(c, dict):
            continue
        sha, date = c.get("sha"), c.get("date")
        login = c.get("login") or None
        if not sha or not date or sha in vus:
            continue
        if login and not is_human(login):     # commit de bot (capture auto, etc.)
            continue
        vus.add(sha)
        events.append({"sha": sha, "date": date, "login": login})
    return events


def agreger_activite(events):
    """events -> {total, by_day{date:n}, by_weekday[7 Lun..Dim], by_hour[24]} (Paris)."""
    by_day = defaultdict(int)
    by_weekday = [0] * 7
    by_hour = [0] * 24
    for ev in events:
        dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(PARIS)
        by_day[dt.date().isoformat()] += 1
        by_weekday[dt.weekday()] += 1     # 0 = lundi
        by_hour[dt.hour] += 1
    return {"total": len(events), "by_day": dict(by_day),
            "by_weekday": by_weekday, "by_hour": by_hour}


def commits_apres_echeance(events, seuil_iso=PROJET_FIN):
    """Commits posterieurs a la fin du projet -> {count, last, authors}.

    `events` est deja filtre des bots et de l'enseignant (cf. collecter_activite),
    donc tout commit restant date d'apres l'echeance est un vrai travail tardif.
    `count` = nb de commits, `last` = date du plus recent (ISO), `authors` = logins
    distincts (hors commits sans auteur GitHub rattache). Sert au point de vigilance.
    """
    seuil = datetime.fromisoformat(seuil_iso)
    tardifs = [ev for ev in events
               if datetime.fromisoformat(ev["date"].replace("Z", "+00:00")) > seuil]
    if not tardifs:
        return {"count": 0, "last": None, "authors": []}
    last = max(ev["date"] for ev in tardifs)
    authors = sorted({ev["login"] for ev in tardifs if ev["login"]})
    return {"count": len(tardifs), "last": last, "authors": authors}


# ------------------------------------------------------------------------------
# CI : temps cumule, nombre de runs, taux d'echec (API actions/runs)
# ------------------------------------------------------------------------------
CI_ECHEC = {"failure", "timed_out", "startup_failure"}


def collecter_ci(repo):
    """Runs GitHub Actions depuis le debut du projet -> {minutes, runs, runs_failed}.

    `minutes` = somme des durees de run (updated_at - run_started_at), juste meme
    sur runner self-hosted (les « minutes facturees » y valent 0). `runs` = runs
    TERMINES (avec conclusion) ; `runs_failed` = conclusions d'echec (hors annule).
    """
    runs = gh_json(
        f"repos/{ORG}/{repo}/actions/runs?created=%3E%3D{PROJET_DEBUT}&per_page=100",
        jq=".workflow_runs[] | {conclusion, run_started_at, updated_at, created_at}",
        paginate=True,
    )
    runs = runs if isinstance(runs, list) else ([runs] if runs else [])
    secs = total = failed = 0
    for r in runs:
        if not isinstance(r, dict) or r.get("conclusion") is None:
            continue                      # en cours / en file : pas encore comptabilise
        total += 1
        if r["conclusion"] in CI_ECHEC:
            failed += 1
        deb, fin = r.get("run_started_at") or r.get("created_at"), r.get("updated_at")
        if deb and fin:
            d = (datetime.fromisoformat(fin.replace("Z", "+00:00"))
                 - datetime.fromisoformat(deb.replace("Z", "+00:00"))).total_seconds()
            if d > 0:
                secs += d
    return {"minutes": round(secs / 60), "runs": total, "runs_failed": failed}


def collecter_branches(repo, membres=None):
    """Travail EN COURS dans les branches non mergees.

    Renvoie (nb de branches en avance sur main, {login: commits en avance}).
    Les commits en avance sur `main` (compare main...branche) mesurent le travail
    pas encore merge (et meme pas encore en PR). Une branche deja mergee est en
    avance de 0 -> ni comptee, ni double-comptee.

    Si `membres` (set de logins) est fourni, on n'attribue qu'aux membres de
    l'equipe : un commit attribue a un non-membre vient quasi toujours d'un email
    git mal configure (ex. `youremail@example.com`) que GitHub rattache a un compte
    aleatoire -> contributeur fantome, ecarte. (Le decompte de branches, lui, reste
    complet : la branche est bien « en cours », meme si on ne peut pas crediter.)
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
            if (sha and sha not in vus and is_human(login)
                    and (membres is None or login in membres)):
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
    pr_liste = defaultdict(list)         # login -> [resume PR visible, ...] (ouvertes + mergees)
    lignes_ajout = defaultdict(int)      # login -> total additions (PR ouvertes + mergees)
    lignes_suppr = defaultdict(int)      # login -> total deletions (PR ouvertes + mergees)
    feats_pr = defaultdict(set)          # login -> {feature, ...} d'apres le prefixe du titre de PR
    revues_donnees = defaultdict(list)   # login -> [revue, ...]
    revues_recues = defaultdict(int)     # login (auteur PR) -> nb revues par pairs
    closes_par_auteur = defaultdict(set) # login -> {numeros d'issues fermees via « Closes #N »}
    # conformite revue (equipe)
    merged_total = 0
    merged_relues = 0
    self_merges = 0
    merged_info = []   # PR mergees : {number, sha, author, mergedAt} pour le delta de tests
    for pr in prs:
        auteur = (pr.get("author") or {}).get("login")
        # PR « visibles » (ouvertes en cours + mergees livrees) : on accumule le
        # detail par auteur (liste cliquable, lignes modifiees, feature du titre).
        # Les PR fermees sans merge (travail jete) sont ignorees.
        if is_human(auteur) and (pr["state"] == "OPEN" or pr.get("merged")):
            add = pr.get("additions") or 0
            sup = pr.get("deletions") or 0
            lignes_ajout[auteur] += add
            lignes_suppr[auteur] += sup
            pr_liste[auteur].append({
                "number": pr["number"],
                "title": pr.get("title") or "",
                "url": pr.get("url") or "",
                "state": pr["state"],
                "merged": bool(pr.get("merged")),
                "additions": add,
                "deletions": sup,
            })
            f = prefixe_feature(pr.get("title"))
            if f in CHAINE:
                feats_pr[auteur].add(f)
        if pr["state"] == "OPEN":
            if is_human(auteur):
                prs_open[auteur] += 1
        # PR mergees : on EXCLUT celles authored par un bot (ex. captures auto-mergees par
        # github-actions[bot]). Le bot self-merge sans revue par un pair : sans ce filtre, ces
        # PR gonfleraient merged_total / self_merges et plomberaient le voyant qualite de revue.
        if pr.get("merged") and is_human(auteur):
            merged_total += 1
            prs_merged[auteur] += 1
            # Issues fermees par cette PR (lien « Closes #N » resolu par GitHub) :
            # on credite l'AUTEUR de la PR, pas l'assigne de l'issue.
            for ref in ((pr.get("closingIssuesReferences") or {}).get("nodes") or []):
                if ref and ref.get("number"):
                    closes_par_auteur[auteur].add(ref["number"])
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

    # Travail EN COURS : commits en avance sur main dans les branches non mergees,
    # attribues aux seuls membres (evite les fantomes dus a un email git mal configure).
    branches_en_cours, wip = collecter_branches(repo, set(membres))

    logins = set(commits) | set(prs_open) | set(prs_merged) | set(revues_donnees) \
        | set(membres) | set(issues_data["_assignes"]) | set(wip)
    # Garde-fou global : on ne garde que des etudiants. is_human exclut bots ET
    # encadrants (COMPTES_EXCLUS, ex. nedseb) qui pouvaient se glisser via les
    # assignations d'issues (non filtrees a la collecte des issues).
    logins = {l for l in logins if is_human(l)}

    # Issues fermees : creditees a qui les a fermees via « Closes #N » dans une PR
    # mergee (closes_par_auteur, l'auteur de la PR) ; pour les issues fermees sans
    # PR liee (fermeture manuelle), repli sur l'assigne. -> {login: nb distinct}.
    closed_assignees = issues_data.get("_closed_assignees", {})
    fermees_via_pr, ferme_par = set(), defaultdict(set)
    for auteur, nums in closes_par_auteur.items():
        for n in nums:
            if n in closed_assignees:           # issue reellement fermee
                ferme_par[auteur].add(n)
                fermees_via_pr.add(n)
    for n, assignees in closed_assignees.items():
        if n not in fermees_via_pr:             # fermeture manuelle -> assigne(s)
            for a in assignees:
                if is_human(a):
                    ferme_par[a].add(n)

    feats_issues = issues_data.get("_features_assigne", {})
    contributeurs = []
    for login in sorted(logins):
        v = voyant_revue(revues_donnees.get(login, []))
        # Features touchees = union des issues assignees (prefixe [feature]) et des
        # titres de PR (prefixe [feature]), reordonnees selon la chaine pipeline.
        feats = set(feats_issues.get(login, [])) | feats_pr.get(login, set())
        contributeurs.append({
            "login": login,
            "commits": commits.get(login, 0),
            "branch_commits": wip.get(login, 0),
            "prs_open": prs_open.get(login, 0),
            "prs_merged": prs_merged.get(login, 0),
            "lines_added": lignes_ajout.get(login, 0),
            "lines_deleted": lignes_suppr.get(login, 0),
            "reviews_given": v["reviews_total"],
            "reviews_received": revues_recues.get(login, 0),
            "issues_assigned": issues_data["_assignes"].get(login, 0),
            "issues_closed": len(ferme_par.get(login, set())),
            "tests_validated": 0,   # rempli apres coup (delta de tests par PR mergee)
            "features": [f for f in CHAINE if f in feats],
            "prs": sorted(pr_liste.get(login, []), key=lambda p: -p["number"]),
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
    # Robustesse : sous rate-limit / hoquet API, gh_json peut renvoyer une chaine
    # (message d'erreur non-JSON) au lieu de dicts -> on filtre les non-dicts pour
    # ne pas planter tout le build sur un `a["name"]`.
    cible = next((a for a in arts if isinstance(a, dict) and a.get("name") == "ci-summary"), None)
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
    """Renvoie (tests_dict, quality_dict, ci_status, source, tests_by_feature)."""
    ci_status = run.get("conclusion")
    run_id = run.get("id")
    quality = {"coverage_pct": None, "pmd_violations": None,
               "spotless_ok": None, "archunit_ok": None}
    tbf = None
    if not run_id:
        return ({"passed": None, "total": None, "pct": None}, quality, ci_status, "aucun-run", tbf)

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
        tbf = summary.get("tests_by_feature")
        source = "artefact"
    else:
        parsed = parser_logs(repo, run_id)
        if not parsed:
            return ({"passed": None, "total": None, "pct": None}, quality, ci_status, "indisponible", tbf)
        tests = {"passed": parsed["passed"], "total": parsed["total"]}
        source = "logs"

    if tests["total"]:
        tests["pct"] = round(100 * tests["passed"] / tests["total"], 1)
    else:
        tests["pct"] = None
    return tests, quality, ci_status, source, tbf


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
        if not isinstance(r, dict) or "id" not in r:   # hoquet API -> non-dict
            continue
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


def repartir(acc, delta, auteurs):
    """Repartit `delta` tests entre `auteurs` (liste, doublons = plusieurs PR) a
    parts egales PAR PR. Accumulation en float (arrondie en fin de calcul). Ne fait
    rien si delta <= 0 (les regressions ne creditent personne)."""
    if not auteurs or delta <= 0:
        return
    part = delta / len(auteurs)
    for a in auteurs:
        acc[a] += part


def attribuer_tests_pr(repo, merged_info, baseline, cache_team, live_total=None):
    """Delta de tests verts par PR mergee, attribue a l'auteur de la PR.

    Pour chaque PR mergee (ordre de merge) : delta = tests(apres) - tests(avant).
    Le nb de tests au commit de merge est mis en CACHE (immuable une fois mergee) :
    le cout par build ne porte que sur les nouvelles PR.

    Robustesse aux comptes manquants (`passed=None`, typiquement un run CI annule
    par `cancel-in-progress` sur des merges rapproches). On ne « saute » plus ces
    PR en versant tout leur delta a la PR mesuree suivante (ce qui creditait le
    MAUVAIS auteur, cf. bug remonte par une etudiante). On accumule les auteurs des
    PR `None` dans un « trou » ; en atteignant la PR mesuree suivante, on REPARTIT
    le delta du trou entre tous ses auteurs (PR `None` + PR mesuree), au prorata du
    nombre de PR. Les PR `None` de fin de file sont creditees via `live_total` (le
    total courant de l'equipe) comme point final. -> {login: total}.
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

    valides = defaultdict(float)
    prev = baseline
    trou = []                      # auteurs des PR None depuis le dernier point mesure
    for m in merged:
        info = cache_team[str(m["number"])]
        p, author = info.get("passed"), info.get("author")
        if p is None:
            if author:
                trou.append(author)        # en attente d'un point mesure pour crediter
            continue
        beneficiaires = trou + ([author] if author else [])
        if prev is not None:
            repartir(valides, p - prev, beneficiaires)
        prev = p
        trou = []
    # PR None de fin de file : creditees via le total live comme point final.
    if trou and prev is not None and live_total is not None:
        repartir(valides, live_total - prev, trou)
    return {a: int(round(v)) for a, v in valides.items() if round(v) > 0}


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

        # Champs detail (vue etudiant) : valeurs plausibles pour les leurres. Lignes
        # derivees des commits ; pas de liste de PR cliquable ni de feature reelles.
        for c in contribs:
            c.setdefault("lines_added", c["commits"] * (12 + _h(c["login"] + "la") % 40))
            c.setdefault("lines_deleted", c["commits"] * (3 + _h(c["login"] + "ld") % 12))
            c.setdefault("features", [])
            c.setdefault("prs", [])

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
    ap.add_argument("--no-activity", action="store_true",
                    help="saute la collecte des commits horodates (diagrammes d'activite)")
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
    activite_par_equipe = {}   # slug -> [events] (commits horodates) ; vide si --no-activity
    ci_par_equipe = {}         # slug -> {minutes, runs, runs_failed} ; vide si --no-activity
    for e in equipes:
        repo, slug = e["repo"], e["slug"]
        print(f"  - {slug} ...", file=sys.stderr)
        issues_data = collecter_issues(repo)
        activite_par_equipe[slug] = [] if args.no_activity else collecter_activite(repo)
        ci_par_equipe[slug] = {} if args.no_activity else collecter_ci(repo)
        contributeurs, bus, review, merged_info, open_branches = \
            collecter_contributeurs(repo, slug, issues_data)
        if args.no_tests:
            tests = {"passed": None, "total": None, "pct": None}
            quality = {"coverage_pct": None, "pmd_violations": None,
                       "spotless_ok": None, "archunit_ok": None}
            ci_status, source, tbf = None, "saute", None
        else:
            run = dernier_run(repo)
            tests, quality, ci_status, source, tbf = collecter_tests(repo, run)
            # Filet anti-hoquet : si le fetch echoue, reutiliser la derniere valeur
            # connue (evite un « n/d » transitoire qui casserait tout le tableau).
            if tests["passed"] is None and slug in last_tests:
                lt = last_tests[slug]
                tests, quality = lt["tests"], lt["quality"]
                tbf = lt.get("tests_by_feature")
                ci_status, source = lt.get("ci_status"), "cache"
            elif tests["passed"] is not None:
                last_tests[slug] = {"tests": tests, "quality": quality,
                                    "ci_status": ci_status, "tests_by_feature": tbf}

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
            "features": chaine_features(repo, e["url"], issues_data["par_feature"], tbf),
            "open_branches": open_branches,
            "late_commits": commits_apres_echeance(activite_par_equipe[slug]),
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
    # Plancher = la plus petite valeur JAMAIS vue (courante + tout l'historique),
    # pas le min courant entre equipes : sinon, des que la derniere equipe progresse,
    # le baseline remonte au-dessus du vrai depart et la sparkline plonge artificiellement
    # (backfill trop haut -> faux creux uniforme) et les deltas sont sous-estimes.
    passes = [t["tests"]["passed"] for t in teams if t["tests"]["passed"] is not None]
    hist_passes = [h["tests_passed"] for h in hist if h.get("tests_passed") is not None]
    baseline = min(passes + hist_passes) if (passes or hist_passes) else None
    for t in teams:
        t["trend"] = tendance(hist, t["slug"], t["tests"]["passed"],
                              t["issues"]["done"], now, baseline)

    # Tests valides par contributeur : delta de tests verts par PR mergee (cache).
    if not args.no_tests:
        cache = charger_cache_pr()
        for t in teams:
            ct = cache.setdefault(t["slug"], {})
            tv = attribuer_tests_pr(t["_repo"], t["_merged_prs"], baseline, ct,
                                    live_total=t["tests"]["passed"])
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

    # Activite : agregats des commits horodates, en collectif + par equipe + par
    # personne (la declinaison par equipe/personne est « gratuite » une fois les
    # evenements collectes ; le front l'exploitera par phases).
    tous_events, par_login, team_de_login = [], defaultdict(list), {}
    for slug, evs in activite_par_equipe.items():
        tous_events.extend(evs)
        for ev in evs:
            if ev["login"]:
                par_login[ev["login"]].append(ev)
                team_de_login[ev["login"]] = slug
    collectif = agreger_activite(tous_events)
    jours = sorted(collectif["by_day"])
    activity = {
        **collectif,
        "first_day": jours[0] if jours else None,
        "last_day": jours[-1] if jours else None,
        "by_team": {slug: agreger_activite(evs) for slug, evs in activite_par_equipe.items()},
        "by_student": {login: {**agreger_activite(evs), "team": team_de_login[login]}
                       for login, evs in par_login.items()},
    }

    # CI : agregats collectifs + par equipe (somme des durees de runs, etc.).
    ci = {
        "minutes": sum(c.get("minutes", 0) for c in ci_par_equipe.values()),
        "runs": sum(c.get("runs", 0) for c in ci_par_equipe.values()),
        "runs_failed": sum(c.get("runs_failed", 0) for c in ci_par_equipe.values()),
        "by_team": ci_par_equipe,
    }

    data = {
        "generated_at": now.isoformat(),
        "totals": {"tests_total": tests_total, "issues_total": 54},
        "promo": promo,
        "teams": teams,
        "students": students,
        "activity": activity,
        "ci": ci,
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
