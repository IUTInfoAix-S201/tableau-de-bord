"use strict";

const PRIO_TOTAL = { must: 0, should: 0, could: 0 }; // rempli a la lecture

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function pct(p) { return p == null ? "n/d" : p.toFixed(0) + " %"; }

function relTime(iso) {
  if (!iso) return "n/d";
  const d = (Date.now() - new Date(iso).getTime()) / 86400000;
  if (d < 1) return "aujourd'hui";
  if (d < 2) return "hier";
  if (d < 30) return "il y a " + Math.floor(d) + " j";
  return "il y a " + Math.floor(d / 30) + " mois";
}

function staleDays(iso) {
  if (!iso) return 999;
  return (Date.now() - new Date(iso).getTime()) / 86400000;
}

function bar(value, total, cls, medianPct) {
  const p = total ? Math.round(100 * value / total) : 0;
  const med = medianPct != null
    ? `<i class="mediane" style="left:${medianPct}%"></i>` : "";
  return `<div class="barre ${cls}"><span style="width:${p}%"></span>${med}</div>`;
}

function miniBande(cls, o) {
  const p = o.total ? Math.round(100 * o.done / o.total) : 0;
  return `<div class="mini ${cls}"><div class="barre"><span style="width:${p}%"></span></div>`
    + `<small>${o.done}/${o.total}</small></div>`;
}

