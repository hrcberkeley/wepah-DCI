// main.js — UI controller & progressive disclosure state machine (AGENTS.md §5)
//
// Pedagogical rules honored here:
//   • No technical jargon anywhere in the rendered interface.
//   • Single search bar + three intent pills; nothing else to configure.
//   • No raw similarity scores on screen — provenance becomes badge language.
//   • Every claim is grounded: verifier.js gates all rendered statements.

import * as db from './db_client.js';
import { CONFIG } from './db_client.js';
import * as verifier from './verifier.js';
import * as pdfStage from './pdf_stage.js';
import * as answerEngine from './answer_engine.js';
import * as reranker from './reranker.js';
import { DEFAULT_REPO, parseRepository, validateDossier, loadDossier } from './dossier.js';

// §5.2 The Pedagogical Mode Switcher
const MODES = {
  exact: {
    placeholder: 'Search names, dates, statute numbers, or exact phrases...',
    helper: 'Finds documents containing your exact words, just like Ctrl+F across every file at once.',
    busy: 'Searching the record…',
  },
  concepts: {
    placeholder: 'Describe a topic or event (e.g., notice of structural defects)...',
    helper: 'Finds passages with similar meaning, even if the document uses different wording.',
    busy: 'Reading the record for related passages…',
  },
  dossier: {
    placeholder: 'Ask a specific question to get a cited summary...',
    helper: 'Synthesizes an answer strictly from matching passages. Click any sentence to inspect the original page.',
    busy: 'Preparing a cited summary from the record…',
  },
};

const state = {
  mode: 'exact',
  query: '',
  passages: new Map(),   // passage_id -> row (for citation chips)
  activeCard: null,
  dossier: null,
  repository: DEFAULT_REPO,
  searching: false,
  switching: false,
  lastRows: [],          // the passages of the latest search, for "Timeline" / "Laws cited" of these results
  lastQuery: '',
};

const el = {
  pills: null, input: null, button: null, helper: null, status: null,
  stream: null, summary: null, count: null, expand: null,
};

function $(id) { return document.getElementById(id); }

// ---------- boot ----------
async function boot() {
  el.pills = document.querySelectorAll('.pill');
  el.input = $('search-input');
  el.button = $('search-button');
  el.helper = $('helper-copy');
  el.status = $('status-bar');
  el.stream = $('evidence-stream');
  el.summary = $('dossier-summary');
  el.count = $('results-count');
  el.expand = $('expand-header');
  const dossierToggle = $('dossier-toggle');
  const dossierPanel = $('dossier-panel');
  const dossierError = $('dossier-error');
  const showPanel = (show) => {
    dossierPanel.hidden = !show;
    dossierToggle.setAttribute('aria-expanded', String(show));
    if (show) {
      const bottom = dossierToggle.getBoundingClientRect().bottom + 8;
      dossierPanel.style.setProperty('--dossier-panel-top', `${Math.max(12, Math.min(bottom, window.innerHeight - 290))}px`);
      $('dossier-url').focus();
    }
  };
  dossierToggle.addEventListener('click', () => showPanel(dossierPanel.hidden));
  $('dossier-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.searching) return;
    state.switching = true;
    dossierError.hidden = true;
    const submit = $('dossier-form').querySelector('button[type="submit"]');
    submit.disabled = true;
    el.button.disabled = true;
    try {
      const repo = parseRepository($('dossier-url').value);
      const manifest = await loadDossier(repo);
      await db.openDossier(repo, manifest);
      selectDossier(repo, manifest);
      showPanel(false);
    } catch (error) {
      dossierError.textContent = error.message || 'Could not open this dossier.';
      dossierError.hidden = false;
    } finally { submit.disabled = false; el.button.disabled = false; state.switching = false; }
  });
  $('dossier-default').addEventListener('click', () => { $('dossier-url').value = DEFAULT_REPO; $('dossier-form').requestSubmit(); });
  try {
    state.dossier = CONFIG.HF_DATASET_REPO
      ? await loadDossier(CONFIG.HF_DATASET_REPO)
      : validateDossier(await (await fetch('./dist_data/dossier.json')).json());
    updateDossierHeading();
  }
  catch (error) {
    console.error('Default dossier details unavailable:', error);
    setStatus('The dossier details could not be opened. Please reload and try again.', false);
    el.button.disabled = true;
    return;
  }

  el.pills.forEach((pill) => {
    pill.addEventListener('click', () => setMode(pill.dataset.mode));
  });
  el.button.addEventListener('click', () => runSearch());
  el.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runSearch();
    // Escape folds the compact strip back into the full search cover.
    if (e.key === 'Escape' && document.body.classList.contains('compact')) {
      setCompact(false);
      el.input.select();
    }
  });
  el.expand.addEventListener('click', () => { setCompact(false); el.input.focus(); });
  $('home-link').addEventListener('click', (e) => { e.preventDefault(); goHome(); });
  $('browse-timeline').addEventListener('click', () => browse('timeline', null));
  $('browse-laws').addEventListener('click', () => browse('laws', null));
  $('browse-people').addEventListener('click', () => browse('people', null));
  $('browse-orgs').addEventListener('click', () => browse('orgs', null));
  document.querySelectorAll('.view-option').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.view === 'passages') { renderEvidenceCards(state.lastRows, state.mode, state.lastQuery); setView('passages'); }
    else browse(b.dataset.view, state.lastRows.map((r) => r.passage_id));
  }));

  try {
    await db.initEngine(state.dossier);
  } catch (err) {
    console.error(err);
    setStatus('The record index could not be opened. Please make sure the data folder is being served and reload.', false);
    el.button.disabled = true;
    return;
  }
  updateBrowseLinks();
  el.input.focus();
}

