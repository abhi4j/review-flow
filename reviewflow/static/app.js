"use strict";

// All text from the API is inserted with textContent; links are only set for
// https://github.com/ URLs. No innerHTML anywhere.

const $ = (id) => document.getElementById(id);
const API = "/v1/repos";

const METRICS = [
  ["merged_prs", "Merged PRs", "neutral"],
  ["ttfr_p50_hours", "Wait for first review, median", "lower"],
  ["ttfr_p90_hours", "Wait for first review, p90", "lower"],
  ["review_to_merge_p50_hours", "First review to merge, median", "lower"],
  ["cycle_p50_hours", "Ready to merge, median", "lower"],
  ["cycle_p90_hours", "Ready to merge, p90", "lower"],
  ["merged_without_review_pct", "Merged without review", "lower"],
  ["pr_size_p50_lines", "PR size, median", "neutral"],
  ["active_reviewers", "Active reviewers", "higher"],
  ["pr_reviews", "PR reviews", "neutral"],
  ["top_reviewer_share_pct", "Busiest reviewer's share", "lower"],
  ["review_bus_factor", "Reviewers doing half the work", "higher"],
  ["bot_prs_excluded", "Bot PRs (excluded above)", "neutral"],
];

function unitOf(name) {
  if (name.endsWith("_hours")) return "hours";
  if (name.endsWith("_pct")) return "percent";
  if (name.endsWith("_lines")) return "lines";
  if (name.endsWith("_vs_overall")) return "ratio";
  return "count";
}

function fmt(value, unit) {
  if (value === null || value === undefined) return "–";
  switch (unit) {
    case "hours": return Math.abs(value) >= 48 ? `${(value / 24).toFixed(1)} days` : `${value.toFixed(1)}h`;
    case "percent": return `${Math.round(value * 10) / 10}%`;
    case "pp": return `${Math.round(value * 10) / 10} pp`;
    case "lines": return `${Math.round(value).toLocaleString()} lines`;
    case "ratio": return `${value.toFixed(1)}×`;
    default: return value.toLocaleString();
  }
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key === "className") node.className = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function githubLink(url, text) {
  return url.startsWith("https://github.com/")
    ? el("a", { href: url, rel: "noopener noreferrer", target: "_blank", text })
    : el("span", { text });
}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed with HTTP ${response.status}.`);
  return body;
}

// ---- Evidence linking: a JSON pointer from the API names the row that holds a number.

function sourceId(pointer) {
  const parts = pointer.split("/").filter(Boolean);
  if (["summary", "reviewers", "drivers"].includes(parts[0])) return `src-${parts[0]}-${parts[1]}`;
  return `src-${parts[0]}-${parts[1]}-${parts[2]}`;
}

function showSource(pointer) {
  const target = $(sourceId(pointer));
  if (!target) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" });
  target.classList.remove("flash");
  void target.offsetWidth; // restart the animation
  target.classList.add("flash");
  target.setAttribute("tabindex", "-1");
  target.focus({ preventScroll: true });
}

function refButton(reference) {
  const button = el("button", { type: "button", className: "ref", text: reference.display });
  button.title = reference.label ? `${reference.label}. Show where this comes from.` : "Show source";
  button.addEventListener("click", () => showSource(reference.source));
  return button;
}

// Splits a template like "Wait grew to {{summary.ttfr_p50_hours.current}}" into text and
// clickable numbers. The server has already verified every reference resolves.
function withReferences(template, references) {
  const fragment = document.createDocumentFragment();
  let last = 0;
  for (const match of template.matchAll(/\{\{\s*([^{}\s]+)\s*\}\}/g)) {
    fragment.append(template.slice(last, match.index));
    const reference = references[match[1]];
    fragment.append(reference ? refButton(reference) : match[0]);
    last = match.index + match[0].length;
  }
  fragment.append(template.slice(last));
  return fragment;
}

// ---- Report -----------------------------------------------------------------------

function signed(value, unit) {
  return `${value > 0 ? "+" : ""}${fmt(value, unit)}`;
}

function intervalTitle(ci, significant, unit) {
  if (!ci) return "No interval: too few samples, or none for this metric.";
  const range = `95% interval ${fmt(ci[0], unit)} to ${fmt(ci[1], unit)}`;
  return significant ? `${range}.` : `${range}: within noise.`;
}

// Only changes whose interval excludes zero are coloured; the rest read as noise.
function tone(delta, significant, polarity) {
  if (!significant || delta === 0 || polarity === "neutral") return "flat";
  return (polarity === "lower" ? delta < 0 : delta > 0) ? "better" : "worse";
}

function changeCell(name, comparison, polarity) {
  const { delta, delta_pct: pct, delta_ci: ci, significant } = comparison;
  if (delta === null) return el("td", { className: "flat", text: "–" });
  const unit = unitOf(name);
  let text;
  if (pct !== null && unit !== "percent") text = `${pct > 0 ? "+" : ""}${pct}%`;
  else if (unit === "percent") text = signed(delta, "pp");
  else text = signed(delta, unit);
  return el("td", { className: tone(delta, significant, polarity), text,
    title: intervalTitle(ci, significant, unit === "percent" ? "pp" : unit) });
}

function renderDrivers(drivers) {
  const rows = [["by_size", "By PR size"], ["by_author", "By author"]];
  $("drivers").replaceChildren(...rows.map(([key, label]) => {
    const split = drivers[key];
    const cell = (effect) => el("td", { className: tone(effect.hours, effect.significant, "lower"),
      text: signed(effect.hours, "hours"), title: intervalTitle(effect.ci, effect.significant, "hours") });
    return el("tr", { id: `src-drivers-${key}` }, el("td", { text: label }),
      ...(split ? [cell(split.mix), cell(split.within)]
        : [el("td", { className: "flat", text: "–", title: "Too few reviewed PRs." }),
           el("td", { className: "flat", text: "–" })]));
  }));
}

function renderMetrics(summary) {
  $("metrics").replaceChildren(...METRICS.map(([name, label, polarity]) => {
    const c = summary[name];
    return el("tr", { id: `src-summary-${name}` },
      el("td", { text: label }),
      el("td", { text: fmt(c.current, unitOf(name)) }),
      el("td", { text: fmt(c.previous, unitOf(name)) }),
      changeCell(name, c, polarity));
  }));
}

function renderReviewers(rows) {
  if (!rows.length) {
    $("reviewers").replaceChildren(el("li", { className: "empty", text: "No reviews in this period." }));
    return;
  }
  $("reviewers").replaceChildren(...rows.map((row, i) => {
    const share = row.share_pct;
    const now = el("span", { className: "bar-now" });
    now.style.width = `${share.current ?? 0}%`;
    const prev = el("span", { className: "bar-prev" });
    prev.style.left = `${share.previous ?? 0}%`;
    prev.hidden = share.previous === null;
    const count = row.prs_reviewed.current;
    const prs = count === 1 ? "1 PR" : `${count} PRs`;
    return el("li", { id: `src-reviewers-${i}` },
      githubLink(`https://github.com/${encodeURIComponent(row.login)}`, row.login),
      el("span", { className: "bar", title: `Previous period: ${fmt(share.previous, "percent")}` }, now, prev),
      el("span", { className: "reviewer-figures", text: `${prs}, ${fmt(share.current, "percent")}` }));
  }));
}

