// pdf_stage.js — PDF.js renderer with normalized SVG/DIV bounding-box overlays (§5.5)
//
// The browser never converts raw PDF points: rows already carry top-left percentage
// boxes, so overlay styling is pure CSS percentages and stays zoom-proof.

import { pdfUrl } from './db_client.js';

const PDFJS_URL = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.min.mjs';
const PDFJS_WORKER_URL = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.worker.min.mjs';

const ZOOM_MODES = ['Fit Width', '100%', '150%'];

let pdfjsLib = null;
let currentPdf = null;      // PDFDocumentProxy
let currentPageNum = 1;
let renderTask = null;
let currentDocId = null;
let pendingHighlight = null; // { x, y, w, h } in 0..1 percentages
let zoomIndex = 0;

const el = {
  canvas: null, wrap: null, layer: null, scroll: null, empty: null,
  title: null, counter: null, prev: null, next: null, zoom: null, download: null,
};

function $(id) { return document.getElementById(id); }

function bindElements() {
  el.canvas = $('pdf-canvas');
  el.wrap = $('canvas-wrap');
  el.layer = $('bbox-layer');
  el.scroll = $('stage-scroll');
  el.empty = $('stage-empty');
  el.title = $('stage-doc-title');
  el.counter = $('page-counter');
  el.prev = $('page-prev');
  el.next = $('page-next');
  el.zoom = $('zoom-toggle');
  el.download = $('download-pdf');

  el.prev.addEventListener('click', () => gotoPage(currentPageNum - 1));
  el.next.addEventListener('click', () => gotoPage(currentPageNum + 1));
  el.zoom.addEventListener('click', () => {
    zoomIndex = (zoomIndex + 1) % ZOOM_MODES.length;
    el.zoom.textContent = ZOOM_MODES[zoomIndex];
    renderCurrent();
  });
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (!currentPdf) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (ZOOM_MODES[zoomIndex] === 'Fit Width') renderCurrent(); }, 180);
  });
}

async function loadLibrary() {
  if (pdfjsLib) return pdfjsLib;
  pdfjsLib = await import(PDFJS_URL);
  pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
  return pdfjsLib;
}

function fitWidthScale(viewportAtScale1) {
  const available = el.scroll.clientWidth - 44; // padding
  return Math.max(0.2, available / viewportAtScale1.width);
}

function targetScale(viewportAtScale1) {
  const label = ZOOM_MODES[zoomIndex];
  if (label === 'Fit Width') return fitWidthScale(viewportAtScale1);
  if (label === '100%') return 1.0;
  return 1.5;
}

async function renderCurrent() {
  if (!currentPdf) return;
  await renderPage(currentPageNum, pendingHighlight);
}

async function renderPage(pageNum, bbox) {
  const page = await currentPdf.getPage(pageNum);
  const viewport1 = page.getViewport({ scale: 1 });
  const scale = targetScale(viewport1);
  const viewport = page.getViewport({ scale });

  if (renderTask) { try { renderTask.cancel(); } catch { /* noop */ } }

  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  el.canvas.width = Math.floor(viewport.width * dpr);
  el.canvas.height = Math.floor(viewport.height * dpr);
  el.canvas.style.width = `${viewport.width}px`;
  el.canvas.style.height = `${viewport.height}px`;

  el.empty.hidden = true;
  el.wrap.hidden = false;
  el.layer.innerHTML = '';

  renderTask = page.render({
    canvasContext: el.canvas.getContext('2d'),
    viewport,
    transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
  });
  try {
    await renderTask.promise;
  } catch (err) {
    if (err && err.name === 'RenderingCancelledException') return;
    throw err;
  }

  currentPageNum = pageNum;
  el.counter.textContent = `Page ${pageNum} of ${currentPdf.numPages}`;
  el.prev.disabled = pageNum <= 1;
  el.next.disabled = pageNum >= currentPdf.numPages;

  if (bbox) highlightBox(bbox);
  pendingHighlight = bbox || null;
}

/** Zoom-proof percentage overlay straight from the Parquet row (§5.5.3). */
export function highlightBox(bbox) {
  el.layer.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'evidence-highlight-box';
  box.style.left = `${bbox.x * 100}%`;
  box.style.top = `${bbox.y * 100}%`;
  box.style.width = `${bbox.w * 100}%`;
  box.style.height = `${bbox.h * 100}%`;
  el.layer.appendChild(box);
  box.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
}

/**
 * Open a document at a page, optionally highlighting a normalized bbox.
 * Reuses the already-streamed document when the user stays inside one file.
 */
export async function open({ docId, docTitle, pageNumber, bbox }) {
  await loadLibrary();
  if (!el.canvas) bindElements();

  el.download.hidden = false;
  el.download.href = pdfUrl(docId);
  el.download.setAttribute('download', `${docId}.pdf`);
  el.zoom.disabled = false;

  const wanted = Math.max(1, Number(pageNumber) || 1);

  if (currentDocId === docId && currentPdf) {
    el.title.textContent = docTitle || docId;
    pendingHighlight = bbox || null;
    await gotoPage(wanted);
    return;
  }

  setStatusOnLoad();
  if (currentPdf) { try { await currentPdf.destroy(); } catch { /* noop */ } }
  currentPdf = null;
  currentDocId = null;
  currentPdf = await pdfjsLib.getDocument({
    url: pdfUrl(docId),
    disableAutoFetch: true,   // HTTP Range Requests: open p.45 of a 300-page file without the rest
    disableStream: false,
  }).promise;
  currentDocId = docId;
  el.title.textContent = docTitle || docId;
  await gotoPage(wanted, bbox);
}

async function gotoPage(pageNum, bbox) {
  const clamped = Math.min(Math.max(1, pageNum), currentPdf.numPages);
  await renderPage(clamped, bbox || null);
}

function setStatusOnLoad() {
  el.title.textContent = 'Opening document…';
  el.counter.textContent = '—';
}