function updateBrowseLinks() {
  $('browse-links').hidden = !db.hasEntities();
  for (const id of ['browse-people', 'browse-orgs']) $(id).hidden = !db.hasNames();
  document.querySelectorAll('.view-option[data-view="people"], .view-option[data-view="orgs"]').forEach((b) => { b.hidden = !db.hasNames(); });
}

function updateDossierHeading() {
  if (!state.dossier) return;
  $('dossier-name').textContent = state.dossier.title;
  $('dossier-toggle').title = state.dossier.title;
  document.querySelector('.masthead-facts').textContent = `${state.dossier.description || state.dossier.title} · ${state.dossier.document_count.toLocaleString()} documents · ${state.dossier.passage_count.toLocaleString()} passages`;
}

function selectDossier(repo, manifest) {
  state.repository = repo;
  state.dossier = manifest;
  state.passages.clear();
  state.activeCard = null;
  state.query = '';
  el.input.value = '';
  el.summary.hidden = true;
  el.count.hidden = true;
  el.stream.innerHTML = '<div class="empty-state"><p class="empty-line">This dossier is ready.</p><p class="empty-hint">Search above to inspect its documents.</p></div>';
  pdfStage.reset();
  updateDossierHeading();
  updateBrowseLinks();
  setView(null);
  setCompact(false);
  setStatus(`${manifest.title} is ready — ${manifest.document_count.toLocaleString()} documents.`);
}

function setMode(mode) {
  if (!MODES[mode]) return;
  state.mode = mode;
  el.pills.forEach((p) => {
    const active = p.dataset.mode === mode;
    p.classList.toggle('active', active);
    p.setAttribute('aria-selected', String(active));
  });
  document.getElementById('experimental-warning').hidden = mode !== 'dossier';
  el.input.placeholder = MODES[mode].placeholder;
  el.helper.innerHTML = `<em>${MODES[mode].helper}</em>`;
  el.summary.hidden = true;
  el.input.focus();
}

function setStatus(msg, busy = false) {
  el.status.textContent = msg || '';
  el.status.classList.toggle('busy', busy);
}

/**
 * The essay cover folds into a slim sticky strip once the first search runs,
 * giving the results and the original pages nearly the whole window.
 */
function setCompact(on) {
  document.body.classList.toggle('compact', on);
  el.expand.setAttribute('aria-expanded', String(!on));
}

// ---------- search dispatch ----------
async function runSearch() {
  const query = el.input.value.trim();
  if (!query) { el.input.focus(); return; }
  if (!db.isReady()) return;
  if (state.searching || state.switching) return;

  state.searching = true;
  $('dossier-toggle').disabled = true;
  $('dossier-panel').hidden = true;
  $('dossier-toggle').setAttribute('aria-expanded', 'false');
  state.query = query;
  setCompact(true);
  el.button.disabled = true;
  el.summary.hidden = true;
  setStatus(MODES[state.mode].busy, true);
  renderStreamLoading();

  try {
    if (state.mode === 'exact') {
      const rows = await db.keywordSearch(query);
      renderEvidenceCards(rows, 'exact', query);
      rememberResults(rows, query);
      setStatus(rows.length
        ? `Found ${rows.length} passage${rows.length === 1 ? '' : 's'} containing your exact words.`
        : 'No passages contain those exact words. Try fewer words, or switch to “Explore Concepts”.', false);
    } else if (state.mode === 'concepts') {
      const rows = await db.conceptSearch(query);
      renderEvidenceCards(rows, 'concepts', query);
      rememberResults(rows, query);
      setStatus(rows.length
        ? `Found ${rows.length} related passage${rows.length === 1 ? '' : 's'}. Click a card to see the original page.`
        : 'Nothing closely related yet. Try describing the topic with different words.', false);
    } else {
      setView(null);
      await startDossier(query);
    }
  } catch (err) {
    console.error(err);
    renderStreamError();
    setStatus('Something went wrong while searching. Please try again.', false);
  } finally {
    el.button.disabled = false;
    state.searching = false;
    $('dossier-toggle').disabled = false;
  }
}

// ---------- evidence entries (§5.3) ----------
function badgeFor(row, mode) {
  if (row.content_type === 'visual_evidence') {
    return { cls: 'tag-visual', text: 'Figure · Stamp' };
  }
  if (mode === 'exact') return { cls: 'tag-exact', text: 'Exact word match' };
  if (mode === 'concepts') {
    return { cls: 'tag-related', text: 'Related context' };
  }
  return { cls: 'tag-related', text: 'Related context' };
}

function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

/** Highlight the same literal phrase that the database matched. */
function highlightExactPhrase(text, query) {
  if (!query) return escapeHtml(text);
  const re = new RegExp(escapeRegExp(query), 'gi');
  let html = '';
  let start = 0;
  for (const hit of text.matchAll(re)) {
    html += escapeHtml(text.slice(start, hit.index));
    html += `<mark class="exact-hit">${escapeHtml(hit[0])}</mark>`;
    start = hit.index + hit[0].length;
  }
  return html + escapeHtml(text.slice(start));
}