function sparkline(serie) {
  if (!serie || serie.length < 2) return "";
  const vals = serie.map(p => p.tests_passed == null ? 0 : p.tests_passed);
  const max = Math.max(...vals, 1), min = Math.min(...vals);
  const w = 90, h = 18, n = vals.length;
  const pts = vals.map((v, i) => {
    const x = (i / (n - 1)) * w;
    const y = h - ((v - min) / (max - min || 1)) * h;
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">`
    + `<polyline fill="none" stroke="#27ae60" stroke-width="1.5" points="${pts}"/></svg>`;
}

function ciBadge(status) {
  if (status === "success") return `<span class="badge ok">verte</span>`;
  if (status == null) return `<span class="badge nd">n/d</span>`;
  return `<span class="badge ko">${esc(status)}</span>`;
}

function okBadge(v, labelOk = "OK", labelKo = "KO") {
  if (v == null) return `<span class="badge nd">n/d</span>`;
  return v ? `<span class="badge ok">${labelOk}</span>`
    : `<span class="badge ko">${labelKo}</span>`;
}

function voyantTip(c) {
  return `revues=${c.reviews_given} | commentaires en ligne=${c.inline_comments} | `
    + `changements demandés=${c.changes_requested} | approbations à vide=${c.empty_approvals}`;
}

// --- tri -------------------------------------------------------------------
const SORTERS = {
  tests: t => { const c = confTotaux(t); return c ? c.passed : -1; },
  issues: t => t.issues.total ? t.issues.done / t.issues.total : 0,
  reviewed: t => t.review.pct_reviewed == null ? -1 : t.review.pct_reviewed,
  activity: t => -staleDays(t.last_activity),
  slug: t => t.slug,
};
let currentSort = "tests";
let badgeCtx = null;   // contexte des badges (superlatifs promo) partage equipe/etudiant

function render(data) {
  document.getElementById("meta").textContent =
    `Généré le ${new Date(data.generated_at).toLocaleString("fr-FR")} `
    + `- ${data.teams.length} équipes - total ${data.totals.tests_total} tests, `
    + `${data.totals.issues_total} tâches.`;

  badgeCtx = contexteBadges(data.students);
  renderPodiums(data);
  startCountdown();
  renderStats(data);
  renderAlertes(data);
  renderTable(data);
  renderStudents(data);
}

// --- compte a rebours vers la fin du projet -------------------------------
// Bornes du projet, heure de Paris (UTC+2 en juin) : du 04/06 au 18/06 a 8 h 15.
const DEBUT_PROJET = new Date("2026-06-04T00:00:00+02:00").getTime();
const FIN_PROJET = new Date("2026-06-18T08:15:00+02:00").getTime();
// Bouquet final : vidéo Gource servie par Pages (URL relative) -> lecture inline
// video/mp4 seekable. Stockée en asset de release, déployée dans site/ par la CI.
// (Lire directement l'asset de release donne octet-stream + nosniff -> pas de <video>.)
const BOUQUET_URL = "bouquet-final.mp4";
let cptRebours = null;

// % de tests d'acceptation verts sur l'ensemble de la promo (Σ passés / Σ total).
function pctTestsPromo() {
  const ts = (window.__data && window.__data.teams) || [];
  let p = 0, t = 0;
  ts.forEach(x => { p += (x.tests && x.tests.passed) || 0; t += (x.tests && x.tests.total) || 0; });
  return t ? Math.round(100 * p / t) : 0;
}

// % d'issues terminées sur l'ensemble de la promo (Σ fermées / Σ total).
function pctIssuesPromo() {
  const ts = (window.__data && window.__data.teams) || [];
  let d = 0, t = 0;
  ts.forEach(x => { d += (x.issues && x.issues.done) || 0; t += (x.issues && x.issues.total) || 0; });
  return t ? Math.round(100 * d / t) : 0;
}

function startCountdown() {
  const el = document.getElementById("countdown");
  if (!el) return;
  const boite = (v, l) => `<span class="cd-box"><b>${String(v).padStart(2, "0")}</b><small>${l}</small></span>`;
  const barre = (lbl, pct, cls) =>
    `<div class="cd-bar"><span class="cd-bar-lbl">${lbl} : <b>${pct} %</b></span>`
    + `<div class="barre ${cls}"><span style="width:${pct}%"></span></div></div>`;
  const tick = () => {
    const ms = FIN_PROJET - Date.now();
    const pctTemps = Math.max(0, Math.min(100,
      Math.round(100 * (Date.now() - DEBUT_PROJET) / (FIN_PROJET - DEBUT_PROJET))));
    const pctTests = pctTestsPromo();
    // `?bouquet` force l'aperçu du bouquet final avant l'échéance (test du lecteur).
    const apercu = typeof location !== "undefined" && /[?&]bouquet\b/.test(location.search);
    const fini = ms <= 0 || apercu;
    let haut, bouquet = "";
    if (fini) {
      haut = `<span class="cd-fini">🎆 Projet terminé — bravo à toutes les équipes ! 🦇</span>`;
      // En état « terminé » (vrai ou aperçu), on rend UNE fois et on stoppe le
      // tick : sinon le réécriture du bandeau chaque seconde recrée le <video>
      // -> il recharge en boucle sans jamais démarrer (clignotement).
      if (cptRebours) { clearInterval(cptRebours); cptRebours = null; }
      // Dévoile les podiums en même temps que le bouquet.
      const pod = document.getElementById("podiums");
      if (pod) pod.hidden = false;
      // Bouquet final : la visualisation Gource de l'évolution du code, révélée
      // à l'instant où le compte à rebours atteint zéro.
      bouquet = `<div class="bouquet">
        <div class="bouquet-titre">🎬 Le bouquet final : l'évolution du code de toutes les équipes</div>
        <video class="bouquet-video" controls playsinline preload="metadata" src="${BOUQUET_URL}"></video>
        <a class="bouquet-lien" href="${BOUQUET_URL}" target="_blank" rel="noopener">Ouvrir la vidéo en plein écran ↗</a>
      </div>`;
    } else {
      const s = Math.floor(ms / 1000);
      haut = `<span class="cd-lbl">⏳ Temps restant avant la fin du projet <small>(18/06 à 8 h 15)</small></span>`
        + `<span class="cd-boites">`
        + boite(Math.floor(s / 86400), "jours") + boite(Math.floor(s % 86400 / 3600), "heures")
        + boite(Math.floor(s % 3600 / 60), "min") + boite(s % 60, "s")
        + `</span>`;
    }
    el.innerHTML = `<div class="cd-haut">${haut}</div>`
      + `<div class="cd-progress">`
      + barre("Temps écoulé", pctTemps, "temps")
      + barre("Tests validés", pctTests, "tests")
      + barre("Issues terminées", pctIssuesPromo(), "issues")
      + `</div>${bouquet}`;
    el.hidden = false;
  };
  // Intervalle créé AVANT le 1er tick : si on est déjà « terminé » (ou aperçu),
  // ce 1er tick le coupe immédiatement -> un seul rendu, pas de clignotement.
  if (cptRebours) clearInterval(cptRebours);
  cptRebours = setInterval(tick, 1000);
  tick();
}

// --- podiums « superlatifs » de la promo (dévoilés à countdown 0) ----------
// Chaque podium classe les étudiants sur une métrique amusante, calculée depuis
// les champs étudiant (s) et/ou son activité commits (a = activity.by_student[login]).
const PODIUMS = [
  { emoji: "🦇", nom: "La chauve-souris", desc: "le plus de commits la nuit (22 h–6 h)", unit: "commits",
    val: (s, a) => a ? [22, 23, 0, 1, 2, 3, 4, 5].reduce((x, h) => x + (a.by_hour[h] || 0), 0) : 0 },
  { emoji: "🦫", nom: "Le castor affairé", desc: "le plus de commits (contributions fréquentes)", unit: "commits",
    val: (s, a) => a ? a.total : 0 },
  { emoji: "🦉", nom: "Le hibou vigilant", desc: "le plus de revues de code données", unit: "revues",
    val: s => s.reviews_given || 0 },
  { emoji: "🐓", nom: "Le coq matinal", desc: "le plus de commits tôt le matin (6 h–9 h)", unit: "commits",
    val: (s, a) => a ? [6, 7, 8].reduce((x, h) => x + (a.by_hour[h] || 0), 0) : 0 },
  { emoji: "🐗", nom: "Le sanglier du week-end", desc: "le plus de commits le samedi/dimanche", unit: "commits",
    val: (s, a) => a ? (a.by_weekday[5] || 0) + (a.by_weekday[6] || 0) : 0 },
  { emoji: "🐜", nom: "La fourmi laborieuse", desc: "le plus d'issues fermées", unit: "issues",
    val: s => s.issues_closed || 0 },
  { emoji: "🐢", nom: "La tortue régulière", desc: "le plus de jours actifs distincts", unit: "jours",
    val: (s, a) => a ? Object.values(a.by_day).filter(v => v > 0).length : 0 },
  { emoji: "🧹", nom: "L'élagueur impitoyable", desc: "le plus de lignes supprimées (nettoyage)", unit: "lignes",
    val: s => s.lines_deleted || 0 },
];
const POD_MEDS = ["🥇", "🥈", "🥉"];

function renderPodiums(data) {
  const grid = document.getElementById("podiums-grid");
  if (!grid) return;
  const acts = (data.activity && data.activity.by_student) || {};
  const students = data.students || [];
  grid.innerHTML = PODIUMS.map(p => {
    const top = students.map(s => ({ s, v: p.val(s, acts[s.login]) }))
      .filter(o => o.v > 0)
      .sort((a, b) => b.v - a.v || a.s.login.localeCompare(b.s.login))
      .slice(0, 3);
    const lignes = top.length
      ? top.map((o, i) => `<li><span class="pod-med">${POD_MEDS[i]}</span>`
          + `<span class="pod-login">${esc(o.s.login)}</span>`
          + `<span class="pod-val">${o.v} ${esc(p.unit)}</span>`
          + `<small class="pod-team">${esc(o.s.team)}</small></li>`).join("")
      : `<li class="pod-vide">Personne pour l'instant</li>`;
    return `<div class="podium"><div class="pod-titre"><span class="pod-emoji">${p.emoji}</span> ${esc(p.nom)}</div>`
      + `<div class="pod-desc">${esc(p.desc)}</div><ol class="pod-liste">${lignes}</ol></div>`;
  }).join("") + podiumSkynet(data);
}

// Podium « Adorateurs de Skynet » : classement par ÉQUIPE (et non par étudiant)
// des plus forts apports de code concentrés sur la dernière journée — proxy de
// finition au LLM. score = lignes src du dernier jour × part du total (cf.
// derniere_journee.score_llm côté collecte).
function podiumSkynet(data) {
  const top = (data.teams || [])
    .map(t => ({ t, dj: t.derniere_journee }))
    .filter(o => o.dj && o.dj.score_llm > 0)
    .sort((a, b) => b.dj.score_llm - a.dj.score_llm || a.t.slug.localeCompare(b.t.slug))
    .slice(0, 3);
  const lignes = top.length
    ? top.map((o, i) => `<li><span class="pod-med">${POD_MEDS[i]}</span>`
        + `<span class="pod-login">${esc(o.t.name)}</span>`
        + `<span class="pod-val">${o.dj.solo_max ?? o.dj.lignes} l. / ${Math.round(o.dj.part * 100)} %</span>`
        + `<small class="pod-team" title="le ${esc(o.dj.date)}${o.dj.solo_auteur ? ", " + esc(o.dj.solo_auteur) : ""}, sur ${o.dj.prs} PR">${o.dj.solo_auteur ? esc(o.dj.solo_auteur) : esc(o.dj.date)}</small></li>`).join("")
    : `<li class="pod-vide">Personne pour l'instant</li>`;
  return `<div class="podium podium-skynet"><div class="pod-titre"><span class="pod-emoji">🤖</span> Les adorateurs de Skynet</div>`
    + `<div class="pod-desc">apport de code (src) le plus concentré sur la dernière journée — finition au LLM ?</div>`
    + `<ol class="pod-liste">${lignes}</ol></div>`;
}

// --- statistiques generales (activite collective) -------------------------
const JOURS_SEM = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"];

// Diagramme baton en CSS pur. items: [{label, value, title?, cls?}].
function barChart(items, maxH = 120) {
  const max = Math.max(1, ...items.map(it => it.value || 0));
  const bars = items.map(it => {
    const v = it.value || 0;
    const h = v ? Math.max(2, Math.round(v / max * maxH)) : 0;
    return `<div class="bc-col ${it.cls || ""}" title="${esc(it.title || (it.label + " : " + v))}">`
      + `<span class="bc-v">${v || ""}</span>`
      + `<span class="bc-bar" style="height:${h}px"></span></div>`;
  }).join("");
  const labels = items.map(it => `<span class="bc-l ${it.cls || ""}">${esc(it.label)}</span>`).join("");
  return `<div class="bc-bars" style="height:${maxH + 18}px">${bars}</div>`
    + `<div class="bc-labels">${labels}</div>`;
}

// Liste dense des jours (AAAA-MM-JJ) de d1 a d2 inclus.
function plageJours(d1, d2) {
  const out = [];
  for (let d = new Date(d1 + "T00:00:00Z"), fin = new Date(d2 + "T00:00:00Z");
       d <= fin; d.setUTCDate(d.getUTCDate() + 1)) {
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

function skpi(val, label, title) {
  return `<div class="skpi"${title ? ` title="${esc(title)}"` : ""}><b>${val}</b><small>${esc(label)}</small></div>`;
}

function renderStats(data) {
  const sec = document.getElementById("stats");
  const a = data.activity;
  if (!sec) return;
  if (!a || !a.total) { sec.hidden = true; return; }
  sec.hidden = false;

  // KPI : ampleur du travail collectif.
  const nf = n => (n || 0).toLocaleString("fr-FR");
  const ci = data.ci || {};
  const students = data.students || [];
  const mergedTotal = (data.teams || []).reduce((s, t) => s + ((t.review && t.review.merged_total) || 0), 0);
  const selfMerges = (data.teams || []).reduce((s, t) => s + ((t.review && t.review.self_merges) || 0), 0);
  const pctRelues = mergedTotal ? Math.round(100 * (mergedTotal - selfMerges) / mergedTotal) : null;
  const lignes = students.reduce((s, st) => s + (st.lines_added || 0) + (st.lines_deleted || 0), 0);
  const revues = students.reduce((s, st) => s + (st.reviews_given || 0), 0);
  const nbJours = (a.first_day && a.last_day) ? plageJours(a.first_day, a.last_day).length
    : Object.keys(a.by_day).length;
  const joursActifs = Object.keys(a.by_day).filter(k => a.by_day[k] > 0).length;
  const weCommits = (a.by_weekday[5] || 0) + (a.by_weekday[6] || 0);
  const partWe = Math.round(100 * weCommits / a.total);
  const nuitCommits = a.by_hour.reduce((s, v, h) => s + ((h >= 22 || h < 6) ? v : 0), 0);
  const partNuit = Math.round(100 * nuitCommits / a.total);
  const ciFailPct = ci.runs ? Math.round(100 * (ci.runs_failed || 0) / ci.runs) : null;
  document.getElementById("stats-kpi").innerHTML = [
    // Équipe & production
    skpi(nf(Object.keys(a.by_student || {}).length), "contributeurs actifs"),
    skpi(nf(a.total), "commits", "Total des commits horodatés (toutes branches, dédoublonnés)"),
    skpi(nf(lignes), "lignes écrites", "lignes ajoutées + supprimées (sur les PR)"),
    skpi(nf(mergedTotal), "PR mergées"),
    // Collaboration
    skpi(nf(revues), "revues de code", "revues de pull request données"),
    skpi(pctRelues == null ? "n/d" : pctRelues + " %", "PR relues", "PR mergées relues par un pair (promo)"),
    // Rythme
    skpi(`${joursActifs} / ${nbJours}`, "jours actifs", "jours du projet avec au moins un commit"),
    skpi(`${partWe} %`, "le week-end", `${weCommits} commits le samedi ou le dimanche`),
    skpi(`${partNuit} %`, "la nuit", `${nuitCommits} commits entre 22 h et 6 h`),
    // Intégration continue
    skpi(nf(ci.minutes), "minutes de CI", `≈ ${nf(Math.round((ci.minutes || 0) / 60))} h cumulées de runs GitHub Actions`),
    skpi(nf(ci.runs), "runs CI", "exécutions de workflows depuis le début du projet"),
    skpi(ciFailPct == null ? "n/d" : ciFailPct + " %", "runs CI en échec", `${nf(ci.runs_failed)} runs en échec sur ${nf(ci.runs)}`),
  ].join("");

  // Diagrammes collectifs, masqués par défaut derrière un bouton dépliable (comme
  // les vues détaillées équipe/étudiant). La déclinaison par équipe / par personne
  // vit dans les panneaux détaillés respectifs (detailPanneau / detailEtudiant).
  document.getElementById("stats-charts").innerHTML = blocCharts(a);
  bindStatsToggle();
}

let statsToggleBound = false;
function bindStatsToggle() {
  if (statsToggleBound) return;
  statsToggleBound = true;
  const btn = document.getElementById("stats-toggle");
  const charts = document.getElementById("stats-charts");
  if (!btn || !charts) return;
  btn.hidden = false;
  btn.addEventListener("click", () => {
    charts.hidden = !charts.hidden;
    btn.setAttribute("aria-expanded", String(!charts.hidden));
    const ch = btn.querySelector(".chevron");
    if (ch) ch.textContent = charts.hidden ? "▶" : "▼";
  });
}

// --- diagrammes d'activite (reutilisables : collectif, equipe, personne) ----
// Diagramme par jour sur la plage DENSE du projet (jours sans commit visibles)
// pour que collectif, equipe et personne partagent le meme axe temporel.
function chartJour(src) {
  const a = (window.__data && window.__data.activity) || {};
  const byDay = src.by_day || {};
  const jours = (a.first_day && a.last_day) ? plageJours(a.first_day, a.last_day)
    : Object.keys(byDay).sort();
  return barChart(jours.map(d => {
    const wd = (new Date(d + "T00:00:00Z").getUTCDay() + 6) % 7;   // 0=Lun
    const [, mm, dd] = d.split("-");
    return { label: `${dd}/${mm}`, value: byDay[d] || 0, cls: wd >= 5 ? "we" : "",
             title: `${d} (${JOURS_SEM[wd]}) : ${byDay[d] || 0} commits` };
  }));
}

function chartSemaine(src) {
  const byWd = src.by_weekday || [];
  return barChart(JOURS_SEM.map((lbl, i) =>
    ({ label: lbl, value: byWd[i] || 0, cls: i >= 5 ? "we" : "" })));
}

function chartHeure(src) {
  const byHr = src.by_hour || [];
  return barChart(Array.from({ length: 24 }, (_, h) =>
    ({ label: String(h), value: byHr[h] || 0, cls: (h < 7 || h >= 20) ? "nuit" : "",
       title: `${h} h–${h + 1} h : ${byHr[h] || 0} commits` })));
}

// Bloc des 3 diagrammes pour une source {total, by_day, by_weekday, by_hour}.
function blocCharts(src) {
  if (!src || !src.total)
    return `<p class="aide">Aucune activité (commit) enregistrée sur la période.</p>`;
  return `<div class="charts">
    <div class="chart"><h3>Contributions par jour du projet</h3><div class="barchart">${chartJour(src)}</div></div>
    <div class="chart"><h3>Par jour de la semaine</h3><div class="barchart">${chartSemaine(src)}</div></div>
    <div class="chart"><h3>Par heure du jour</h3><div class="barchart">${chartHeure(src)}</div></div>
  </div>`;
}

function renderAlertes(data) {
  const out = [];
  data.teams.forEach(t => {
    const lc = t.late_commits;
    if (lc && lc.count > 0) {
      const qui = (lc.authors && lc.authors.length) ? ` — ${lc.authors.map(esc).join(", ")}` : "";
      const titre = `${lc.count} commit(s) de code (src/) d'étudiant après l'échéance du 18/06 8 h 15 ; dernier ${relTime(lc.last)}${qui} — hors captures, sync d'infra et commits sans modification du livrable`;
      out.push(`<span class="alerte danger" title="${esc(titre)}">⏰ commits de code après la fin (${lc.count}) : ${esc(t.slug)}</span>`);
    }
    const dj = t.derniere_journee;
    if (dj && dj.suspect) {
      const pct = Math.round(dj.part * 100);
      const solo = dj.solo_max ?? dj.lignes;
      const qui = dj.solo_auteur ? ` par ${dj.solo_auteur}` : "";
      const titre = `${solo} lignes de code déposées${qui} le ${dj.date} (${pct} % du total de l'équipe, ${dj.auteurs ?? "?"} auteur(s) actifs ce jour, ${dj.prs} PR) — apport solo très concentré sur la dernière journée, à vérifier (usage d'un LLM pour finir ?)`;
      out.push(`<span class="alerte llm" title="${esc(titre)}">🤖 gros apport solo dernier jour : ${esc(t.slug)} (${solo} l.${dj.solo_auteur ? " — " + esc(dj.solo_auteur) : ""} / ${pct} %)</span>`);
    }
    if (t.ci_status && t.ci_status !== "success")
      out.push(`<span class="alerte">CI rouge : ${esc(t.slug)}</span>`);
    if (staleDays(t.last_activity) > 4)
      out.push(`<span class="alerte warn">inactive (${relTime(t.last_activity)}) : ${esc(t.slug)}</span>`);
    if (t.review.merged_total >= 3 && t.review.pct_reviewed != null && t.review.pct_reviewed < 0.5)
      out.push(`<span class="alerte warn">merges peu relus : ${esc(t.slug)}</span>`);
  });
  const sec = document.getElementById("alertes");
  if (out.length) {
    document.getElementById("alertes-contenu").innerHTML = out.join(" ");
    sec.hidden = false;
  } else {
    sec.hidden = true;
  }
}

// Conformite agregee par equipe : Σ passes / Σ total sur conformite_by_feature
// (features + parcours + integrite). null si la CI conformite n'a pas encore tourne.
function confTotaux(t) {
  if (!t || !t.conformite) return null;
  let p = 0, tot = 0;
  for (const k in t.conformite) {
    p += (t.conformite[k] && t.conformite[k].passed) || 0;
    tot += (t.conformite[k] && t.conformite[k].total) || 0;
  }
  return { passed: p, total: tot, pct: tot ? Math.round(100 * p / tot) : null };
}

// Classement canonique des equipes : nombre de tests de CONFORMITE reussis (mesure
// de reference = l'ecran passe-t-il NOS tests), puis tests de l'equipe, issues, slug.
// Sert au podium ET de departage au tri du tableau (medailles et rang concordent).
function classementCanonique(a, b) {
  const ca = confTotaux(a), cb = confTotaux(b);
  return ((cb ? cb.passed : -1) - (ca ? ca.passed : -1))
    || (b.tests.pct || 0) - (a.tests.pct || 0)
    || b.issues.done - a.issues.done
    || a.slug.localeCompare(b.slug);
}

function renderTable(data) {
  const med = data.promo || {};
  const teams = [...data.teams].sort((a, b) => {
    const va = SORTERS[currentSort](a), vb = SORTERS[currentSort](b);
    const d = (typeof va === "string") ? va.localeCompare(vb) : (vb - va);
    return d || classementCanonique(a, b);   // departage = classement canonique
  });

  // Podium (sur le classement canonique = tests de conformite), independant du tri courant.
  const podium = {};
  [...data.teams].filter(t => confTotaux(t))
    .sort(classementCanonique)
    .slice(0, 3)
    .forEach((t, i) => {
      podium[t.slug] = [["👑", "Équipe en tête"], ["🥈", "2e équipe"], ["🥉", "3e équipe"]][i];
    });

  const corps = document.getElementById("classement-corps");
  corps.innerHTML = "";
  teams.forEach((t, i) => {
    const tr = document.createElement("tr");
    tr.className = "equipe";
    tr.id = idEquipe(t.slug);
    const conf = confTotaux(t);
    tr.innerHTML = `
      <td class="rang"><span class="rang-badge">${i + 1}</span></td>
      <td class="nom-equipe"><span class="chevron">▶</span>${podium[t.slug] ? `<span class="medaille" title="${esc(podium[t.slug][1])}">${podium[t.slug][0]}</span> ` : ""}${esc(t.name)}${t.repo_url ? ` <a class="lien-repo" href="${esc(t.repo_url)}" target="_blank" rel="noopener" title="Ouvrir le dépôt GitHub de l'équipe">↗ dépôt</a>` : ""}${sparkline(t.trend && t.trend.tests_series)}</td>
      <td class="num">
        ${bar(conf ? conf.passed : 0, conf ? conf.total : 0, "tests", null)}
        <span class="barre-label" title="Tests de conformité réussis (mesure de référence, base du classement)">${conf ? conf.passed + "/" + conf.total + " (" + pct(conf.pct) + ")" : "n/d"}</span>
      </td>
      <td class="num">
        ${bar(t.issues.done, t.issues.total, "issues", med.median_issues_pct)}
        <span class="barre-label">${t.issues.done}/${t.issues.total}</span>
      </td>
      <td class="num"><div class="mini-bandes">
        ${miniBande("must", t.priorities.must)}
        ${miniBande("should", t.priorities.should)}
        ${miniBande("could", t.priorities.could)}
        <span class="mvp" title="MVP (toutes les MUST)">${t.priorities.mvp_complete ? "✅" : "⬜"}</span>
      </div></td>
      <td class="num">${t.review.pct_reviewed == null ? '<span class="badge nd">n/a</span>' : pct(t.review.pct_reviewed * 100)}</td>
      <td class="num">${relTime(t.last_activity)}</td>
      <td class="num">${ciBadge(t.ci_status)}</td>`;
    const detail = document.createElement("tr");
    detail.className = "detail";
    detail.hidden = true;
    detail.innerHTML = `<td colspan="8">${detailPanneau(t)}</td>`;
    tr.addEventListener("click", e => {
      if (e.target.closest("a")) return;   // ne pas replier en cliquant le lien dépôt
      detail.hidden = !detail.hidden;
      tr.querySelector(".chevron").textContent = detail.hidden ? "▶" : "▼";
    });
    corps.appendChild(tr);
    corps.appendChild(detail);
  });
}

// --- frise d'avancement par feature (chaine VigieChiro) -------------------
const FEATURE_LABEL = {
  diagnostic: "Diagnostic", lot: "Lot", bibliotheque: "Bibliothèque",
  validation: "Validation", importation: "Importation",
  qualification: "Qualification", multisite: "Multisite", passage: "Passage",
};
const FEATURE_EMOJI = {
  diagnostic: "🩺", lot: "📦", bibliotheque: "📚", validation: "✅",
  importation: "📥", qualification: "🎧", multisite: "🗺️", passage: "🎯",
};
const MOSCOW = { must: "M", should: "S", could: "C" };

function friseFeatures(t) {
  if (!t.features || !t.features.length) return "";
  const total = t.features.length;
  const faits = t.features.filter(f => f.complete).length;
  const noeuds = t.features.map((f, i) => {
    const p = f.total ? Math.round(100 * f.done / f.total) : 0;
    const etat = f.complete ? "complete" : (f.done > 0 ? "encours" : "vide");
    const lbl = FEATURE_LABEL[f.key] || f.key;
    const mos = f.priority
      ? `<span class="moscow ${f.priority}" title="${esc(f.priority)}">${MOSCOW[f.priority] || ""}</span>` : "";
    const lien = f.capture_url
      ? `<a class="capture" href="${esc(f.capture_url)}" target="_blank" rel="noopener" title="Voir l'écran (capture dans le dépôt)">📸 voir l'écran ↗</a>` : "";
    const fleche = i < total - 1 ? `<span class="fleche">▸</span>` : "";
    const tTests = f.tests ? `tests ${f.tests.passed}/${f.tests.total}` : "tests n/d";
    const tTaches = f.issues ? ` · tâches ${f.issues.done}/${f.issues.total}` : "";
    const cf = f.conformite;
    const tConf = cf ? ` · conformité ${cf.passed}/${cf.total}` : "";
    const tip = `${lbl} — ${tTests}${tTaches}${tConf}` + (f.source === "issues" ? " (jauge : tâches)" : "");
    // Badge conformité : l'écran passe-t-il NOS tests de référence (lookup fx:id) ?
    // vert = tout passe, orange = partiel, rouge = aucun. Distinct des tests de l'équipe.
    const confColor = cf ? (cf.total && cf.passed === cf.total ? "#2e7d32" : (cf.passed > 0 ? "#ef6c00" : "#c62828")) : null;
    const confBadge = cf
      ? `<span class="conf-badge" style="color:${confColor};font-size:.8em;white-space:nowrap" title="Tests de référence (conformité) : ${cf.passed}/${cf.total} — distinct des tests de l'équipe">⚑ ${cf.passed}/${cf.total}</span>`
      : "";
    return `<div class="feat ${etat}" title="${esc(tip)}">
        <span class="feat-tete"><span class="emoji">${FEATURE_EMOJI[f.key] || "•"}</span> ${esc(lbl)} ${mos}</span>
        <span class="jauge"><span style="width:${p}%"></span></span>
        <span class="cpt">${f.done}/${f.total} ${confBadge}</span>
        ${lien}
      </div>${fleche}`;
  }).join("");
  return `<div class="frise">
      <div class="frise-titre">Avancement par écran <small>${faits}/${total} terminés</small></div>
      <div class="frise-rail">${noeuds}</div>
    </div>`;
}

// Id DOM stable d'une ligne etudiant (ancre du lien depuis le panneau equipe).
function idEtudiant(login) {
  return "etu-" + String(login).replace(/[^a-zA-Z0-9_-]/g, "_");
}

// Id DOM stable d'une ligne equipe (ancre du lien depuis la table etudiant).
function idEquipe(slug) {
  return "eq-" + String(slug).replace(/[^a-zA-Z0-9_-]/g, "_");
}

function detailPanneau(t) {
  const q = t.quality, b = t.bus_factor, r = t.review;
  const bal = (b.balance != null) ? b.balance : null;
  const tot = (window.__data ? totauxEquipes(window.__data) : {})[t.slug] || null;
  const contribs = t.contributors.map(c => {
    const { taux, parts } = tauxContribution(c, tot);
    const tauxCell = taux == null ? '<span class="badge nd">n/d</span>'
      : `<strong>${Math.round(taux * 100)} %</strong>`;
    return `
    <tr>
      <td class="login"><a class="lien-etudiant" href="#${idEtudiant(c.login)}" data-login="${esc(c.login)}" title="Voir ${esc(c.login)} dans le classement par étudiant">${esc(c.login)}</a></td>
      <td class="num"><strong>${c.tests_validated ?? 0}</strong></td>
      <td class="num">${c.branch_commits ?? 0}</td>
      <td class="num">${c.prs_open}/${c.prs_merged}</td>
      <td class="num">${c.reviews_given}/${c.reviews_received}</td>
      <td class="num">${c.issues_closed}/${c.issues_assigned}</td>
      <td class="num"><span class="pastille ${c.review_quality}" title="${esc(voyantTip(c))}"></span></td>
      <td class="num" title="${esc(tauxTip(parts))}">${tauxCell}</td>
    </tr>`;
  }).join("");

  return `<div class="panneau">
    ${friseFeatures(t)}
    <div class="qualite">
      <div class="kpi"><b>${t.open_branches == null ? "n/d" : t.open_branches}</b><small>branches en cours</small></div>
      <div class="kpi"><b>${q.coverage_pct == null ? "n/d" : q.coverage_pct + " %"}</b><small>couverture JaCoCo</small></div>
      <div class="kpi"><b>${q.pmd_violations == null ? "n/d" : q.pmd_violations}</b><small>violations PMD</small></div>
      <div class="kpi"><b>${okBadge(q.spotless_ok)}</b><small>Spotless</small></div>
      <div class="kpi"><b>${okBadge(q.archunit_ok)}</b><small>ArchUnit (MVVM)</small></div>
      <div class="kpi"><b>${r.merged_total}</b><small>PR mergées (${r.self_merges} sans revue)</small></div>
      <div class="kpi"><b title="Équilibre du travail (entropie normalisée des commits) : 100% = parfaitement réparti entre les membres, bas = concentré sur quelques-uns. Bus factor (Wikipédia) : ${b.factor}.">${bal != null ? `<span class="busbar"><span style="width:${bal}%"></span></span> ${bal} %` : "n/d"}</b>
        <small>équilibre du travail (${b.active_members}/${b.members} actifs)</small></div>
      ${ciKpisEquipe(t.slug)}
    </div>
    <table class="contribs">
      <thead><tr>
        <th>Contributeur (login GitHub)</th>
        <th class="num" title="Tests activés (retrait de @Disabled) ou ajoutés par les PR mergées de l'étudiant, lus dans le diff">Tests validés</th>
        <th class="num" title="Commits dans des branches non encore mergées (travail en cours)">Travail en cours</th>
        <th class="num" title="PR actuellement ouvertes (en cours) / PR mergées">PR en cours/merg.</th><th class="num">Revues don./rec.</th>
        <th class="num">Issues fer./assig.</th><th class="num">Revue</th>
        <th class="num" title="Part du travail de l'équipe (lignes ajoutées, PR, contribution par feature, issues, revues, travail en cours ; somme = 100 %)">Contribution</th>
      </tr></thead>
      <tbody>${contribs || '<tr><td colspan="8">Aucun contributeur détecté.</td></tr>'}</tbody>
    </table>
    ${featureContribEquipe(t)}
    ${blocActivite((window.__data && window.__data.activity && window.__data.activity.by_team || {})[t.slug],
      "Activité de l'équipe (commits)")}
  </div>`;
}

// Tuiles CI d'une equipe (minutes cumulees, runs, taux d'echec), ou rien si
// aucune donnee CI (ex. equipe de reference synthetique).
function ciKpisEquipe(slug) {
  const c = (window.__data && window.__data.ci && window.__data.ci.by_team || {})[slug];
  if (!c || !c.runs) return "";
  const nf = n => (n || 0).toLocaleString("fr-FR");
  const failPct = Math.round(100 * (c.runs_failed || 0) / c.runs);
  return `<div class="kpi"><b title="≈ ${nf(Math.round((c.minutes || 0) / 60))} h cumulées de runs">${nf(c.minutes)}</b><small>minutes de CI</small></div>`
    + `<div class="kpi"><b>${nf(c.runs)}</b><small>runs CI</small></div>`
    + `<div class="kpi"><b>${failPct} %</b><small>runs CI en échec</small></div>`;
}

// Bloc activite (titre + 3 diagrammes) pour un panneau detaille, ou rien si
// aucune donnee d'activite (ex. equipe de reference synthetique).
function blocActivite(src, titre) {
  if (!src || !src.total) return "";
  return `<div class="frise-titre" style="margin-top:1rem">${esc(titre)} <small>${src.total} commits</small></div>`
    + blocCharts(src);
}

// --- classement par etudiant (toutes equipes confondues) ------------------
// Tri par defaut : TAUX DE CONTRIBUTION (part du travail de l'equipe), puis
// contribution par feature > PR mergees > issues. Le nb de tests, sorti du taux
// (gameable par le decommentage en masse), n'est plus une colonne du classement.
const ESTRING = new Set(["login", "team"]);
const ETIEBREAK = ["feature_equivalents", "prs_merged", "issues_closed", "branch_commits", "commits"];
let currentESort = "taux";

function compareStudents(a, b) {
  if (ESTRING.has(currentESort)) {
    return String(a[currentESort]).localeCompare(String(b[currentESort]));
  }
  // colonne choisie (desc), puis chaine de departage PR > issues > commits, puis login
  for (const k of [currentESort, ...ETIEBREAK.filter(k => k !== currentESort)]) {
    const d = (b[k] || 0) - (a[k] || 0);
    if (d) return d;
  }
  return a.login.localeCompare(b.login);
}

// Helpers badges dérivés des PR / features de l'étudiant.
function plusGrossePR(s) { return Math.max(0, ...((s.prs || []).map(p => p.additions || 0))); }
function nbPetitesPR(s) { return (s.prs || []).filter(p => p.merged && (p.additions || 0) <= 30).length; }
function nbFeatures(s) { return (s.features || []).length; }

// Contexte pour les badges : maxima de la promo + equipes ayant au moins un actif.
function contexteBadges(students) {
  const max = k => Math.max(0, ...students.map(s => s[k] || 0));
  const mediane = k => {
    const xs = students.map(s => s[k] || 0).sort((a, b) => a - b);
    const n = xs.length;
    return n ? (n % 2 ? xs[(n - 1) / 2] : (xs[n / 2 - 1] + xs[n / 2]) / 2) : 0;
  };
  const actif = s => (s.commits + s.prs_merged + s.reviews_given + (s.branch_commits || 0)) > 0;
  const actifsParEquipe = {};
  students.filter(actif).forEach(s => {
    actifsParEquipe[s.team] = (actifsParEquipe[s.team] || 0) + 1;
  });
  return {
    commits: max("commits"), reviews_given: max("reviews_given"),
    prs_merged: max("prs_merged"), inline_comments: max("inline_comments"),
    reviews_received: max("reviews_received"), changes_requested: max("changes_requested"),
    features: Math.max(0, ...students.map(nbFeatures)),
    grossePR: Math.max(0, ...students.map(plusGrossePR)),
    petitesPR: Math.max(0, ...students.map(nbPetitesPR)),
    med: {
      commits: mediane("commits"), prs_merged: mediane("prs_merged"),
      reviews_given: mediane("reviews_given"), reviews_received: mediane("reviews_received"),
      issues_closed: mediane("issues_closed"),
    },
    actifsParEquipe,
  };
}

// Badges (achievements) calcules a partir des donnees existantes.
function badgesEtudiant(s, c) {
  const b = [];
  // superlatifs (un seul critere, au-dessus de 0 et egal au max de la promo)
  if (s.commits > 0 && s.commits === c.commits) b.push(["🏗️", "Bâtisseur : le plus de commits de la promo"]);
  if (s.reviews_given > 0 && s.reviews_given === c.reviews_given) b.push(["🔍", "La loupe : le plus de revues de code"]);
  if (s.prs_merged > 0 && s.prs_merged === c.prs_merged) b.push(["🚀", "Locomotive : le plus de PR mergées"]);
  if (s.inline_comments > 0 && s.inline_comments === c.inline_comments) b.push(["💬", "Bavard utile : le plus de commentaires de revue"]);
  if (s.reviews_received > 0 && s.reviews_received === c.reviews_received) b.push(["🌟", "La star : le code le plus relu par les autres"]);
  if (s.changes_requested > 0 && s.changes_requested === c.changes_requested) b.push(["🛡️", "Le gardien : le plus de changements demandés en revue"]);
  if (nbFeatures(s) > 0 && nbFeatures(s) === c.features) b.push(["🗺️", "L'explorateur : le plus de features différentes touchées"]);
  { const g = plusGrossePR(s); if (g > 0 && g === c.grossePR) b.push(["🐘", "L'éléphant : la plus grosse PR de la promo"]); }
  { const p = nbPetitesPR(s); if (p > 0 && p === c.petitesPR) b.push(["🐿️", "L'écureuil : le plus de toutes petites PR (≤ 30 lignes)"]); }
  // qualitatifs (cumulables)
  // Couteau suisse : vraie polyvalence -> au-dessus de la médiane promo sur les 4
  // dimensions clés (sinon trop courant : 68/87 avec le simple « >0 partout »).
  if ((s.commits || 0) > c.med.commits && (s.prs_merged || 0) > c.med.prs_merged
      && (s.reviews_given || 0) > c.med.reviews_given && (s.issues_closed || 0) > c.med.issues_closed)
    b.push(["🐝", "Couteau suisse : au-dessus de la médiane de la promo sur le code, les PR, les revues et les issues"]);
  // Fair-play : relit autant qu'il est relu, à un niveau soutenu (au-dessus de
  // la médiane en revues données ET reçues), sinon trop courant (1 donnée ↔ 1 reçue).
  if (c.med.reviews_given > 0 && (s.reviews_given || 0) >= c.med.reviews_given
      && (s.reviews_received || 0) >= c.med.reviews_received
      && Math.abs((s.reviews_given || 0) - (s.reviews_received || 0)) <= 2)
    b.push(["🤝", "Fair-play : relit autant qu'il est relu, à un niveau soutenu (au-dessus de la médiane)"]);
  if (s.review_quality === "green" && s.changes_requested >= 1) b.push(["🧐", "Œil de lynx : vraies revues, demande des changements"]);
  if (s.review_quality === "red") b.push(["🦆", "Tampon : approbations à vide"]);
  // Passager clandestin : moins de 5 % du travail de l'équipe (taux de
  // contribution), alors qu'au moins un coéquipier contribue. Capte aussi les
  // quasi-inactifs (un commit alibi), pas seulement les zéros absolus.
  if (s.taux != null && s.taux < 0.05 && (c.actifsParEquipe[s.team] || 0) >= 1)
    b.push(["👻", "Passager clandestin : moins de 5 % du travail de l'équipe alors qu'au moins un coéquipier contribue"]);
  return b;
}

// Totaux d'equipe (denominateurs des parts de contribution), calcules a partir
// des contributeurs de chaque equipe -> {slug: {lines, prs, issues_closed, commits, tests_validated}}.
function totauxEquipes(data) {
  const m = {};
  (data.teams || []).forEach(t => {
    const cs = t.contributors || [];
    const som = f => cs.reduce((a, c) => a + (f(c) || 0), 0);
    m[t.slug] = {
      lines: som(c => (c.lines_added || 0) + (c.lines_deleted || 0)),
      lines_added: som(c => c.lines_added),
      prs: som(c => (c.prs_open || 0) + (c.prs_merged || 0)),
      prs_merged: som(c => c.prs_merged),
      issues_closed: som(c => c.issues_closed),
      commits: som(c => c.commits),
      tests_validated: som(c => c.tests_validated),
      feature_equivalents: som(c => c.feature_equivalents),
      reviews_given: som(c => c.reviews_given),
      branch_commits: som(c => c.branch_commits),
      members: (t.bus_factor && t.bus_factor.members) || cs.length,
    };
  });
  return m;
}

function partPct(v, total) { return total ? Math.round(100 * v / total) : 0; }

// Bloc KPI avec barre de part de contribution a l'equipe.
function kpiPart(label, valHtml, val, total) {
  const p = partPct(val, total);
  return `<div class="kpi part">
    <b>${valHtml}</b><small>${esc(label)}</small>
    <div class="barre part-barre"><span style="width:${p}%"></span></div>
    <small class="part-lbl">${total ? p + " % de l'équipe" : "n/d"}</small>
  </div>`;
}

// --- taux de contribution + facteur d'effort -----------------------------
// Taux = moyenne ponderee des PARTS d'equipe de l'etudiant sur 6 dimensions.
// « Sortie ponderee » : valorise la Definition of Done (tests verts) sans
// ignorer revues et travail en cours. Commits bruts exclus (non fiables sans
// squash). Chaque part etant valeur/total_equipe, les taux d'une equipe
// somment a 100 % -> la moyenne d'une equipe de N est 1/N.
// PR mergées et lignes à poids égal (27,5 % chacune) : unité de travail livrée
// + volume de code produit. « Contribution par feature » à 20 % : somme des parts
// de l'étudiant dans le code de PRODUCTION (src/main) de chaque écran — remplace
// l'ancien « tests validés » (gameable par le décommentage @Disabled en masse).
// Fiable, normalisé par feature, non manipulable. Lignes plafonnées car le FXML/CSS
// verbeux sur-valorise le volume.
const CONTRIB_DIMS = [
  { cle: "lines", label: "lignes ajoutées", poids: 0.275, tot: "lines_added", val: s => s.lines_added || 0 },
  { cle: "prm", label: "PR mergées", poids: 0.275, tot: "prs_merged", val: s => s.prs_merged || 0 },
  { cle: "feat", label: "contribution par feature", poids: 0.20, tot: "feature_equivalents", val: s => s.feature_equivalents || 0 },
  { cle: "iss", label: "issues fermées", poids: 0.10, tot: "issues_closed", val: s => s.issues_closed || 0 },
  { cle: "rev", label: "revues données", poids: 0.10, tot: "reviews_given", val: s => s.reviews_given || 0 },
  { cle: "wip", label: "travail en cours", poids: 0.05, tot: "branch_commits", val: s => s.branch_commits || 0 },
];

// Renvoie {taux: 0..1 | null, parts: [{label, poids, part, apport}]}. On ne garde
// que les dimensions dont le total d'equipe > 0 et on RENORMALISE leurs poids
// (somme ramenee a 1) -> la somme par equipe reste 100 % meme si une dimension
// est absente (ex. aucune revue, ou lievres sans lignes). null si rien de mesurable.
function tauxContribution(s, tot) {
  if (!tot) return { taux: null, parts: [] };
  const actives = CONTRIB_DIMS.filter(d => (tot[d.tot] || 0) > 0);
  const sommePoids = actives.reduce((a, d) => a + d.poids, 0);
  if (!sommePoids) return { taux: null, parts: [] };
  let taux = 0;
  const parts = actives.map(d => {
    const poids = d.poids / sommePoids;          // renormalise
    const part = d.val(s) / tot[d.tot];          // part de l'etudiant dans l'equipe
    const apport = poids * part;
    taux += apport;
    return { label: d.label, poids, part, apport };
  });
  return { taux, parts };
}

// Facteur d'effort : ideal = 1/N (N = membres) ; min(1, taux / ideal).
function facteurEffort(taux, n) {
  if (taux == null || !n) return null;
  return Math.min(1, taux * n);                  // taux / (1/n) = taux * n
}

// Badge gradue : vert plein a 1, ambre en dessous.
function badgeFacteur(f) {
  if (f == null) return `<span class="badge nd">n/d</span>`;
  const cls = f >= 0.999 ? "plein" : "partiel";
  return `<span class="facteur ${cls}" title="Facteur d'effort appliqué à la note">`
    + `<span class="facteur-jauge"><span style="width:${Math.round(f * 100)}%"></span></span>`
    + `${f.toFixed(2)}</span>`;
}

// Infobulle de ventilation du taux : pour chaque dimension, la part de l'étudiant
// dans l'équipe × le poids de la dimension = points apportés (leur somme = le taux).
// Le poids affiché est la pondération de référence REMISE À L'ÉCHELLE sur les seules
// dimensions où l'équipe a de l'activité (pour que le total fasse 100 %), donc il
// peut différer du barème nominal selon l'équipe (identique pour tous ses membres).
function tauxTip(parts) {
  if (!parts.length) return "Aucune dimension mesurable dans l'équipe.";
  const lignes = parts.map(p =>
    `• ${p.label} : ${Math.round(p.part * 100)} % de l'équipe × poids ${Math.round(p.poids * 100)} % = ${Math.round(p.apport * 100)} pts`
  ).join("\n");
  return "D'où vient ce taux (somme des points = le taux).\n"
    + "« % de l'équipe » = la part de l'étudiant ; « poids » = barème (PR 27,5, lignes 27,5,\n"
    + "feature 20, issues 10, revues 10, en cours 5) rééquilibré sur les dimensions où\n"
    + "l'équipe a de l'activité, donc identique pour tous les membres de l'équipe.\n"
    + lignes;
}

function featuresEtudiant(s) {
  const shares = s.feature_shares || {};
  const touched = new Set(s.features || []);
  // Ordre pipeline ; on garde les features touchées OU avec une part de code de prod.
  const keys = Object.keys(FEATURE_LABEL).filter(k => touched.has(k) || shares[k] != null);
  if (!keys.length)
    return `<p class="aide">Aucune feature identifiée (ni code de production, ni issue/ PR taguée).</p>`;
  const chips = keys.map(k => {
    const pct = shares[k] != null ? Math.round(shares[k] * 100) : null;
    const part = pct != null
      ? ` <strong class="feat-part" style="opacity:${0.5 + shares[k] / 2}">${pct} %</strong>` : "";
    const titre = pct != null
      ? `${pct} % du code de production de cet écran (src/main)`
      : "feature touchée (sans code de production mesuré)";
    return `<span class="feat-chip" title="${esc(titre)}"><span class="emoji">${FEATURE_EMOJI[k] || "•"}</span> ${esc(FEATURE_LABEL[k] || k)}${part}</span>`;
  }).join(" ");
  return `<div class="feat-chips">${chips}</div>`;
}

// Qui possède chaque écran : barre empilée des parts de code de production (src/main).
const FC_PALETTE = ["#4a90d9", "#7bb563", "#e8a838", "#e74c3c", "#8e44ad", "#00838f", "#c0392b", "#16a085"];
function featureContribEquipe(t) {
  const fc = t.feature_contrib || {};
  const keys = Object.keys(FEATURE_LABEL).filter(k => fc[k] && fc[k].total > 0);
  if (!keys.length) return "";
  const rows = keys.map(k => {
    const owners = fc[k].owners || [];
    const segs = owners.map((o, i) => {
      const pct = Math.round(o.part * 100);
      return `<span class="fc-seg" style="width:${pct}%;background:${FC_PALETTE[i % FC_PALETTE.length]}"`
        + ` title="${esc(o.login)} : ${pct} % (${o.lignes} l.)">${o.part >= 0.16 ? esc(o.login.split(/[-_]/)[0]) : ""}</span>`;
    }).join("");
    return `<div class="fc-row">
      <span class="fc-feat"><span class="emoji">${FEATURE_EMOJI[k] || "•"}</span> ${esc(FEATURE_LABEL[k] || k)}</span>
      <span class="fc-bar">${segs}</span>
      <span class="fc-tot" title="lignes de code de production (src/main) de cet écran">${fc[k].total} l.</span>
    </div>`;
  }).join("");
  return `<div class="frise-titre" style="margin-top:1rem">Contribution par feature`
    + ` <small>part de chacun dans le code de production (src/main) de chaque écran</small></div>`
    + `<div class="fc-grid">${rows}</div>`;
}

function prListEtudiant(s) {
  const prs = s.prs || [];
  if (!prs.length) return `<p class="aide">Aucune pull request (ouverte ou mergée).</p>`;
  const rows = prs.map(p => {
    const etat = p.merged ? `<span class="badge ok">mergée</span>`
      : (p.state === "OPEN" ? `<span class="badge nd">ouverte</span>` : `<span class="badge ko">fermée</span>`);
    // Tests activés/ajoutés par CETTE PR (retrait de @Disabled + nouveaux @Test).
    // ≥100 = surligné : souvent une finition « passe-finale » qui lève le @Disabled
    // de tout un pan de la suite d'un coup (à juger : apport réel ou uncomment massif).
    const act = p.actives;
    const actCell = (act == null) ? `<td class="num"></td>`
      : `<td class="num${act >= 100 ? " alerte-cell" : ""}" title="tests activés (retrait de @Disabled) ou ajoutés par cette PR">${act}</td>`;
    return `<tr>
      <td><a href="${esc(p.url)}" target="_blank" rel="noopener">#${p.number} ${esc(p.title)} ↗</a></td>
      <td class="num">${etat}</td>
      <td class="num diff"><span class="add">+${p.additions}</span> <span class="del">−${p.deletions}</span></td>
      ${actCell}
    </tr>`;
  }).join("");
  return `<table class="contribs prs">
    <thead><tr><th>Pull request</th><th class="num">État</th><th class="num">Lignes</th><th class="num" title="Tests activés (retrait de @Disabled) ou ajoutés par la PR. Un gros nombre sur une seule PR = activation en masse, à vérifier.">Tests activés</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function detailEtudiant(s, tot) {
  tot = tot || { lines: 0, prs: 0, issues_closed: 0, commits: 0, tests_validated: 0 };
  const add = s.lines_added || 0, del = s.lines_deleted || 0;
  const lignes = add + del;
  const prCount = (s.prs_open || 0) + (s.prs_merged || 0);
  const lignesHtml = `<span class="add">+${add}</span> <span class="del">−${del}</span>`;
  const { taux, parts } = tauxContribution(s, tot);
  const n = tot.members || 0;
  const f = facteurEffort(taux, n);
  const ideal = n ? Math.round(100 / n) : null;
  const tauxPct = taux == null ? "n/d" : Math.round(taux * 100) + " %";
  const synthese = `<div class="contrib-synth">
    <div class="cs-bloc">
      <div class="cs-titre">Taux de contribution</div>
      <div class="cs-val" title="${esc(tauxTip(parts))}">${tauxPct}</div>
      <div class="barre"><span style="width:${taux == null ? 0 : Math.round(taux * 100)}%"></span></div>
      <small class="aide">part du travail de l'équipe (survol = détail)</small>
    </div>
    <div class="cs-bloc">
      <div class="cs-titre">Facteur d'effort</div>
      <div class="cs-val">${badgeFacteur(f)}</div>
      <small class="aide">${ideal == null ? "effectif inconnu"
        : `part attendue ${ideal} % (équipe de ${n}) : atteinte → facteur 1, sinon proportionnel`}</small>
    </div>
  </div>`;
  return `<div class="panneau">
    ${synthese}
    <div class="qualite">
      ${kpiPart("lignes modifiées (PR)", lignesHtml, lignes, tot.lines)}
      ${kpiPart("PR ouvertes + mergées", String(prCount), prCount, tot.prs)}
      ${kpiPart("issues fermées", String(s.issues_closed || 0), s.issues_closed || 0, tot.issues_closed)}
      ${kpiPart("commits (branche défaut)", String(s.commits || 0), s.commits || 0, tot.commits)}
      ${kpiPart("tests validés", String(s.tests_validated || 0), s.tests_validated || 0, tot.tests_validated)}
    </div>
    <div class="frise-titre">Contribution par feature <small>part du code de production (src/main) de chaque écran · ${(s.feature_equivalents || 0).toFixed(2)} écran-équiv.</small></div>
    ${featuresEtudiant(s)}
    <div class="frise-titre" style="margin-top:.9rem">Pull requests <small>${(s.prs || []).length}</small></div>
    ${prListEtudiant(s)}
    ${blocActivite((window.__data && window.__data.activity && window.__data.activity.by_student || {})[s.login],
      "Activité individuelle (commits)")}
  </div>`;
}

function renderStudents(data) {
  const corps = document.getElementById("etudiants-corps");
  if (!corps) return;
  const totaux = totauxEquipes(data);
  const students = [...(data.students || [])].sort(compareStudents);
  const ctx = badgeCtx || contexteBadges(students);
  // taux de contribution + facteur d'effort, attaches pour permettre le tri.
  students.forEach(s => {
    const t = totaux[s.team];
    s.taux = tauxContribution(s, t).taux;
    s.facteur = facteurEffort(s.taux, t && t.members);
  });
  corps.innerHTML = "";
  if (!students.length) {
    corps.innerHTML = '<tr><td colspan="11">Aucun étudiant détecté.</td></tr>';
    return;
  }
  students.forEach((s, i) => {
    const bs = badgesEtudiant(s, ctx)
      .map(([e, t]) => `<span class="badge-emoji" title="${esc(t)}">${e}</span>`).join(" ");
    const tr = document.createElement("tr");
    tr.className = "etudiant";
    tr.id = idEtudiant(s.login);
    tr.innerHTML = `
      <td class="rang"><span class="rang-badge">${i + 1}</span></td>
      <td class="login"><span class="chevron">▶</span>${esc(s.login)}</td>
      <td><a class="lien-equipe" href="#${idEquipe(s.team)}" data-slug="${esc(s.team)}" title="Voir l'équipe ${esc(s.team)} dans le classement">${esc(s.team)}</a></td>
      <td class="num">${s.taux == null ? '<span class="badge nd">n/d</span>' : Math.round(s.taux * 100) + " %"}</td>
      <td class="num">${badgeFacteur(s.facteur)}</td>
      <td class="num">${s.branch_commits ?? 0}</td>
      <td class="num">${s.prs_open}</td>
      <td class="num">${s.prs_merged}</td>
      <td class="num">${s.reviews_given}</td>
      <td class="num"><span class="pastille ${s.review_quality}" title="${esc(voyantTip(s))}"></span></td>
      <td class="badges">${bs || "—"}</td>`;
    const detail = document.createElement("tr");
    detail.className = "detail";
    detail.hidden = true;
    detail.innerHTML = `<td colspan="11">${detailEtudiant(s, totaux[s.team])}</td>`;
    tr.addEventListener("click", e => {
      if (e.target.closest("a")) return;   // ne pas replier en cliquant un lien de PR
      detail.hidden = !detail.hidden;
      tr.querySelector(".chevron").textContent = detail.hidden ? "▶" : "▼";
    });
    corps.appendChild(tr);
    corps.appendChild(detail);
  });
}

function bindTri() {
  document.querySelectorAll("th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      currentSort = th.dataset.sort;
      renderTable(window.__data);
    });
  });
  document.querySelectorAll("th[data-esort]").forEach(th => {
    th.addEventListener("click", () => {
      currentESort = th.dataset.esort;
      renderStudents(window.__data);
    });
  });
}

// Deplie (si replie) une ligne, y fait defiler la page et la met en avant.
function activerLigne(row) {
  if (!row) return;
  const detail = row.nextElementSibling;
  if (detail && detail.classList.contains("detail") && detail.hidden) {
    detail.hidden = false;
    const ch = row.querySelector(".chevron");
    if (ch) ch.textContent = "▼";
  }
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.remove("cible");
  void row.offsetWidth;              // relance l'animation si deja ciblee
  row.classList.add("cible");
}

// Navigation croisee entre les deux classements (delegation : les lignes sont
// re-rendues a chaque tri, les id restent valables).
//   .lien-etudiant (panneau equipe) -> ligne du classement par etudiant
//   .lien-equipe   (table etudiant)  -> ligne du classement des equipes
function bindLiensNavigation() {
  document.addEventListener("click", e => {
    const aEtu = e.target.closest(".lien-etudiant");
    if (aEtu) {
      e.preventDefault();
      activerLigne(document.getElementById(idEtudiant(aEtu.dataset.login)));
      return;
    }
    const aEq = e.target.closest(".lien-equipe");
    if (aEq) {
      e.preventDefault();
      activerLigne(document.getElementById(idEquipe(aEq.dataset.slug)));
    }
  });
}

fetch("data.json", { cache: "no-store" })
  .then(r => r.json())
  .then(data => { window.__data = data; render(data); bindTri(); bindLiensNavigation(); })
  .catch(e => {
    document.getElementById("meta").textContent =
      "Erreur de chargement de data.json : " + e;
  });
