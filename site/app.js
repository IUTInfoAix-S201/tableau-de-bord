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
  renderAlertes(data);
  renderTable(data);
  renderStudents(data);
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
    const delta = t.trend && t.trend.delta_7d;
    const deltaHtml = delta ? `<span class="delta pos" title="progression récente des tests verts">+${delta}</span>` : "";
    tr.innerHTML = `
      <td class="rang"><span class="rang-badge">${i + 1}</span></td>
      <td class="nom-equipe"><span class="chevron">▶</span>${podium[t.slug] ? `<span class="medaille" title="${esc(podium[t.slug][1])}">${podium[t.slug][0]}</span> ` : ""}${esc(t.name)}${sparkline(t.trend && t.trend.tests_series)}</td>
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
    tr.addEventListener("click", () => {
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
  </div>`;
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
      issues_closed: som(c => c.issues_closed),
      commits: som(c => c.commits),
      tests_validated: som(c => c.tests_validated),
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
  return `<div class="panneau">
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
  </div>`;
}

function renderStudents(data) {
  const corps = document.getElementById("etudiants-corps");
  if (!corps) return;
  const totaux = totauxEquipes(data);
  const students = [...(data.students || [])].sort(compareStudents);
  const ctx = badgeCtx || contexteBadges(students);
  corps.innerHTML = "";
  if (!students.length) {
    corps.innerHTML = '<tr><td colspan="10">Aucun étudiant détecté.</td></tr>';
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
      <td>${esc(s.team)}</td>
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
    detail.innerHTML = `<td colspan="10">${detailEtudiant(s, totaux[s.team])}</td>`;
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

// Clic sur un login dans un panneau equipe -> deplie et met en avant la ligne
// correspondante du classement par etudiant. Delegation (les lignes sont
// re-rendues a chaque tri, l' id reste valable). Une seule direction : equipe -> etudiant.
function bindLiensEtudiant() {
  document.addEventListener("click", e => {
    const a = e.target.closest(".lien-etudiant");
    if (!a) return;
    e.preventDefault();
    const row = document.getElementById(idEtudiant(a.dataset.login));
    if (!row) return;
    const detail = row.nextElementSibling;
    if (detail && detail.classList.contains("detail") && detail.hidden) {
      detail.hidden = false;
      const ch = row.querySelector(".chevron");
      if (ch) ch.textContent = "▼";
    }
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.remove("cible");
    void row.offsetWidth;            // relance l'animation si deja ciblee
    row.classList.add("cible");
  });
}

fetch("data.json", { cache: "no-store" })
  .then(r => r.json())
  .then(data => { window.__data = data; render(data); bindTri(); bindLiensEtudiant(); })
  .catch(e => {
    document.getElementById("meta").textContent =
      "Erreur de chargement de data.json : " + e;
  });
