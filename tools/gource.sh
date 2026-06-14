#!/usr/bin/env bash
# Bouquet final : visualisation Gource de TOUTES les equipes SAE 2.01.
#
# Fusionne les forks `vigiechiro-pr-companion-*` de l'org en UN SEUL arbre (un
# sous-dossier par equipe, via prefixe de chemin), telecharge les avatars GitHub
# des auteurs, puis rend un mp4 (gource | ffmpeg). Le resultat est destine a etre
# publie en asset de release et revele par le tableau de bord a la fin du projet.
#
# Prerequis : gh (authentifie), git, gource, ffmpeg, xvfb-run, curl, awk, sort.
# Usage : tools/gource.sh [sortie.mp4]
# Variables : ORG, SECONDS_PER_DAY, RES, FPS, GH_TOKEN (sinon `gh auth token`).
set -euo pipefail

ORG="${ORG:-IUTInfoAix-S201-2026}"
PREFIX="vigiechiro-pr-companion-"
OUT="${1:-bouquet-final.mp4}"
SECONDS_PER_DAY="${SECONDS_PER_DAY:-48}"
RES="${RES:-1280x720}"
FPS="${FPS:-60}"
TOKEN="${GH_TOKEN:-$(gh auth token)}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/repos" "$WORK/logs" "$WORK/avatars"

echo "Decouverte des depots d'equipe (org $ORG)..." >&2
mapfile -t REPOS < <(gh api "orgs/$ORG/repos" --paginate \
  --jq ".[] | select(.fork and (.name|startswith(\"$PREFIX\")) and (.name != \"${PREFIX%-}\")) | .name" | sort -u)
echo "${#REPOS[@]} depot(s)." >&2

declare -A AV_DONE
for repo in "${REPOS[@]}"; do
  slug="${repo#"$PREFIX"}"
  echo "  - $slug : clone + log" >&2
  git clone --quiet "https://x-access-token:${TOKEN}@github.com/$ORG/$repo.git" "$WORK/repos/$repo"
  if ! gource --output-custom-log "$WORK/logs/$repo.raw" "$WORK/repos/$repo" 2>/dev/null; then
    echo "    (pas d'historique exploitable, ignore)" >&2
    continue
  fi
  # Prefixe le chemin (champ 4 de `ts|auteur|type|chemin`) par /slug : chaque
  # equipe devient une branche distincte de l'arbre commun. On EXCLUT les bots
  # (auteur champ 2) : leurs commits auto (generate-student, captures) balayent
  # des centaines de fichiers et ecraseraient la visualisation du travail etudiant.
  awk -F'|' -v t="$slug" 'BEGIN{OFS="|"}
    NF>=4 && $2 !~ /\[bot\]$/ && tolower($2) !~ /(github-actions|dependabot|github-classroom|web-flow|actions-user)/ {
      $4="/"t$4; print }' \
    "$WORK/logs/$repo.raw" > "$WORK/logs/$repo.log"
  # Avatars : map nom d'auteur git -> login GitHub, telecharge "<nom>.png"
  # (gource matche l'image par le nom d'auteur affiche dans le log).
  while IFS=$'\t' read -r name login; do
    [[ -z "$name" || -z "$login" || "$login" == "null" ]] && continue
    [[ -n "${AV_DONE["$name"]:-}" ]] && continue
    img="$WORK/avatars/$name.png"; raw="$WORK/avatars/.raw"
    # Telecharge PUIS re-encode en PNG RGB 128x128 propre. Gource (SDL_image)
    # refuse certains avatars GitHub (profil colorimetrique, APNG, page d'erreur
    # HTML recue sous rate-limit...) et PLANTE le rendu sur une ressource invalide.
    # ffmpeg decode (donc valide) et normalise tout en un PNG sur lequel il ne cale pas.
    if curl -fsSL "https://github.com/$login.png?size=128" -o "$raw" 2>/dev/null && [[ -s "$raw" ]] \
       && ffmpeg -y -loglevel error -i "$raw" -vf scale=128:128:flags=lanczos -pix_fmt rgb24 "$img" 2>/dev/null; then
      AV_DONE["$name"]=1
    else
      rm -f "$img"
    fi
    rm -f "$raw"
  done < <(gh api "repos/$ORG/$repo/commits" --paginate \
            --jq '.[] | select(.author and .commit.author) | (.commit.author.name)+"\t"+(.author.login)' \
            2>/dev/null | sort -u)
done

echo "Fusion + tri des logs..." >&2
cat "$WORK"/logs/*.log | sort -n > "$WORK/combined.log"
echo "$(wc -l < "$WORK/combined.log") evenement(s), $(ls "$WORK/avatars" | wc -l) avatar(s)." >&2

echo "Rendu Gource -> $OUT ..." >&2
xvfb-run -a -s "-screen 0 ${RES}x24" gource "$WORK/combined.log" \
  -"$RES" \
  --seconds-per-day "$SECONDS_PER_DAY" --auto-skip-seconds 1 \
  --title "SAE 2.01 VigieChiro - toutes les equipes" \
  --user-image-dir "$WORK/avatars" \
  --hide filenames,mouse,progress \
  --key --highlight-users --multi-sampling \
  --background-colour 0a0a14 --font-size 22 \
  --stop-at-end \
  --output-ppm-stream - \
| ffmpeg -y -loglevel warning -r "$FPS" -f image2pipe -vcodec ppm -i - \
    -vcodec libx264 -preset medium -pix_fmt yuv420p -crf 22 -movflags +faststart \
    "$OUT"
echo "OK : $OUT" >&2