function renderSegments(id, group, rows) {
  $(id).replaceChildren(...rows.map((row, i) => el("tr", { id: `src-segments-${group}-${i}` },
    el("td", { text: row.label }),
    el("td", { text: row.merged_prs }),
    el("td", { text: fmt(row.ttfr_p50_hours, "hours") }),
    el("td", { text: fmt(row.ttfr_p50_vs_overall, "ratio") }))));
}

function renderExamples(id, group, rows, describe) {
  if (!rows.length) {
    $(id).replaceChildren(el("li", { className: "empty", text: "None in this period." }));
    return;
  }
  $(id).replaceChildren(...rows.map((pr, i) => el("li", { id: `src-examples-${group}-${i}` },
    githubLink(pr.url, `#${pr.number}`), " ",
    el("span", { className: "meta", text: describe(pr) }))));
}

function renderReport(insight) {
  const since = new Date(insight.period.since).toLocaleDateString();
  const until = new Date(insight.period.until).toLocaleDateString();
  $("period-caption").textContent =
    `${since} to ${until}, compared with the ${Math.round(insight.period.days)} days before. ` +
    "Bot-authored PRs are left out of flow metrics.";
  renderMetrics(insight.summary);
  renderDrivers(insight.drivers);
  renderReviewers(insight.reviewers);
  renderSegments("by-size", "by_size", insight.segments.by_size);
  renderSegments("by-author", "by_author", insight.segments.by_author);
  renderExamples("slowest", "slowest_first_review", insight.examples.slowest_first_review,
    (pr) => `waited ${fmt(pr.ttfr_hours, "hours")} for a first review, ${fmt(pr.size_lines, "lines")}, by ${pr.author}`);
  renderExamples("unreviewed", "merged_without_review", insight.examples.merged_without_review,
    (pr) => `${fmt(pr.size_lines, "lines")}, merged ${fmt(pr.cycle_hours, "hours")} after opening, by ${pr.author}`);
  $("as-of").textContent = `GitHub data as of ${new Date(insight.data_as_of).toLocaleString()}`;
  $("report").hidden = false;
}