function excerpt(text, max = 700) {
  const t = String(text ?? '');
  return t.length > max ? `${t.slice(0, max)}…` : t;
}

function exactExcerpt(text, query, max = 700) {
  const full = String(text ?? '');
  if (full.length <= max) return full;
  const hit = full.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
  if (hit < 0) return excerpt(full, max);
  const start = Math.max(0, Math.min(hit - Math.floor((max - query.length) / 2), full.length - max));
  return `${start ? '…' : ''}${full.slice(start, start + max)}${start + max < full.length ? '…' : ''}`;
}

function renderStreamLoading() {
  el.count.hidden = true;
  el.stream.innerHTML = Array.from({ length: 3 }, () => `
    <div class="skeleton" aria-hidden="true">
      <div class="bar" style="width: 42%"></div>
      <div class="bar" style="width: 94%"></div>
      <div class="bar" style="width: 78%"></div>
      <div class="bar" style="width: 61%"></div>
    </div>`).join('');
}

function renderStreamError() {
  el.stream.innerHTML = `
    <div class="empty-state">
      <p class="empty-line">Something went wrong while searching.</p>
      <p class="empty-hint">Please try again — the record hasn't changed.</p>
    </div>`;
}

function renderEvidenceCards(rows, mode, query) {
  state.passages.clear();
  rows.forEach((r) => state.passages.set(r.passage_id, r));

  el.count.hidden = false;
  el.count.innerHTML = mode === 'dossier'
    ? `<b>${rows.length} source${rows.length === 1 ? '' : 's'}</b> · “${escapeHtml(query)}”`
    : `<b>${rows.length} passage${rows.length === 1 ? '' : 's'}</b> · “${escapeHtml(query)}”`;

  if (!rows.length) {
    el.stream.innerHTML = `
      <div class="empty-state">
        <p class="empty-line">No matching passages.</p>
        <p class="empty-hint">Try different words, or switch search modes on the cover.</p>
      </div>`;
    return;
  }

  el.stream.innerHTML = '';
  for (const [idx, row] of rows.entries()) {
    const badge = badgeFor(row, mode);
    const card = document.createElement('article');
    card.className = 'entry';
    card.dataset.passageId = row.passage_id;
    card.style.setProperty('--i', idx); // stagger for the rise + highlighter swipe

    const quoteHtml = mode === 'exact'
      ? highlightExactPhrase(exactExcerpt(row.passage_text, query), query)
      : escapeHtml(excerpt(row.passage_text));

    card.innerHTML = `
      <header class="entry-head">
        <h3 class="entry-doc" title="${escapeHtml(row.doc_title)}">${escapeHtml(row.doc_title)}</h3>
        <span class="entry-page">p. ${row.page_number}</span>
        <span class="entry-tag ${badge.cls}">${badge.text}</span>
      </header>
      <p class="entry-passage">${quoteHtml}</p>
      <p class="entry-meta" title="${escapeHtml(`${row.doc_date} · ${row.doc_id}.pdf`)}">${escapeHtml(row.doc_date)} · ${escapeHtml(row.doc_id)}.pdf</p>
    `;

    card.addEventListener('click', () => {
      if (state.activeCard) state.activeCard.classList.remove('active');
      state.activeCard = card;
      card.classList.add('active');
      openGrounding(row);
    });

    el.stream.appendChild(card);
  }
}

function openGrounding(row) {
  pdfStage.open({
    docId: row.doc_id,
    docTitle: row.doc_title,
    pageNumber: row.page_number,
    bbox: {
      x: row.bbox_x_pct, y: row.bbox_y_pct,
      w: row.bbox_w_pct, h: row.bbox_h_pct,
    },
  }).catch((err) => {
    console.error(err);
    setStatus('The original page could not be opened.', false);
  });
}

// ---------- Timeline & laws cited ----------
// Both read small tables built from the passage text (ingestion/entities.py): every
// date and citation shown is the exact wording on the page, so no checking step can fail.

function rememberResults(rows, query) {
  state.lastRows = rows;
  state.lastQuery = query;
  setView(rows.length && db.hasEntities() ? 'passages' : null);
}

/** The Passages / Timeline / Laws cited switch under the results count; null hides it. */
function setView(view) {
  const sw = $('view-switch');
  sw.hidden = !view;
  sw.querySelectorAll('.view-option').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
}

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
function formatDate(iso, precision) {
  const [y, m, d] = iso.split('-').map(Number);
  return precision === 'day' ? `${MONTH_NAMES[m - 1]} ${d}, ${y}` : `${MONTH_NAMES[m - 1]} ${y}`;
}

async function browse(view, passageIds) {
  if (state.searching || state.switching || !db.isReady()) return;
  const scoped = Array.isArray(passageIds);
  state.searching = true;
  setCompact(true);
  el.summary.hidden = true;
  if (!scoped) { setView(null); state.lastRows = []; }
  else setView(view);
  renderStreamLoading();
  setStatus({ timeline: 'Putting the dated passages in order…', laws: 'Gathering the laws and cases cited…',
    people: 'Gathering the people named…', orgs: 'Gathering the organizations named…' }[view], true);
  try {
    if (view === 'timeline') {
      renderTimeline(await db.timeline(passageIds), scoped);
    } else {
      if (view === 'laws') renderLawIndex(await db.authorityIndex(passageIds), passageIds);
      else renderNameIndex(view, await db.nameIndex(view, passageIds), passageIds);
    }
    setStatus('Every date and citation is quoted exactly as written — click one to see its page.', false);
  } catch (err) {
    console.error(err);
    renderStreamError();
    setStatus('This view could not be opened. Please try again.', false);
  } finally { state.searching = false; }
}

