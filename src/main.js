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

  try {
    await db.initEngine(state.dossier);
  } catch (err) {
    console.error(err);
    setStatus('The record index could not be opened. Please make sure the data folder is being served and reload.', false);
    el.button.disabled = true;
    return;
  }
  el.input.focus();
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
      setStatus(rows.length
        ? `Found ${rows.length} passage${rows.length === 1 ? '' : 's'} containing your exact words.`
        : 'No passages contain those exact words. Try fewer words, or switch to “Explore Concepts”.', false);
    } else if (state.mode === 'concepts') {
      const rows = await db.conceptSearch(query);
      renderEvidenceCards(rows, 'concepts', query);
      setStatus(rows.length
        ? `Found ${rows.length} related passage${rows.length === 1 ? '' : 's'}. Click a card to see the original page.`
        : 'Nothing closely related yet. Try describing the topic with different words.', false);
    } else {
      const rows = await selectBestPassages(query);
      await renderDossier(query, rows);
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

// ---------- Ask the Dossier (§5.4) ----------

/**
 * Cross-encoder re-ranking (§ retrieval quality): pull a wide candidate pool,
 * score every (question, passage) pair with bge-reranker, keep the best 6.
 * Falls back to plain hybrid retrieval if the reranker can't load or score.
 */
async function selectBestPassages(query) {
  const candidates = await db.candidatePassages(query, CONFIG.RERANKER_CANDIDATES);
  // Drop table-cell fragments — they carry no answer and pollute the prompt.
  const pool = candidates.filter((r) => (r.passage_text || '').length >= 200);
  const use = pool.length >= CONFIG.DOSSIER_PASSAGES ? pool : candidates;
  if (use.length <= CONFIG.DOSSIER_PASSAGES) return use;

  try {
    setStatus('Double-checking which passages matter most…', true);
    await reranker.ensureReady((p) => {
      if (p.kind === 'progress' && p.total) {
        const pct = Math.round((p.loaded / p.total) * 100);
        setStatus(`Preparing the passage double-checker — ${pct}% (one-time download, saved on this device)…`, pct < 100);
      }
    });
    const scores = await reranker.rank(
      query,
      pool.map((r) => excerpt(r.passage_text, 1000)),
      (p) => {
        const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
        setStatus(`Reading the ${p.total} closest passages against your question — ${pct}%…`, pct < 100);
      }
    );
    const ranked = pool
      .map((row, i) => ({ row, score: scores[i] ?? Number.NEGATIVE_INFINITY }))
      .sort((a, b) => b.score - a.score);
    const seenText = new Set();
    return ranked.filter(({ row }) => {
      const key = (row.passage_text || '').replace(/\s+/g, ' ').trim().toLowerCase().slice(0, 220);
      if (seenText.has(key)) return false;
      seenText.add(key);
      return true;
    }).slice(0, CONFIG.DOSSIER_PASSAGES).map((x) => x.row);
  } catch (err) {
    console.warn('Re-ranking unavailable; using plain hybrid retrieval.', err);
    return pool.slice(0, CONFIG.DOSSIER_PASSAGES);
  }
}

async function renderDossier(query, rows) {
  state.passages.clear();
  rows.forEach((r) => state.passages.set(r.passage_id, r));

  renderEvidenceCards(rows, 'dossier', query);
  if (!rows.length) {
    setStatus('No matching passages were found for that question.', false);
    return;
  }

  const gpuOK = await answerEngine.webGPUSupported();
  const engineOn = answerEngine.optedIn() && gpuOK;
  let claims = null;
  let usedModel = false;

  if (engineOn) {
    try {
      // One-time model setup (tiered by device, ~0.9–2.5 GB), cached afterwards.
      await answerEngine.ensureReady((p) => {
        const pct = Math.round((p.progress || 0) * 100);
        setStatus(`Preparing the answering helper — ${pct}% (one-time download, saved on this device)…`, pct < 100);
      });
      setStatus('Reading the record and writing a cited answer…', true);
      const raw = await answerEngine.generateClaims(query, rows);
      claims = gateClaims(raw, rows);
      usedModel = claims.length > 0;
    } catch (err) {
      console.warn('Answering helper unavailable; showing extracted key passages instead.', err);
      claims = null;
    }
  }

  // Extractive fallback: deterministic and available without another download.
  if (!claims || !claims.length) {
    claims = extractiveClaims(query, rows);
  }

  renderSummary(query, claims, rows, {
    title: usedModel ? 'Selected Source Passages' : 'Extracted Key Passages',
    offerSetup: gpuOK && !usedModel,
    onEnableEngine: gpuOK && !usedModel
      ? () => renderDossier(query, rows)
      : null,
  });

  setStatus(claims.length
    ? (usedModel
      ? 'Source passages selected — every line cites the page it came from.'
      : 'Summary prepared below — every line cites the page it came from.')
    : 'No matching passages were found for that question.', false);
  el.summary.hidden = false;
}

/** A displayed statement must itself occur in the cited quote. Quote presence
 * alone cannot establish that a paraphrase follows from the source. */
function gateClaims(rawClaims, rows) {
  const passageById = new Map(rows.map((r) => [r.passage_id, r]));
  const verified = verifier.verifyClaims(rawClaims, passageById)
    .filter((claim) => {
      const statement = verifier.normalizeWhitespace(claim.statement);
      const quote = verifier.normalizeWhitespace(claim.verbatim_quote);
      return statement.length >= 40 && verifier.isGrounded(statement, quote);
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

function renderSummary(query, claims, rows, { offerSetup = false, onEnableEngine = null, title = 'Extracted Key Passages' } = {}) {
  el.summary.innerHTML = `
    <h2>${escapeHtml(title)}</h2>
    <p class="dossier-q">For “${escapeHtml(query)}”</p>
    <ol id="claim-list"></ol>
  `;
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

    // Gold verification chip on its own line under the sentence: hover quotes
    // the verbatim source, click opens the cited page.
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'verify-pill';
    pill.textContent = `verify · ${passage.doc_title}, p. ${passage.page_number}`;
    pill.title = `${passage.doc_title} — page ${passage.page_number}`;
    pill.dataset.quote = claim.verbatim_quote || claim.statement;
    pill.addEventListener('click', () => openGrounding(passage));
    li.appendChild(pill);

    list.appendChild(li);
  }

  if (offerSetup && onEnableEngine) {
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
    chip.addEventListener('click', async () => {
      answerEngine.optIn();
      chip.disabled = true;
      chip.textContent = 'Setting up the answering helper…';
      await onEnableEngine();
    });
    el.summary.appendChild(chip);
  }
}

boot();
