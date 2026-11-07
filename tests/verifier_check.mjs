import assert from 'node:assert';
import { isSupported } from '../src/verifier.js';
const q = 'The Contractor shall notify the Owner in writing within 14 days of discovering any structural defect.';
assert(isSupported('The contractor must give the Owner written notice within 14 days of finding a structural defect.', q));
assert(!isSupported('The contractor must notify the Owner within 30 days.', q), 'wrong number');
assert(!isSupported('The contractor need not notify the Owner of structural defects.', q), 'added negation');
assert(!isSupported('The Architect must notify the Owner within 14 days.', q), 'invented name');
assert(!isSupported('The contractor was negligent and liable for damages.', q), 'unsupported facts');
console.log('ok');