/** Back to the start: the full cover and an empty results column. */
function goHome() {
  if (state.searching) return;
  state.activeCard = null;
  el.summary.hidden = true;
  el.count.hidden = true;
  setView(null);
  el.stream.innerHTML = '<div class="empty-state"><p class="empty-line">Matching passages will collect here.</p><p class="empty-hint">Search above &mdash; each result opens its original page, with the exact words highlighted.</p></div>';
  setStatus('');
  setCompact(false);
  window.scrollTo(0, 0);
  el.input.focus();
}

/** "← Start" for whole-dossier views, "← Results" for views of a search. */
function backLink(scoped) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'back-link';
  b.textContent = scoped ? '← Results' : '← Start';
  b.addEventListener('click', () => {
    if (!scoped) return goHome();
    renderEvidenceCards(state.lastRows, state.mode, state.lastQuery);
    setView('passages');
  });
  el.count.prepend(b, ' ');
}

function scopeLabel(scoped) {
  return scoped ? `in these results · “${escapeHtml(state.lastQuery)}”` : 'whole dossier';
}

function placeEntry(row, idx, { head, tag, quoteHtml, meta }) {
  const card = document.createElement('article');
  card.className = 'entry';
  card.style.setProperty('--i', Math.min(idx, 20));
  card.innerHTML = `
    <header class="entry-head">
      ${head}
      <span class="entry-page">p. ${row.page_number}</span>
      ${tag ? `<span class="entry-tag tag-related">${tag}</span>` : ''}
    </header>
    <p class="entry-passage">${quoteHtml}</p>
    ${meta ? `<p class="entry-meta">${meta}</p>` : ''}`;
  card.addEventListener('click', () => {
    if (state.activeCard) state.activeCard.classList.remove('active');
    state.activeCard = card;
    card.classList.add('active');
    openGrounding(row);
  });
  return card;
}

function emptyView(line, hint) {
  el.stream.innerHTML = `<div class="empty-state"><p class="empty-line">${line}</p><p class="empty-hint">${hint}</p></div>`;
}

function renderTimeline(events, scoped) {
  el.count.hidden = false;
  el.count.innerHTML = `<b>${events.length}${events.length >= 5000 ? '+' : ''} date${events.length === 1 ? '' : 's'}</b> · ${scopeLabel(scoped)}`;
  backLink(scoped);
  if (!events.length) return emptyView('No dates appear in these passages.', 'Try “Explore Concepts” with a wider description, or open the whole-dossier timeline from the cover.');
  el.stream.innerHTML = '';
  let year = '';
  for (const [idx, ev] of events.entries()) {
    if (ev.date.slice(0, 4) !== year) {
      year = ev.date.slice(0, 4);
      const rule = document.createElement('h3');
      rule.className = 'year-rule';
      rule.textContent = year;
      el.stream.appendChild(rule);
    }
    const n = Number(ev.mentions);
    el.stream.appendChild(placeEntry(ev, idx, {
      head: `<span class="entry-date">${formatDate(ev.date, ev.precision)}</span>
             <h3 class="entry-doc" title="${escapeHtml(ev.doc_title)}">${escapeHtml(ev.doc_title)}</h3>`,
      quoteHtml: highlightExactPhrase(ev.sentence, ev.date_text),
      meta: n > 1 ? `Mentioned ${n} times in this document` : '',
    }));
  }
}

function renderLawIndex(laws, passageIds) {
  const scoped = Array.isArray(passageIds);
  el.count.hidden = false;
  el.count.innerHTML = `<b>${laws.length} law${laws.length === 1 ? '' : 's'} and cases cited</b> · ${scopeLabel(scoped)}`;
  backLink(scoped);
  if (!laws.length) return emptyView('No laws or cases are cited in these passages.', 'Open “Laws & cases cited” on the cover to see the whole dossier.');
  el.stream.innerHTML = '';
  for (const [idx, law] of laws.entries()) {
    const item = document.createElement('article');
    item.className = 'entry law-entry';
    item.style.setProperty('--i', Math.min(idx, 20));
    const provisions = Array.from(law.provisions ?? []);
    item.innerHTML = `
      <header class="entry-head">
        <h3 class="entry-doc" title="${escapeHtml(law.instrument)}">${escapeHtml(law.instrument)}</h3>
        <span class="entry-tag tag-related">${Number(law.mentions)} citation${Number(law.mentions) === 1 ? '' : 's'} · ${Number(law.documents)} doc${Number(law.documents) === 1 ? '' : 's'}</span>
      </header>
      ${provisions.length ? `<p class="law-provisions">${provisions.slice(0, 24).map((p) => `<button type="button" class="provision" data-p="${escapeHtml(p)}">§ ${escapeHtml(p)}</button>`).join('')}${provisions.length > 24 ? `<span class="provision-more">+${provisions.length - 24} more</span>` : ''}</p>` : ''}`;
    item.addEventListener('click', (e) => {
      const p = e.target.closest('.provision')?.dataset.p || '';
      showCitations(law.instrument, p, passageIds);
    });
    el.stream.appendChild(item);
  }
}

const KIND_LABEL = { person: 'Person', government: 'Government', company: 'Company', court: 'Court', organization: 'Organization' };

