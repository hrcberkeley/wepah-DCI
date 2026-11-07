// verifier.js — Deterministic quote-grounding verification gate (AGENTS.md §5.4.4)
//
// Before ANY model-generated claim is rendered on screen, its verbatim quote must be
// found inside the cited source passage (whitespace-normalized, case-insensitive).
// Claims that fail the check are discarded silently — unverified text never renders.

export function normalizeWhitespace(s) {
  return String(s ?? '').replace(/\s+/g, ' ').trim();
}

/**
 * Deterministic grounding check: does `quote` appear inside `passageText`?
 * @returns {boolean}
 */
export function isGrounded(verbatimQuote, passageText) {
  const q = normalizeWhitespace(verbatimQuote).toLowerCase();
  if (!q) return false;
  const t = normalizeWhitespace(passageText).toLowerCase();
  return t.includes(q);
}

/**
 * Filter an array of claims `{ statement, passage_id, verbatim_quote }` against the
 * retrieved passages map. Only verified claims are returned — everything else is
 * dropped silently, exactly as the specification requires.
 * @param {Array<{statement:string, passage_id:string, verbatim_quote:string}>} claims
 * @param {Map<string, object>} passageById
 * @returns {Array} verified claims only
 */
export function verifyClaims(claims, passageById) {
  const verified = [];
  for (const claim of Array.isArray(claims) ? claims : []) {
    const passage = passageById.get(claim.passage_id);
    if (!passage) continue; // no source -> unverifiable -> discard
    if (isGrounded(claim.verbatim_quote, passage.passage_text)) {
      verified.push(claim);
    }
    // If FALSE: discard the claim silently. Never render unverified text.
  }
  return verified;
}

/**
 * Extractive fallback: split a passage into sentences and verify the chosen one.
 * Used by the zero-download synthesis path so every rendered sentence passes the
 * same gate as model claims.
 */
export function sentenceCandidates(passageText) {
  // Line breaks separate a heading from its paragraph: never glue them into one sentence.
  return String(passageText ?? '').split(/\n+/).map(normalizeWhitespace)
    .flatMap((line) => line.split(/(?<=[.;:!?])\s+(?=[A-Z(“"\d])/))
    .map((s) => s.trim())
    .filter((s) => s.length > 40);
}

// ---------- support gate for written (rephrased) claims ----------
// A rephrased statement may change connective wording, but every fact in it
// must come from its cited quote. Deterministic lexical rules:
//   • every number in the statement appears in the quote,
//   • every capitalized name (not sentence-initial) appears in the quote,
//   • negation agrees (no added or dropped "not"/"no"/"never"…),
//   • ≥ 75% of remaining content words share a 4-letter stem with the quote.
// ponytail: lexical, not semantic — can't catch a reordered "A sued B" vs "B sued A";
// add a cross-encoder entailment check if that ever shows up in practice.
const STOP = new Set(('the a an and or of to in on at by for with from as is are was were be been being ' +
  'this that these those it its their them they he she his her there which who whom whose what when where ' +
  'why how than then also such any all each per into onto upon under over about after before between ' +
  'during within shall will would should could may might must can has have had do does did so if but ' +
  'because while both other only same').split(' '));
const NEGATION = /\b(not|no|never|nor|none|neither|without|cannot)\b|n't\b/i;

function tokens(text) {
  return normalizeWhitespace(text).replace(/(\d),(\d)/g, '$1$2').match(/[A-Za-z0-9][A-Za-z0-9'-]*/g) || [];
}
const stem = (w) => w.toLowerCase().replace(/'s$/, '').slice(0, 4);

export function isSupported(statement, quote) {
  const st = tokens(statement);
  const qt = tokens(quote);
  const quoteWords = new Set(qt.map((w) => w.toLowerCase()));
  const quoteStems = new Set(qt.map(stem));
  if (NEGATION.test(statement) !== NEGATION.test(quote)) return false;

  let content = 0;
  let covered = 0;
  st.forEach((word, i) => {
    const lower = word.toLowerCase();
    if (/\d/.test(word) || (i > 0 && /^[A-Z]/.test(word) && !STOP.has(lower))) {
      if (!quoteWords.has(lower)) content = Infinity; // invented number/name: hard fail
      return;
    }
    if (lower.length < 3 || STOP.has(lower) || NEGATION.test(lower)) return;
    content += 1;
    if (quoteStems.has(stem(lower))) covered += 1;
  });
  return content >= 2 && content !== Infinity && covered / content >= 0.75;
}
