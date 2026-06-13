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
  tests: t => t.tests.pct == null ? -1 : t.tests.pct,
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
  renderStats(data);
  renderAlertes(data);
  renderTable(data);
  renderStudents(data);
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

function argmax(arr) {           // indice du max d'un tableau
  let bi = 0;
  arr.forEach((v, i) => { if (v > arr[bi]) bi = i; });
  return bi;
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
  const prMerg = (data.teams || []).reduce((s, t) => s + ((t.review && t.review.merged_total) || 0), 0);
  const issDone = (data.teams || []).reduce((s, t) => s + ((t.issues && t.issues.done) || 0), 0);
  const contribs = Object.keys(a.by_student || {}).length;
  const equipesActives = Object.values(a.by_team || {}).filter(t => t.total > 0).length;
  const jourMaxKey = Object.keys(a.by_day).reduce((m, k) => a.by_day[k] > (a.by_day[m] || 0) ? k : m,
    Object.keys(a.by_day)[0]);
  const [jy, jm, jd] = jourMaxKey.split("-");
  const hMax = argmax(a.by_hour);
  const horsJour = a.by_hour.reduce((s, v, h) => s + ((h < 7 || h >= 20) ? v : 0), 0);
  const partSoir = Math.round(100 * horsJour / a.total);
  const weMax = a.by_weekday[5] + a.by_weekday[6];
  document.getElementById("stats-kpi").innerHTML = [
    skpi(a.total, "commits", "Total des commits horodatés (toutes branches, dédoublonnés)"),
    skpi(contribs, "contributeurs actifs"),
    skpi(equipesActives, "équipes"),
    skpi(prMerg, "PR mergées"),
    skpi(issDone, "issues fermées"),
    skpi(`${jd}/${jm}`, "jour le plus actif", `${jourMaxKey} : ${a.by_day[jourMaxKey]} commits`),
    skpi(`${hMax} h`, "heure de pointe", `${a.by_hour[hMax]} commits entre ${hMax} h et ${hMax + 1} h`),
    skpi(`${partSoir} %`, "en soirée / nuit (20 h–7 h)", `${horsJour} commits hors 7 h–20 h ; ${weMax} le week-end`),
  ].join("");

  // Diagrammes collectifs. La déclinaison par équipe / par personne vit
  // désormais dans les panneaux détaillés respectifs (detailPanneau / detailEtudiant).
  document.getElementById("stats-charts").innerHTML = blocCharts(a);
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

function renderTable(data) {
  const med = data.promo || {};
  const teams = [...data.teams].sort((a, b) => {
    const va = SORTERS[currentSort](a), vb = SORTERS[currentSort](b);
    if (typeof va === "string") return va.localeCompare(vb);
    return vb - va;
  });

  // Podium (sur le classement canonique = tests verts), independant du tri courant.
  const podium = {};
  [...data.teams].filter(t => t.tests.pct != null)
    .sort((a, b) => b.tests.pct - a.tests.pct || b.issues.done - a.issues.done
      || a.slug.localeCompare(b.slug))
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
    const delta = t.trend && t.trend.delta_7d;
    const deltaHtml = delta ? `<span class="delta pos" title="progression récente des tests verts">+${delta}</span>` : "";
    tr.innerHTML = `
      <td class="rang"><span class="rang-badge">${i + 1}</span></td>
      <td class="nom-equipe"><span class="chevron">▶</span>${podium[t.slug] ? `<span class="medaille" title="${esc(podium[t.slug][1])}">${podium[t.slug][0]}</span> ` : ""}${esc(t.name)}${t.repo_url ? ` <a class="lien-repo" href="${esc(t.repo_url)}" target="_blank" rel="noopener" title="Ouvrir le dépôt GitHub de l'équipe">↗ dépôt</a>` : ""}${sparkline(t.trend && t.trend.tests_series)}</td>
      <td class="num">
        ${bar(t.tests.passed || 0, t.tests.total || data.totals.tests_total, "tests", med.median_tests_pct)}
        <span class="barre-label">${t.tests.passed == null ? "n/d" : t.tests.passed + "/" + t.tests.total} (${pct(t.tests.pct)})${deltaHtml}</span>
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
    const tip = `${lbl} — ${tTests}${tTaches}` + (f.source === "issues" ? " (jauge : tâches)" : "");
    return `<div class="feat ${etat}" title="${esc(tip)}">
        <span class="feat-tete"><span class="emoji">${FEATURE_EMOJI[f.key] || "•"}</span> ${esc(lbl)} ${mos}</span>
        <span class="jauge"><span style="width:${p}%"></span></span>
        <span class="cpt">${f.done}/${f.total}</span>
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
  const contribs = t.contributors.map(c => `
    <tr>
      <td class="login"><a class="lien-etudiant" href="#${idEtudiant(c.login)}" data-login="${esc(c.login)}" title="Voir ${esc(c.login)} dans le classement par étudiant">${esc(c.login)}</a></td>
      <td class="num"><strong>${c.tests_validated ?? 0}</strong></td>
      <td class="num">${c.branch_commits ?? 0}</td>
      <td class="num">${c.prs_open}/${c.prs_merged}</td>
      <td class="num">${c.reviews_given}/${c.reviews_received}</td>
      <td class="num">${c.issues_closed}/${c.issues_assigned}</td>
      <td class="num"><span class="pastille ${c.review_quality}" title="${esc(voyantTip(c))}"></span></td>
      <td class="badges">${(badgeCtx ? badgesEtudiant({ ...c, team: t.slug }, badgeCtx) : [])
        .map(([e, tt]) => `<span class="badge-emoji" title="${esc(tt)}">${e}</span>`).join(" ") || "—"}</td>
    </tr>`).join("");

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
    </div>
    <table class="contribs">
      <thead><tr>
        <th>Contributeur (login GitHub)</th>
        <th class="num" title="Tests verts apportés par les PR mergées de l'étudiant">Tests validés</th>
        <th class="num" title="Commits dans des branches non encore mergées (travail en cours)">Travail en cours</th>
        <th class="num" title="PR actuellement ouvertes (en cours) / PR mergées">PR en cours/merg.</th><th class="num">Revues don./rec.</th>
        <th class="num">Issues fer./assig.</th><th class="num">Revue</th><th>Badges</th>
      </tr></thead>
      <tbody>${contribs || '<tr><td colspan="8">Aucun contributeur détecté.</td></tr>'}</tbody>
    </table>
    ${blocActivite((window.__data && window.__data.activity && window.__data.activity.by_team || {})[t.slug],
      "Activité de l'équipe (commits)")}
  </div>`;
}

// Bloc activite (titre + 3 diagrammes) pour un panneau detaille, ou rien si
// aucune donnee d'activite (ex. equipe de reference synthetique).
function blocActivite(src, titre) {
  if (!src || !src.total) return "";
  return `<div class="frise-titre" style="margin-top:1rem">${esc(titre)} <small>${src.total} commits</small></div>`
    + blocCharts(src);
}

// --- classement par etudiant (toutes equipes confondues) ------------------
// Tri par defaut : PR mergees (travail livre/relu) > issues fermees > commits.
// Le nombre de commits seul n'est pas fiable si les PR ne sont pas squashees.
const ESTRING = new Set(["login", "team"]);
const ETIEBREAK = ["tests_validated", "prs_merged", "issues_closed", "branch_commits", "commits"];
let currentESort = "tests_validated";

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

// Contexte pour les badges : maxima de la promo + equipes ayant au moins un actif.
function contexteBadges(students) {
  const max = k => Math.max(0, ...students.map(s => s[k] || 0));
  const actif = s => (s.commits + s.prs_merged + s.reviews_given + (s.branch_commits || 0)) > 0;
  const actifsParEquipe = {};
  students.filter(actif).forEach(s => {
    actifsParEquipe[s.team] = (actifsParEquipe[s.team] || 0) + 1;
  });
  return {
    commits: max("commits"), reviews_given: max("reviews_given"),
    prs_merged: max("prs_merged"), inline_comments: max("inline_comments"),
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
  // qualitatifs (cumulables)
  if (s.commits > 0 && s.prs_merged > 0 && s.reviews_given > 0) b.push(["🐝", "Couteau suisse : actif sur le code, les PR et les revues"]);
  if (s.reviews_given > 0 && s.reviews_received > 0 && Math.abs(s.reviews_given - s.reviews_received) <= 1)
    b.push(["🤝", "Fair-play : relit autant qu'il est relu"]);
  if (s.review_quality === "green" && s.changes_requested >= 1) b.push(["🧐", "Œil de lynx : vraies revues, demande des changements"]);
  if (s.review_quality === "red") b.push(["🦆", "Tampon : approbations à vide"]);
  if ((s.commits + s.prs_merged + s.reviews_given + (s.branch_commits || 0)) === 0 && (c.actifsParEquipe[s.team] || 0) >= 1)
    b.push(["👻", "Passager clandestin : aucune contribution (ni mergée, ni en cours dans une branche) alors qu'au moins un coéquipier travaille"]);
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
      prs: som(c => (c.prs_open || 0) + (c.prs_merged || 0)),
      prs_merged: som(c => c.prs_merged),
      issues_closed: som(c => c.issues_closed),
      commits: som(c => c.commits),
      tests_validated: som(c => c.tests_validated),
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
const CONTRIB_DIMS = [
  { cle: "tests", label: "tests validés", poids: 0.30, tot: "tests_validated", val: s => s.tests_validated || 0 },
  { cle: "prm", label: "PR mergées", poids: 0.25, tot: "prs_merged", val: s => s.prs_merged || 0 },
  { cle: "iss", label: "issues fermées", poids: 0.15, tot: "issues_closed", val: s => s.issues_closed || 0 },
  { cle: "lines", label: "lignes modifiées", poids: 0.15, tot: "lines", val: s => (s.lines_added || 0) + (s.lines_deleted || 0) },
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

// Infobulle de ventilation du taux (dimension : poids renormalise x part).
function tauxTip(parts) {
  if (!parts.length) return "Aucune dimension mesurable dans l'équipe.";
  return parts.map(p =>
    `${p.label} : ${Math.round(p.poids * 100)}% × ${Math.round(p.part * 100)}% = ${Math.round(p.apport * 100)}%`
  ).join(" | ");
}

function featuresEtudiant(s) {
  const fs = s.features || [];
  if (!fs.length)
    return `<p class="aide">Aucune feature identifiée (ni issue assignée préfixée <code>[feature]</code>, ni PR taguée).</p>`;
  const chips = fs.map(k =>
    `<span class="feat-chip"><span class="emoji">${FEATURE_EMOJI[k] || "•"}</span> ${esc(FEATURE_LABEL[k] || k)}</span>`).join(" ");
  return `<div class="feat-chips">${chips}</div>`;
}

function prListEtudiant(s) {
  const prs = s.prs || [];
  if (!prs.length) return `<p class="aide">Aucune pull request (ouverte ou mergée).</p>`;
  const rows = prs.map(p => {
    const etat = p.merged ? `<span class="badge ok">mergée</span>`
      : (p.state === "OPEN" ? `<span class="badge nd">ouverte</span>` : `<span class="badge ko">fermée</span>`);
    return `<tr>
      <td><a href="${esc(p.url)}" target="_blank" rel="noopener">#${p.number} ${esc(p.title)} ↗</a></td>
      <td class="num">${etat}</td>
      <td class="num diff"><span class="add">+${p.additions}</span> <span class="del">−${p.deletions}</span></td>
    </tr>`;
  }).join("");
  return `<table class="contribs prs">
    <thead><tr><th>Pull request</th><th class="num">État</th><th class="num">Lignes</th></tr></thead>
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
        : `idéal ${ideal} % pour ${n} membres · min(1, taux ÷ idéal)`}</small>
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
    <div class="frise-titre">Features touchées <small>${(s.features || []).length}/8</small></div>
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
    corps.innerHTML = '<tr><td colspan="12">Aucun étudiant détecté.</td></tr>';
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
      <td class="num"><strong>${s.tests_validated}</strong></td>
      <td class="num">${s.branch_commits ?? 0}</td>
      <td class="num">${s.prs_open}</td>
      <td class="num">${s.prs_merged}</td>
      <td class="num">${s.reviews_given}</td>
      <td class="num"><span class="pastille ${s.review_quality}" title="${esc(voyantTip(s))}"></span></td>
      <td class="badges">${bs || "—"}</td>`;
    const detail = document.createElement("tr");
    detail.className = "detail";
    detail.hidden = true;
    detail.innerHTML = `<td colspan="12">${detailEtudiant(s, totaux[s.team])}</td>`;
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