function renderNameIndex(group, names, passageIds) {
  const scoped = Array.isArray(passageIds);
  const noun = group === 'people' ? ['person', 'people'] : ['organization', 'organizations'];
  el.count.hidden = false;
  el.count.innerHTML = `<b>${names.length} ${names.length === 1 ? noun[0] : noun[1]} named</b> · ${scopeLabel(scoped)}`;
  backLink(scoped);
  if (!names.length) return emptyView(`No ${noun[1]} are named in these passages.`, `Open “${group === 'people' ? 'People' : 'Organizations'}” on the cover to see the whole dossier.`);
  el.stream.innerHTML = '';
  for (const [idx, n] of names.entries()) {
    const item = document.createElement('article');
    item.className = 'entry law-entry';
    item.style.setProperty('--i', Math.min(idx, 20));
    const roles = Array.from(n.roles ?? []).map((r) => ROLE_LABEL[r]).filter(Boolean);
    const notes = [group === 'orgs' ? KIND_LABEL[n.kind] : '', n.defined_as ? `the ${n.defined_as.split(',').join(' / ')}` : '', roles.length ? roles.join(' · ') : '']
      .filter(Boolean).join(' · ');
    item.innerHTML = `
      <header class="entry-head">
        <h3 class="entry-doc" title="${escapeHtml(n.entity)}">${escapeHtml(n.entity)}</h3>
        <span class="entry-tag tag-related">${Number(n.mentions)} mention${Number(n.mentions) === 1 ? '' : 's'} · ${Number(n.documents)} doc${Number(n.documents) === 1 ? '' : 's'}</span>
      </header>
      ${notes ? `<p class="entry-meta">${escapeHtml(notes)}</p>` : ''}`;
    item.addEventListener('click', () => showMentions(group, n.entity, passageIds));
    el.stream.appendChild(item);
  }
}

async function showMentions(group, entity, passageIds) {
  if (state.searching) return;
  state.searching = true;
  renderStreamLoading();
  try {
    const rows = await db.nameMentions(entity, passageIds);
    el.count.hidden = false;
    el.count.innerHTML = `<button type="button" class="back-link">← All ${group === 'people' ? 'people' : 'organizations'}</button> <b>${rows.length} passage${rows.length === 1 ? '' : 's'}</b> · ${escapeHtml(entity)}`;
    el.count.querySelector('.back-link').addEventListener('click', () => browse(group, passageIds));
    el.stream.innerHTML = '';
    for (const [idx, row] of rows.entries()) {
      el.stream.appendChild(placeEntry(row, idx, {
        head: `<h3 class="entry-doc" title="${escapeHtml(row.doc_title)}">${escapeHtml(row.doc_title)}</h3>`,
        tag: ROLE_LABEL[row.role] ? escapeHtml(ROLE_LABEL[row.role]) : '',
        quoteHtml: highlightExactPhrase(row.sentence, row.mention_text),
      }));
    }
  } catch (err) {
    console.error(err);
    renderStreamError();
  } finally { state.searching = false; }
}

async function showCitations(instrument, provision, passageIds) {
  if (state.searching) return;
  state.searching = true;
  renderStreamLoading();
  try {
    const rows = await db.authorityCitations(instrument, provision, passageIds);
    const label = `${instrument}${provision ? ` § ${provision}` : ''}`;
    el.count.hidden = false;
    el.count.innerHTML = `<button type="button" class="back-link">← All laws</button> <b>${rows.length} passage${rows.length === 1 ? '' : 's'}</b> · ${escapeHtml(label)}`;
    el.count.querySelector('.back-link').addEventListener('click', () => browse('laws', passageIds));
    el.stream.innerHTML = '';
    for (const [idx, row] of rows.entries()) {
      el.stream.appendChild(placeEntry(row, idx, {
        head: `<h3 class="entry-doc" title="${escapeHtml(row.doc_title)}">${escapeHtml(row.doc_title)}</h3>`,
        tag: row.provision ? `§ ${escapeHtml(row.provision)}` : '',
        quoteHtml: highlightExactPhrase(row.sentence, row.cite_text),
      }));
    }
  } catch (err) {
    console.error(err);
    renderStreamError();
  } finally { state.searching = false; }
}

// "When…?", "Which law…?" and "Who…?" questions are answered from the build-time tables:
// dates, citations and names found in the passages closest to the question, best match first.
const WHEN_RE = /^\s*(when\b|what (date|day|year|month)\b|on what date|by what date|since when|how long ago)/i;
const LAW_RE = /\b(which|what) (laws?|statutes?|regulations?|codes?|acts?|sections?|cases?|rules?|authorit(y|ies))\b/i;
const WHO_RE = /^\s*(who\b|whom\b|whose\b|(which|what) (person|people|individuals?|agenc(y|ies)|compan(y|ies)|organi[sz]ations?|groups?|firms?|consultants?)\b)/i;

const ROLE_LABEL = { author: 'wrote', recipient: 'addressed to', signer: 'signed' };

