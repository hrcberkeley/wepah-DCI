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
};

const el = {
  pills: null, input: null, button: null, helper: null, status: null,
  stream: null, summary: null,
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

  el.pills.forEach((pill) => {
    pill.addEventListener('click', () => setMode(pill.dataset.mode));
  });
  el.button.addEventListener('click', () => runSearch());
  el.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });

  try {
    await db.initEngine();
  } catch (err) {
    console.error(err);
    setStatus('The record index could not be opened. Please make sure the data folder is being served and reload.', false);
    el.button.disabled = true;
    return;
  }
  el.input.focus();
}

function setMode(mode) {
  if (!MODES[mode]) return;
  state.mode = mode;
  el.pills.forEach((p) => {
    const active = p.dataset.mode === mode;
    p.classList.toggle('active', active);
    p.setAttribute('aria-selected', String(active));
  });
  el.input.placeholder = MODES[mode].placeholder;
  el.helper.innerHTML = `<em>${MODES[mode].helper}</em>`;
  el.summary.hidden = true;
  el.input.focus();
}

function setStatus(msg, busy = false) {
  el.status.textContent = msg || '';
  el.status.classList.toggle('busy', busy);
}

// ---------- search dispatch ----------
async function runSearch() {
  const query = el.input.value.trim();
  if (!query) { el.input.focus(); return; }
  if (!db.isReady()) return;

  state.query = query;
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
      const rows = await db.hybridPassages(query);
      await renderDossier(query, rows);
    }
  } catch (err) {
    console.error(err);
    renderStreamError();
    setStatus('Something went wrong while searching. Please try again.', false);
  } finally {
    el.button.disabled = false;
  }
}

// ---------- evidence cards (§5.3) ----------
function badgeFor(row, mode) {
  if (row.content_type === 'visual_evidence') {
    return { cls: 'visual', text: 'Contains Figure / Stamp' };
  }
  if (mode === 'exact') return { cls: 'exact', text: 'Exact Word Match' };
  if (mode === 'concepts') {
    return (typeof row.score === 'number' && row.score >= CONFIG.STRONG_MATCH_THRESHOLD)
      ? { cls: 'strong', text: 'Strong Conceptual Match' }
      : { cls: 'related', text: 'Related Context' };
  }
  return { cls: 'related', text: 'Related Context' };
}

function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

/** Wrap query terms in <mark class="exact-hit"> for Find Exact Words mode. */
function highlightTerms(htmlEscaped, query) {
  const terms = query.split(/\s+/).filter((t) => t.length >= 2).map(escapeRegExp);
  if (!terms.length) return htmlEscaped;
  const re = new RegExp(`(${terms.join('|')})`, 'gi');
  return htmlEscaped.replace(re, '<mark class="exact-hit">$1</mark>');
}

function excerpt(text, max = 700) {
  const t = String(text ?? '');
  return t.length > max ? `${t.slice(0, max)}…` : t;
}

function renderStreamLoading() {
  el.stream.innerHTML = `
    <div class="empty-state"><p>Looking through the record…</p></div>`;
}

function renderStreamError() {
  el.stream.innerHTML = `
    <div class="empty-state"><p>Nothing to show. Please adjust your search and try again.</p></div>`;
}

