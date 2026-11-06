// A dossier repository must contain dossier.json plus the indexed corpus.
export const DEFAULT_REPO = 'kolenyo/MS_Datacenter';

export function parseRepository(value) {
  const input = String(value || '').trim().replace(/\/$/, '');
  let repo = input;
  if (/^https?:\/\//i.test(input)) {
    const url = new URL(input);
    if (url.hostname !== 'huggingface.co' && url.hostname !== 'www.huggingface.co') throw new Error('Use a Hugging Face dataset link.');
    const parts = url.pathname.split('/').filter(Boolean);
    if (parts[0] !== 'datasets' || parts.length < 3) throw new Error('Use a Hugging Face dataset link.');
    repo = `${parts[1]}/${parts[2]}`;
  }
  if (!/^[A-Za-z0-9][\w.-]*\/[A-Za-z0-9][\w.-]*$/.test(repo)) throw new Error('Enter a dataset link or owner/name.');
  return repo;
}

export function validateDossier(data) {
  if (!data || data.schema_version !== 1) throw new Error('This dataset needs a WeHPAH dossier.json (version 1).');
  for (const key of ['title', 'corpus_path', 'centroids_path', 'pdfs_path']) {
    if (typeof data[key] !== 'string' || !data[key].trim()) throw new Error(`The dossier is missing ${key}.`);
  }
  for (const key of ['corpus_path', 'centroids_path', 'pdfs_path']) {
    if (data[key].startsWith('/') || data[key].split('/').some((part) => !part || part === '.' || part === '..')) throw new Error('The dossier contains an invalid file path.');
  }
  if (data.model_id !== 'Xenova/bge-small-en-v1.5' || data.dimensions !== 384) throw new Error('This dossier uses an unsupported search index.');
  if (!Number.isSafeInteger(data.document_count) || data.document_count < 1 || !Number.isSafeInteger(data.passage_count) || data.passage_count < 1) throw new Error('The dossier has invalid document counts.');
  return data;
}

export async function loadDossier(repo, signal) {
  const response = await fetch(`https://huggingface.co/datasets/${repo}/resolve/main/dossier.json`, { signal });
  if (!response.ok) throw new Error(response.status === 404 ? 'This dataset has no WeHPAH dossier.json.' : `Could not open this dataset (${response.status}).`);
  return validateDossier(await response.json());
}
