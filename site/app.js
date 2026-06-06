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

function render(data) {
  document.getElementById("meta").textContent =
    `Généré le ${new Date(data.generated_at).toLocaleString("fr-FR")} `
    + `- ${data.teams.length} équipes - total ${data.totals.tests_total} tests, `
    + `${data.totals.issues_total} tâches.`;

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

  const corps = document.getElementById("classement-corps");
  corps.innerHTML = "";
  teams.forEach((t, i) => {
    const tr = document.createElement("tr");
    tr.className = "equipe";
    const delta = t.trend && t.trend.delta_7d;
    const deltaHtml = delta ? `<span class="delta pos">+${delta} / 7j</span>` : "";
    tr.innerHTML = `
      <td class="rang"><span class="rang-badge">${i + 1}</span></td>
      <td class="nom-equipe"><span class="chevron">▶</span>${esc(t.name)}${sparkline(t.trend && t.trend.tests_series)}</td>
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

function detailPanneau(t) {
  const q = t.quality, b = t.bus_factor, r = t.review;
  const bus = Math.round((b.top_share_commits || 0) * 100);
  const contribs = t.contributors.map(c => `
    <tr>
      <td class="login">${esc(c.login)}</td>
      <td class="num">${c.commits}</td>
      <td class="num">${c.prs_open}/${c.prs_merged}</td>
      <td class="num">${c.reviews_given}/${c.reviews_received}</td>
      <td class="num">${c.issues_closed}/${c.issues_assigned}</td>
      <td class="num"><span class="pastille ${c.review_quality}" title="${esc(voyantTip(c))}"></span></td>
    </tr>`).join("");

  return `<div class="panneau">
    <div class="qualite">
      <div class="kpi"><b>${q.coverage_pct == null ? "n/d" : q.coverage_pct + " %"}</b><small>couverture JaCoCo</small></div>
      <div class="kpi"><b>${q.pmd_violations == null ? "n/d" : q.pmd_violations}</b><small>violations PMD</small></div>
      <div class="kpi"><b>${okBadge(q.spotless_ok)}</b><small>Spotless</small></div>
      <div class="kpi"><b>${okBadge(q.archunit_ok)}</b><small>ArchUnit (MVVM)</small></div>
      <div class="kpi"><b>${r.merged_total}</b><small>PR mergées (${r.self_merges} sans revue)</small></div>
      <div class="kpi"><b><span class="busbar"><span style="width:${bus}%"></span></span> ${bus} %</b>
        <small>bus factor (${b.active_members}/${b.members} actifs)</small></div>
    </div>
    <table class="contribs">
      <thead><tr>
        <th>Contributeur (login GitHub)</th><th class="num">Commits</th>
        <th class="num">PR ouv./merg.</th><th class="num">Revues don./rec.</th>
        <th class="num">Issues fer./assig.</th><th class="num">Revue</th>
      </tr></thead>
      <tbody>${contribs || '<tr><td colspan="6">Aucun contributeur détecté.</td></tr>'}</tbody>
    </table>
  </div>`;
}

// --- classement par etudiant (toutes equipes confondues) ------------------
const ESORTERS = {
  login: s => s.login,
  team: s => s.team,
  commits: s => s.commits,
  prs_merged: s => s.prs_merged,
  reviews_given: s => s.reviews_given,
  issues_closed: s => s.issues_closed,
};
let currentESort = "commits";

// Contexte pour les badges : maxima de la promo + equipes ayant au moins un actif.
function contexteBadges(students) {
  const max = k => Math.max(0, ...students.map(s => s[k] || 0));
  const actif = s => (s.commits + s.prs_merged + s.reviews_given) > 0;
  return {
    commits: max("commits"), reviews_given: max("reviews_given"),
    prs_merged: max("prs_merged"), inline_comments: max("inline_comments"),
    equipesActives: new Set(students.filter(actif).map(s => s.team)),
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
  if ((s.commits + s.prs_merged + s.reviews_given) === 0 && c.equipesActives.has(s.team))
    b.push(["🏄", "Passager clandestin : aucune contribution alors que des coéquipiers sont actifs"]);
  return b;
}

function renderStudents(data) {
  const corps = document.getElementById("etudiants-corps");
  if (!corps) return;
  const students = [...(data.students || [])].sort((a, b) => {
    const va = ESORTERS[currentESort](a), vb = ESORTERS[currentESort](b);
    if (typeof va === "string") return va.localeCompare(vb);
    return vb - va;
  });
  const ctx = contexteBadges(students);
  corps.innerHTML = students.map((s, i) => {
    const bs = badgesEtudiant(s, ctx)
      .map(([e, t]) => `<span class="badge-emoji" title="${esc(t)}">${e}</span>`).join(" ");
    return `
    <tr>
      <td class="rang"><span class="rang-badge">${i + 1}</span></td>
      <td class="login">${esc(s.login)}</td>
      <td>${esc(s.team)}</td>
      <td class="num">${s.commits}</td>
      <td class="num">${s.prs_merged}</td>
      <td class="num">${s.reviews_given}</td>
      <td class="num">${s.issues_closed}</td>
      <td class="num"><span class="pastille ${s.review_quality}" title="${esc(voyantTip(s))}"></span></td>
      <td class="badges">${bs || "—"}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="9">Aucun étudiant détecté.</td></tr>';
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

fetch("data.json", { cache: "no-store" })
  .then(r => r.json())
  .then(data => { window.__data = data; render(data); bindTri(); })
  .catch(e => {
    document.getElementById("meta").textContent =
      "Erreur de chargement de data.json : " + e;
  });
