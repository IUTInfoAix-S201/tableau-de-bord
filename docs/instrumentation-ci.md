# Instrumentation de la CI des équipes (active la qualité de code)

> Étape **différée**. Le tableau de bord fonctionne sans elle (les tests sont lus
> via le repli sur les logs de CI). Cette instrumentation ajoute les métriques de
> **qualité** (couverture JaCoCo, violations PMD, Spotless, ArchUnit) et un comptage
> de tests plus propre, via un artefact `ci-summary` publié par la CI de chaque équipe.

## Ce que ça change

`maven.yml` (CI des équipes) gagne :
- un déclencheur `workflow_dispatch` (rejouer la CI à la demande) ;
- une étape **Spotless non bloquante** (renseigne le tableau, ne fait jamais échouer la CI) ;
- une étape **Résumé CI** (`if: always()`) qui agrège surefire + JaCoCo + PMD + portes
  qualité en `ci-summary.json` ;
- l'**upload** de cet artefact (`ci-summary`).

La **sémantique rouge/verte est préservée** : l'étape `Run tests` reste bloquante
(la CI passe au rouge si un test échoue, conformément à la Definition of Done).

Le fichier instrumenté complet est ici : [`instrumentation-ci/maven.yml`](instrumentation-ci/maven.yml).

## Comment l'appliquer (quand vous êtes prêts)

1. Copier le fichier de référence dans le méta-dépôt, sur la branche `solution` :
   ```bash
   cp docs/instrumentation-ci/maven.yml \
      ../vigiechiro-pr-companion/.github/workflows/maven.yml
   cd ../vigiechiro-pr-companion
   git checkout solution
   git add .github/workflows/maven.yml
   git commit -m "feat(ci): publier un resume ci-summary pour le tableau de bord"
   git push origin solution
   ```
   Le push déclenche `generate-student.yml` qui régénère la branche `main` (version étudiante).

2. Propager aux 7 forks d'équipe via `classroom-sync` (entrée SAÉ de `student_syncs`) :
   déclencher manuellement le workflow `sync-students.yml` du dépôt
   `IUTInfoAix-R203/classroom-sync`.

3. Au prochain push d'équipe (ou via `workflow_dispatch` sur leur `maven.yml`), la CI
   produit l'artefact `ci-summary`. Le collecteur le lit automatiquement (sinon il
   reste sur le repli logs et affiche la qualité en « n/d »).

## Note ruleset

Si le bot GitHub Actions doit pousser (historique du tableau, ou autre), penser à
l'ajouter au bypass du ruleset de l'org concernée (ou exclure le dépôt concerné).
Pour ce dépôt `tableau-de-bord`, l'exclusion du ruleset de branche org a déjà été faite,
ce qui permet au bot de committer l'historique des tendances sur `main`.