const ENTITY_ANSWERS = {
  when: {
    ready: () => db.hasEntities(),
    find: (ids) => db.timeline(ids),
    title: 'Dates from the Record', noun: 'Dates',
    label: (f) => formatDate(f.date, f.precision), mark: (f) => f.date_text, one: () => '',
    status: 'Dates are quoted exactly as written. If documents disagree, each version is listed.',
  },
  law: {
    ready: () => db.hasEntities(),
    find: async (ids) => (await Promise.all((await db.authorityIndex(ids, 12)).map((l) => db.authorityCitations(l.instrument, '', ids, 3)))).flat(),
    title: 'Laws and Cases from the Record', noun: 'Citations',
    label: (f) => `${f.instrument}${f.provision ? ` § ${f.provision}` : ''}`, mark: (f) => f.cite_text, one: (f) => f.instrument,
    status: 'Citations are quoted exactly as written — click one to see its page.',
  },
  who: {
    ready: () => db.hasNames(),
    find: async (ids) => {
      const names = [...await db.nameIndex('people', ids, 10), ...await db.nameIndex('orgs', ids, 10)];
      return (await Promise.all(names.map((n) => db.nameMentions(n.entity, ids, 3)))).flat();
    },
    title: 'People and Organizations from the Record', noun: 'Names',
    label: (f) => `${f.entity}${ROLE_LABEL[f.role] ? ` · ${ROLE_LABEL[f.role]}` : ''}`, mark: (f) => f.mention_text, one: (f) => f.entity,
    status: 'Names are quoted exactly as written — click one to see its page.',
  },
};

async function answerFromEntities(query, ranked) {
  const kind = WHEN_RE.test(query) ? 'when' : LAW_RE.test(query) ? 'law' : WHO_RE.test(query) ? 'who' : null;
  const spec = ENTITY_ANSWERS[kind];
  if (!spec || !spec.ready()) return false;
  const top = ranked.slice(0, 40);
  const rank = new Map(top.map((r, i) => [r.passage_id, i]));
  // Same exact-quote gate as every other answer line.
  let found = (await spec.find(top.map((r) => r.passage_id)))
    .filter((f) => rank.has(f.passage_id) && verifier.isGrounded(f.sentence, top[rank.get(f.passage_id)].passage_text))
    .sort((a, b) => rank.get(a.passage_id) - rank.get(b.passage_id));
  const seen = new Set();
  found = found.filter((f) => !spec.one(f) || (!seen.has(spec.one(f)) && seen.add(spec.one(f))));
  if (!found.length) return false;
  found = found.slice(0, 8);

  renderEvidenceCards(top.filter((r) => found.some((f) => f.passage_id === r.passage_id)), 'dossier', query);
  el.summary.innerHTML = `
    <h2>${spec.title}</h2>
    <p class="dossier-q">For “${escapeHtml(query)}”</p>
    <ol id="claim-list"></ol>
    <p class="dossier-coverage">${spec.noun} found in the ${top.length} passages closest to your question, closest first</p>`;
  const list = el.summary.querySelector('#claim-list');
  for (const f of found) {
    const li = document.createElement('li');
    li.className = 'claim-row';
    li.innerHTML = `<span class="claim-text"><b class="claim-key">${escapeHtml(spec.label(f))}</b> — ${highlightExactPhrase(f.sentence, spec.mark(f))}</span>`;
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'verify-pill';
    pill.textContent = `verify · ${f.doc_title}, p. ${f.page_number}`;
    pill.dataset.quote = f.sentence;
    pill.addEventListener('click', () => openGrounding(f));
    li.appendChild(pill);
    list.appendChild(li);
  }
  el.summary.hidden = false;
  setStatus(spec.status, false);
  return true;
}

// ---------- Ask the Dossier (§5.4) ----------

// The answering helper reads BATCH passages at a time (that is what fits its
// working memory); thoroughness sets how many it reads before the first answer
// and how wide the pool of candidate passages is. "Read 6 more" extends either.
const BATCH = 6;
const DEPTHS = {
  quick: { label: 'Quick', hint: 'about 15 seconds', candidates: 40, read: 6 },
  thorough: { label: 'Thorough', hint: 'about a minute', candidates: 100, read: 18 },
  exhaustive: { label: 'Exhaustive', hint: 'several minutes', candidates: 250, read: 60 },
};
const DEPTH_KEY = 'wehpah-dossier-depth';
const SHORT_PASSAGE = 120;

function currentDepth() {
  try { const d = localStorage.getItem(DEPTH_KEY); return DEPTHS[d] ? d : 'quick'; } catch { return 'quick'; }
}

const normText = (t) => verifier.normalizeWhitespace(t).toLowerCase();

/**
 * Wide candidate pool, scored against the question by the cross-encoder, best
 * first. Identical text filed in several documents collapses into one passage
 * that remembers its copies; short fragments rank after every real passage.
 */
async function rankPassages(query, depth) {
  const candidates = await db.candidatePassages(query, DEPTHS[depth].candidates);
  let scores = null;
  try {
    setStatus('Double-checking which passages matter most…', true);
    await reranker.ensureReady((p) => {
      if (p.kind === 'progress' && p.total) {
        const pct = Math.round((p.loaded / p.total) * 100);
        setStatus(`Preparing the passage double-checker — ${pct}% (one-time download, saved on this device)…`, pct < 100);
      }
    });
    scores = await reranker.rank(query, candidates.map((r) => excerpt(r.passage_text, 1000)), (p) => {
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      setStatus(`Reading the ${p.total} closest passages against your question — ${pct}%…`, pct < 100);
    });
  } catch (err) {
    console.warn('Re-ranking unavailable; using plain hybrid retrieval.', err);
  }
  // Stable sort: without scores, the retrieval order stands.
  const ranked = candidates
    .map((row, i) => ({ row, score: Number.isFinite(scores?.[i]) ? scores[i] : Number.NEGATIVE_INFINITY }))
    .sort((a, b) => b.score - a.score)
    .map((x) => x.row);

  const byText = new Map();
  const unique = [];
  for (const row of ranked) {
    const key = normText(row.passage_text).slice(0, 220);
    const first = byText.get(key);
    if (first) {
      if (row.doc_id !== first.doc_id && !first.alsoIn.some((r) => r.doc_id === row.doc_id)) first.alsoIn.push(row);
      continue;
    }
    const copy = { ...row, alsoIn: [] };
    byText.set(key, copy);
    unique.push(copy);
  }
  const long = (r) => (r.passage_text || '').length >= SHORT_PASSAGE;
  return [...unique.filter(long), ...unique.filter((r) => !long(r))];
}

