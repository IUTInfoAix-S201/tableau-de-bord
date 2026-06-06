# Instrumentation de la CI des equipes (active la qualite de code)

> Etape **differee**. Le tableau de bord fonctionne sans elle (les tests sont lus
> via le repli sur les logs de CI). Cette instrumentation ajoute les metriques de
> **qualite** (couverture JaCoCo, violations PMD, Spotless, ArchUnit) et un comptage
> de tests plus propre, via un artefact `ci-summary` publie par la CI de chaque equipe.

## Ce que ca change

`maven.yml` (CI des equipes) gagne :
- un declencheur `workflow_dispatch` (rejouer la CI a la demande) ;
- une etape **Spotless non bloquante** (renseigne le tableau, ne fait jamais echouer la CI) ;
- une etape **Resume CI** (`if: always()`) qui agrege surefire + JaCoCo + PMD + portes
  qualite en `ci-summary.json` ;
- l'**upload** de cet artefact (`ci-summary`).

La **semantique rouge/verte est preservee** : l'etape `Run tests` reste bloquante
(la CI passe au rouge si un test echoue, conformement a la Definition of Done).

Le fichier instrumente complet est ici : [`instrumentation-ci/maven.yml`](instrumentation-ci/maven.yml).

## Comment l'appliquer (quand vous etes prets)

1. Copier le fichier de reference dans le meta-depot, sur la branche `solution` :
   ```bash
   cp docs/instrumentation-ci/maven.yml \
      ../vigiechiro-pr-companion/.github/workflows/maven.yml
   cd ../vigiechiro-pr-companion
   git checkout solution
   git add .github/workflows/maven.yml
   git commit -m "feat(ci): publier un resume ci-summary pour le tableau de bord"
   git push origin solution
   ```
   Le push declenche `generate-student.yml` qui regenere la branche `main` (version etudiante).

2. Propager aux 7 forks d'equipe via `classroom-sync` (entree SAE de `student_syncs`) :
   declencher manuellement le workflow `sync-students.yml` du depot
   `IUTInfoAix-R203/classroom-sync`.

3. Au prochain push d'equipe (ou via `workflow_dispatch` sur leur `maven.yml`), la CI
   produit l'artefact `ci-summary`. Le collecteur le lit automatiquement (sinon il
   reste sur le repli logs et affiche la qualite en « n/d »).

## Note ruleset

Si le bot GitHub Actions doit pousser (historique du tableau, ou autre), penser a
l'ajouter au bypass du ruleset de l'org concernee (deja fait pour `capture-vues`).
