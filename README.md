# Tableau de bord SAÉ 2.01 VigieChiro

Leaderboard public d'avancement des équipes de la SAÉ 2.01 (R2.02 + R2.03).
Page statique publiée sur **GitHub Pages**, alimentée par un collecteur qui agrège
les métriques des dépôts d'équipe privés `vigiechiro-pr-companion-<équipe>`
(org Classroom `IUTInfoAix-S201-2026`).

➡️ **URL publique** : https://iutinfoaix-s201.github.io/tableau-de-bord/

## Ce qui est affiché

Par équipe :
- **tests verts** sur ~620 (la *Definition of Done* du sujet : test d'acceptation au vert) ;
- **issues faites** sur 54, avec ventilation **MUST / SHOULD / COULD** et un marqueur **MVP** (fil rouge) ;
- **% de PR mergées relues par un pair** (+ self-merges) ;
- **qualité de code** issue de la CI : couverture JaCoCo, violations PMD, Spotless, ArchUnit (MVVM) ;
- **tendance** (sparkline + « +N / 7j ») ;
- détail **par contributeur** (login GitHub) : commits, PR ouvertes/mergées, revues données/reçues,
  suivi des issues, et un **voyant qualité de revue** (🟢 vraies revues / 🟡 léger / 🔴 tampon / ⚪ aucune).

> Les voyants et indicateurs collaboratifs sont des **repères heuristiques, indicatifs et non des notes**.
> Les chiffres bruts sont toujours affichés pour permettre l'interprétation.

## Architecture

```
tools/collecte.py   -> interroge GitHub (gh) et écrit site/data.json + history/history.jsonl
site/               -> page statique (index.html + style.css + app.js + data.json généré)
history/            -> instantanés journaliers (commit automatique du bot) pour les tendances
.github/workflows/build-dashboard.yml -> cron quotidien + manuel : collecte -> Pages
```

Les dépôts d'équipe sont **privés** : impossible d'interroger GitHub depuis le navigateur.
Les données sont donc **agrégées côté serveur** (workflow) avec un token d'organisation, puis
seuls des agrégats (+ logins publics) sont publiés.

## Mise en place (une fois)

1. **Secret `DASHBOARD_TOKEN`** (Settings > Secrets and variables > Actions) :
   un **PAT fine-grained** avec accès en **lecture** à l'org `IUTInfoAix-S201-2026` :
   - Repository permissions : *Contents* (read), *Issues* (read), *Pull requests* (read),
     *Actions* (read), *Metadata* (read) ;
   - Organization permissions : *Members* (read).
   Le `GITHUB_TOKEN` par défaut ne suffit pas (dépôts dans une **autre** org).
2. **GitHub Pages** : Settings > Pages > *Build and deployment* > **Source : GitHub Actions**.
3. Lancer le workflow **Build dashboard** une première fois (onglet Actions > Run workflow).

## Métrique « tests + qualité » : instrumentation de la CI des équipes

Le collecteur lit en priorité l'artefact **`ci-summary`** publié par la CI `maven.yml` de chaque
équipe (tests surefire + couverture + PMD + portes Spotless/ArchUnit). Cette instrumentation est
ajoutée dans le **méta-dépôt** `IUTInfoAix-S201/vigiechiro-pr-companion` (branche `solution`) puis
propagée aux forks via `classroom-sync` (`student_syncs`, entrée SAÉ). Voir
[docs/instrumentation-ci.md](docs/instrumentation-ci.md).

Tant qu'une équipe n'a pas rejoué sa CI avec la version instrumentée, le collecteur **se rabat**
sur le parsing des logs du dernier run (nombre de tests uniquement ; la qualité reste « n/d »).

## Lancer en local

```bash
GH_TOKEN=$(gh auth token) python3 tools/collecte.py        # génère site/data.json
python3 -m http.server --directory site                    # http://localhost:8000
```

Options : `--teams a,b` (limiter), `--no-tests` (saute tests/qualité, plus rapide),
`--no-history` (n'écrit pas l'historique).

## Réglages

Les seuils du voyant qualité de revue sont des constantes en tête de `tools/collecte.py`
(`CORPS_SUBSTANTIEL`, `SEUIL_VERT`, `SEUIL_ROUGE_TAMPON`). Le mapping feature -> priorité
(`PRIORITE`) suit la table du brief « Travail à faire ».

## Diffusion

Ajouter le lien du tableau de bord au site brief (section « Suivre votre avancement »)
et/ou aux README d'équipe.