// ---- Narrative --------------------------------------------------------------------

function evidenceButtons(evidence) {
  return evidence.map((item) => {
    const button = el("button", { type: "button", title: "Show where this comes from" },
      el("strong", { text: item.display }), " ", el("span", { text: item.label }));
    button.addEventListener("click", () => showSource(item.source));
    return el("li", {}, button);
  });
}

function renderNarrative(narrative) {
  const { template, references } = narrative;
  $("note-headline").replaceChildren(withReferences(template.headline, references));
  $("note-text").replaceChildren(withReferences(template.narrative, references));
  $("observations").replaceChildren(...narrative.observations.map((o) => el("li", { text: o.statement })));

  const hypothesis = narrative.hypothesis;
  $("hypothesis").hidden = !hypothesis;
  if (hypothesis) {
    const c = hypothesis.confidence;
    $("hyp-statement").textContent = hypothesis.statement;
    $("meter-fill").style.width = `${c.score * 100}%`;
    $("meter-cap").style.left = `${c.ceiling * 100}%`;
    $("confidence-text").textContent = c.score < c.model_reported
      ? `Confidence ${c.score.toFixed(2)}. The model said ${c.model_reported.toFixed(2)}; ` +
        `this sample supports at most ${c.ceiling.toFixed(2)}. ${c.basis}`
      : `Confidence ${c.score.toFixed(2)}, below the ${c.ceiling.toFixed(2)} ceiling for ` +
        `this sample. ${c.basis}`;
    $("evidence").replaceChildren(...evidenceButtons(hypothesis.evidence));
    $("alternatives").replaceChildren(...hypothesis.alternatives.map((a) => el("li", { text: a })));
    $("verify").textContent = hypothesis.how_to_verify;
  }
  $("note").hidden = false;
}

function renderNarrativeUnavailable(message) {
  $("note-headline").textContent = "No narrative for this period";
  $("note-text").textContent = message;
  $("observations").replaceChildren();
  $("hypothesis").hidden = true;
  $("note").hidden = false;
}

// ---- Flow -------------------------------------------------------------------------

let generation = 0;

function setStatus(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
  $("status").hidden = !text;
}

async function analyse(repo, days) {
  const mine = ++generation;
  const current = () => mine === generation;
  $("analyse").disabled = true;
  $("note").hidden = true;
  $("report").hidden = true;
  setStatus(`Loading ${repo}. The first time, this fetches its history from GitHub, ` +
    "which can take up to a minute for a busy repository.");

  // Pin the window to the hour so repeat views hit the server's narrative cache.
  const until = new Date();
  until.setUTCMinutes(0, 0, 0);
  const since = new Date(until.getTime() - days * 86_400_000);
  const query = new URLSearchParams({ since: since.toISOString(), until: until.toISOString() });
  const [owner, name] = repo.split("/");
  const base = `${API}/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/insights/review-flow`;

  try {
    const insight = await getJson(`${base}?${query}`);
    if (!current()) return;
    renderReport(insight);
    setStatus("Writing the narrative…");
    try {
      const narrative = await getJson(`${base}/narrative?${query}`);
      if (current()) renderNarrative(narrative);
    } catch (error) {
      if (current()) renderNarrativeUnavailable(error.message);
    }
    if (current()) setStatus("");
  } catch (error) {
    if (current()) setStatus(error.message, true);
  } finally {
    if (current()) $("analyse").disabled = false;
  }
}

$("query").addEventListener("submit", (event) => {
  event.preventDefault();
  const repo = $("repo").value.trim();
  const days = Number($("days").value);
  history.replaceState(null, "", `?${new URLSearchParams({ repo, days })}`);
  analyse(repo, days);
});

const initial = new URLSearchParams(location.search);
if (initial.get("repo")) {
  $("repo").value = initial.get("repo");
  if (["14", "30", "90"].includes(initial.get("days"))) $("days").value = initial.get("days");
  $("query").requestSubmit();
}
