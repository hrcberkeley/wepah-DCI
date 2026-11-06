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
  return normalizeWhitespace(passageText)
    .split(/(?<=[.;:!?])\s+(?=[A-Z(“"\d])/)
    .map((s) => s.trim())
    .filter((s) => s.length > 40);
}