function renderEvidenceCards(rows, mode, query) {
  state.passages.clear();
  rows.forEach((r) => state.passages.set(r.passage_id, r));

  if (!rows.length) {
    el.stream.innerHTML = `
      <div class="empty-state">
        <p>No matching passages.</p>
        <p class="empty-hint">Try different words, or switch search modes above.</p>
      </div>`;
    return;
  }

  el.stream.innerHTML = '';
  for (const row of rows) {
    const badge = badgeFor(row, mode);
    const card = document.createElement('article');
    card.className = 'evidence-card';
    card.dataset.passageId = row.passage_id;

    const quoteHtml = mode === 'exact'
      ? highlightTerms(escapeHtml(excerpt(row.passage_text)), query)
      : escapeHtml(excerpt(row.passage_text));

    card.innerHTML = `
      <div class="card-header">
        <span class="card-title">${escapeHtml(row.doc_title)}</span>
        <span class="page-badge">Page ${row.page_number}</span>
        <span class="match-badge ${badge.cls}">${badge.text}</span>
      </div>
      <blockquote class="card-quote">${quoteHtml}</blockquote>
      <div class="card-meta">${escapeHtml(row.doc_date)} · ${escapeHtml(row.doc_id)}.pdf</div>
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
async function renderDossier(query, rows) {
  state.passages.clear();
  rows.forEach((r) => state.passages.set(r.passage_id, r));

  renderEvidenceCards(rows, 'dossier', query);
  setStatus(rows.length
    ? 'Summary prepared below — every line cites the page it came from.'
    : 'No matching passages were found for that question.', false);
  if (!rows.length) return;

  let claims;
  if (CONFIG.ENABLE_LLM_SYNTHESIS && (await llmAvailable())) {
    claims = await synthesizeWithModel(query, rows);          // may be []
  }
  if (!claims || !claims.length) {
    claims = await extractiveClaims(query, rows);             // zero-download default
  }

  renderSummary(query, claims, rows);
  el.summary.hidden = false;
}

/** One exact sentence per document: the one closest to the question. */
async function extractiveClaims(query, rows) {
  const qv = await db.embedQuery(query);
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
      const vecs = await Promise.all(candidates.map((s) => db.embedRaw(s)));
      for (let i = 0; i < candidates.length; i++) {
        const sim = dot(qv, vecs[i]);
        if (!best || sim > best.sim) {
          best = {
            sim,
            statement: candidates[i],
            verbatim_quote: candidates[i],
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

function dot(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

function renderSummary(query, claims, rows) {
  el.summary.innerHTML = `
    <h2>Extracted Key Passages</h2>
    <p class="dossier-question">For: “${escapeHtml(query)}”</p>
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

    // Gold verification pill: hover shows the verbatim quote, click opens the page.
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'verify-pill';
    pill.textContent = `verify: ${passage.doc_title}, p. ${passage.page_number}`;
    pill.dataset.quote = (claim.verbatim_quote || claim.statement).slice(0, 240);
    pill.addEventListener('click', () => openGrounding(passage));
    li.appendChild(pill);

    list.appendChild(li);
  }
}

// ---------- optional on-device synthesis (§5.4.3, off by default) ----------
let _llm = null;
async function llmAvailable() {
  try { return !!(navigator.gpu && navigator.gpu.requestAdapter); } catch { return false; }
}

async function synthesizeWithModel(query, rows) {
  try {
    if (!_llm) {
      const { pipeline } = await import(
        'https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.2'
      );
      _llm = await pipeline('text-generation', 'onnx-community/Llama-3.2-1B-Instruct-q4f16',
        { device: 'webgpu', dtype: 'q4f16' });
    }
    const excerpts = rows.map((r, i) =>
      `[${i + 1}] passage_id=${r.passage_id} (${r.doc_title}, p. ${r.page_number})\n"${excerpt(r.passage_text, 800)}"`
    ).join('\n\n');
    const prompt =
      'You answer questions using ONLY the excerpts provided. ' +
      'Return ONLY valid JSON, no other text, in this exact shape:\n' +
      '{"claims":[{"statement":"...","passage_id":"...","verbatim_quote":"..."}]}\n' +
      'verbatim_quote MUST be copied character-for-character from an excerpt.\n\n' +
      `Excerpts:\n${excerpts}\n\nQuestion: ${query}\nJSON:`;

    const out = await _llm([{ role: 'user', content: prompt }], { max_new_tokens: 512 });
    const text = out[0]?.generated_text?.at(-1)?.content || '';
    const jsonStart = text.indexOf('{');
    const jsonEnd = text.lastIndexOf('}');
    if (jsonStart < 0 || jsonEnd <= jsonStart) return [];
    const parsed = JSON.parse(text.slice(jsonStart, jsonEnd + 1));

    const passageById = new Map(rows.map((r) => [r.passage_id, r]));
    return verifier.verifyClaims(parsed.claims, passageById)
      .map((c) => ({ ...c, passage: passageById.get(c.passage_id) }));
  } catch (err) {
    console.warn('On-device synthesis unavailable; using extracted key passages.', err);
    return [];
  }
}

boot();