let run = null;   // the answer currently on screen

async function startDossier(query) {
  const depth = currentDepth();
  const ranked = await rankPassages(query, depth);
  const r = {
    query, depth, ranked, read: 0, claims: [], busy: false, stopped: false,
    gpuOK: await answerEngine.webGPUSupported(),
  };
  run = r;
  if (!ranked.length) {
    renderEvidenceCards([], 'dossier', query);
    el.summary.hidden = true;
    setStatus('No matching passages were found for that question.', false);
    return;
  }
  if (await answerFromEntities(query, ranked)) { run = null; return; }
  await readMore(r, DEPTHS[depth].read);
}

/** Read the next `count` passages in batches, adding verified claims as they arrive. */
async function readMore(r, count) {
  r.busy = true;
  r.stopped = false;
  const target = Math.min(r.ranked.length, r.read + count);
  renderRun(r);
  while (r.read < target && !r.stopped && run === r) {
    const batch = r.ranked.slice(r.read, Math.min(target, r.read + BATCH));
    setStatus(`Reading passages ${r.read + 1}–${r.read + batch.length} of ${r.ranked.length}…`, true);
    const claims = await answerBatch(r, batch);
    r.read += batch.length;
    for (const claim of claims) addClaim(r, claim);
    if (run === r) renderRun(r);
  }
  r.busy = false;
  if (run !== r) return;
  renderRun(r);
  setStatus(r.claims.some((c) => c.written)
    ? 'Answer written — every line is checked against the page it cites.'
    : 'Key passages extracted below — every line cites the page it came from.', false);
}

/** Claims for one batch: the answering helper when it is on, otherwise exact sentences. */
async function answerBatch(r, rows) {
  if (answerEngine.optedIn() && r.gpuOK) {
    try {
      // One-time model setup (tiered by device, ~0.9–2.5 GB), cached afterwards.
      await answerEngine.ensureReady((p) => {
        const pct = Math.round((p.progress || 0) * 100);
        setStatus(`Preparing the answering helper — ${pct}% (one-time download, saved on this device)…`, pct < 100);
      });
      setStatus(`Reading passages ${r.read + 1}–${r.read + rows.length} of ${r.ranked.length} and writing a cited answer…`, true);
      return gateClaims(await answerEngine.generateClaims(r.query, rows), rows)
        .map((c) => ({ ...c, written: true }));
    } catch (err) {
      console.warn('Answering helper unavailable; showing extracted key passages instead.', err);
    }
  }
  return extractiveClaims(r.query, rows);
}

/** Skip a claim that repeats (or is contained in) one already shown. */
function addClaim(r, claim) {
  const text = normText(claim.statement);
  const dup = r.claims.findIndex((c) => { const o = normText(c.statement); return o.includes(text) || text.includes(o); });
  if (dup < 0) r.claims.push(claim);
  else if (text.length > normText(r.claims[dup].statement).length) r.claims[dup] = claim;
}

/** The quote must occur verbatim in its passage, and the written statement must
 * pass verifier.isSupported against that quote (no new numbers, names or negations). */
function gateClaims(rawClaims, rows) {
  const passageById = new Map(rows.map((r) => [r.passage_id, r]));
  const verified = verifier.verifyClaims(rawClaims, passageById)
    .filter((claim) => {
      const statement = verifier.normalizeWhitespace(claim.statement);
      const quote = verifier.normalizeWhitespace(claim.verbatim_quote);
      return statement.length >= 20 && verifier.isSupported(statement, quote);
    });
  const distinct = [];
  for (const claim of verified) {
    const statement = verifier.normalizeWhitespace(claim.statement).toLowerCase();
    const duplicateIndex = distinct.findIndex((prior) => {
      const other = verifier.normalizeWhitespace(prior.statement).toLowerCase();
      return statement.includes(other) || other.includes(statement);
    });
    if (duplicateIndex < 0) distinct.push(claim);
    else if (statement.length > distinct[duplicateIndex].statement.length) distinct[duplicateIndex] = claim;
  }
  return distinct.map((c) => ({ ...c, passage: passageById.get(c.passage_id) }));
}

