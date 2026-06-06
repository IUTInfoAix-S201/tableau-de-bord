# Tableau de bord SAE 2.01 VigieChiro

Leaderboard public d'avancement des equipes de la SAE 2.01 (R2.02 + R2.03).
Page statique publiee sur **GitHub Pages**, alimentee par un collecteur qui agrege
les metriques des depots d'equipe prives `vigiechiro-pr-companion-<equipe>`
(org Classroom `IUTInfoAix-S201-2026`).

➡️ **URL publique** : https://iutinfoaix-s201.github.io/tableau-de-bord/

## Ce qui est affiche

Par equipe :
- **tests verts** sur ~620 (la *Definition of Done* du sujet : test d'acceptation au vert) ;
- **issues faites** sur 54, avec ventilation **MUST / SHOULD / COULD** et un marqueur **MVP** (fil rouge) ;
- **% de PR mergees relues par un pair** (+ self-merges) ;
- **qualite de code** issue de la CI : couverture JaCoCo, violations PMD, Spotless, ArchUnit (MVVM) ;
- **tendance** (sparkline + « +N / 7j ») ;
- detail **par contributeur** (login GitHub) : commits, PR ouvertes/mergees, revues donnees/recues,
  suivi des issues, et un **voyant qualite de revue** (🟢 vraies revues / 🟡 leger / 🔴 tampon / ⚪ aucune).

> Les voyants et indicateurs collaboratifs sont des **reperes heuristiques, indicatifs et non des notes**.
> Les chiffres bruts sont toujours affiches pour permettre l'interpretation.

## Architecture

```
tools/collecte.py   -> interroge GitHub (gh) et ecrit site/data.json + history/history.jsonl
site/               -> page statique (index.html + style.css + app.js + data.json genere)
history/            -> instantanes journaliers (persistes via le cache Actions) pour les tendances
.github/workflows/build-dashboard.yml -> cron quotidien + manuel : collecte -> Pages
```

Les depots d'equipe sont **prives** : impossible d'interroger GitHub depuis le navigateur.
Les donnees sont donc **agregees cote serveur** (workflow) avec un token d'organisation, puis
seuls des agregats (+ logins publics) sont publies.

## Mise en place (une fois)

1. **Secret `DASHBOARD_TOKEN`** (Settings > Secrets and variables > Actions) :
   un **PAT fine-grained** avec acces en **lecture** a l'org `IUTInfoAix-S201-2026` :
   - Repository permissions : *Contents* (read), *Issues* (read), *Pull requests* (read),
     *Actions* (read), *Metadata* (read) ;
   - Organization permissions : *Members* (read).
   Le `GITHUB_TOKEN` par defaut ne suffit pas (depots dans une **autre** org).
2. **GitHub Pages** : Settings > Pages > *Build and deployment* > **Source : GitHub Actions**.
3. Lancer le workflow **Build dashboard** une premiere fois (onglet Actions > Run workflow).

## Metrique « tests + qualite » : instrumentation de la CI des equipes

Le collecteur lit en priorite l'artefact **`ci-summary`** publie par la CI `maven.yml` de chaque
equipe (tests surefire + couverture + PMD + portes Spotless/ArchUnit). Cette instrumentation est
ajoutee dans le **meta-depot** `IUTInfoAix-S201/vigiechiro-pr-companion` (branche `solution`) puis
propagee aux forks via `classroom-sync` (`student_syncs`, entree SAE).

Tant qu'une equipe n'a pas rejoue sa CI avec la version instrumentee, le collecteur **se rabat**
sur le parsing des logs du dernier run (nombre de tests uniquement ; la qualite reste « n/d »).

## Lancer en local

```bash
GH_TOKEN=$(gh auth token) python3 tools/collecte.py        # genere site/data.json
python3 -m http.server --directory site                    # http://localhost:8000
```

Options : `--teams a,b` (limiter), `--no-tests` (saute tests/qualite, plus rapide),
`--no-history` (n'ecrit pas l'historique).

## Reglages

Les seuils du voyant qualite de revue sont des constantes en tete de `tools/collecte.py`
(`CORPS_SUBSTANTIEL`, `SEUIL_VERT`, `SEUIL_ROUGE_TAMPON`). Le mapping feature -> priorite
(`PRIORITE`) suit la table du brief « Travail a faire ».

## Diffusion

Ajouter le lien du tableau de bord au site brief (section « Suivre votre avancement »)
et/ou aux README d'equipe.