/** One exact sentence per document, ranked by question-word overlap. */
function extractiveClaims(query, rows) {
  const words = [...new Set((query.toLowerCase().match(/[a-z0-9]{4,}/g) || [])
    .filter((word) => !['what', 'when', 'where', 'which', 'would', 'could', 'should', 'about', 'have', 'from', 'with', 'that', 'this'].includes(word)))];
  const byDoc = new Map();
  for (const row of rows) {
    if (!byDoc.has(row.doc_id)) byDoc.set(row.doc_id, []);
    byDoc.get(row.doc_id).push(row);
  }

  const claims = [];
  for (const [, docRows] of byDoc) {
    let best = null;
    for (const row of docRows) {
      const sentences = verifier.sentenceCandidates(row.passage_text);
      const candidates = sentences.length ? sentences : [verifier.normalizeWhitespace(row.passage_text)];
      for (const sentence of candidates) {
        const lower = sentence.toLowerCase();
        const relevance = words.reduce((score, word) => score + (lower.includes(word) ? 1 : 0), 0);
        if (!best || relevance > best.relevance) {
          best = {
            relevance,
            statement: sentence,
            verbatim_quote: sentence,
            passage_id: row.passage_id,
            passage: row,
          };
        }
      }
    }
    if (best) claims.push(best);
  }

  // Mandatory verification gate — discard anything not verbatim in its source.
  const passageById = new Map(rows.map((r) => [r.passage_id, r]));
  return verifier.verifyClaims(claims, passageById)
    .map((c) => ({ ...c, passage: passageById.get(c.passage_id) }));
}

function renderRun(r) {
  const readRows = r.ranked.slice(0, r.read);
  renderEvidenceCards(readRows, 'dossier', r.query);

  // With the helper on, an empty answer means "nothing here answers it";
  // still show the most relevant exact sentences rather than an empty plate.
  const written = r.claims.some((c) => c.written);
  const claims = r.claims.length || r.busy ? r.claims : extractiveClaims(r.query, readRows.slice(0, BATCH));
  const docs = new Set(readRows.map((row) => row.doc_id)).size;

  el.summary.innerHTML = `
    <h2>${written ? 'Answer from the Record' : 'Extracted Key Passages'}</h2>
    <p class="dossier-q">For “${escapeHtml(r.query)}”</p>
    <div class="depth-choice" role="group" aria-label="How thorough"></div>
    <ol id="claim-list"></ol>
    <p class="dossier-coverage">Read ${r.read} of ${r.ranked.length} closest passages · ${docs} document${docs === 1 ? '' : 's'}</p>
  `;

  const choice = el.summary.querySelector('.depth-choice');
  for (const [key, d] of Object.entries(DEPTHS)) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'depth-option';
    b.textContent = d.label;
    b.title = d.hint;
    b.setAttribute('aria-pressed', String(key === r.depth));
    b.disabled = r.busy;
    b.addEventListener('click', () => {
      if (key === r.depth) return;
      try { localStorage.setItem(DEPTH_KEY, key); } catch { /* private mode */ }
      el.input.value = r.query;
      runSearch();
    });
    choice.appendChild(b);
  }

  const list = el.summary.querySelector('#claim-list');
  for (const claim of claims) {
    const passage = claim.passage || state.passages.get(claim.passage_id);
    if (!passage) continue;
    const li = document.createElement('li');
    li.className = 'claim-row';

    const text = document.createElement('span');
    text.className = 'claim-text';
    text.textContent = claim.statement;
    li.appendChild(text);

    // Gold verification chip on its own line under the sentence: hover quotes the
    // verbatim source, click opens the cited page.
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'verify-pill';
    pill.textContent = `verify · ${passage.doc_title}, p. ${passage.page_number}`;
    pill.title = `${passage.doc_title} — page ${passage.page_number}`;
    pill.dataset.quote = claim.verbatim_quote || claim.statement;
    pill.addEventListener('click', () => openGrounding(passage));
    li.appendChild(pill);

    if (passage.alsoIn?.length) {
      const also = document.createElement('span');
      also.className = 'claim-also';
      const n = passage.alsoIn.length;
      also.textContent = `Same text in ${n} other filing${n === 1 ? '' : 's'}`;
      also.title = passage.alsoIn.map((row) => `${row.doc_title}, p. ${row.page_number}`).join('\n');
      li.appendChild(also);
    }
    list.appendChild(li);
  }

  if (r.busy) {
    const stop = document.createElement('button');
    stop.type = 'button';
    stop.className = 'read-more';
    stop.textContent = 'Stop after this batch';
    stop.addEventListener('click', () => { r.stopped = true; stop.disabled = true; stop.textContent = 'Stopping…'; });
    el.summary.appendChild(stop);
  } else if (r.read < r.ranked.length) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'read-more';
    const n = Math.min(BATCH, r.ranked.length - r.read);
    more.textContent = `Read ${n} more passage${n === 1 ? '' : 's'}`;
    more.addEventListener('click', () => readMore(r, BATCH));
    el.summary.appendChild(more);
  }

  if (!r.busy && r.gpuOK && !written) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'setup-chip';
    chip.textContent = answerEngine.optedIn()
      ? 'Try the written answer again'
      : 'Get full written answers — one-time setup download, saved on this device';
    // The size depends on which model tier this machine gets; fill it in when known.
    answerEngine.estimateSetupGB().then((gb) => {
      if (gb && !answerEngine.optedIn()) chip.textContent = `Get full written answers — one-time ~${gb} GB setup, saved on this device`;
    }).catch(() => {});
    chip.addEventListener('click', () => {
      answerEngine.optIn();
      chip.disabled = true;
      chip.textContent = 'Setting up the answering helper…';
      r.read = 0;
      r.claims = [];
      readMore(r, DEPTHS[r.depth].read);
    });
    el.summary.appendChild(chip);
  }
  el.summary.hidden = false;
}

boot();
