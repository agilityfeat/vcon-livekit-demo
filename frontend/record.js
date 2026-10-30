/*
 * Record detail rendering + doctor/analyst toggle.
 *
 * Fetches the readable vCon JSON the backend already decrypted/verified:
 *   doctor  -> GET /api/records/{run}/full
 *   analyst -> GET /api/records/{run}/redacted
 * then renders it with the salvaged viewer logic. The pure helpers
 * (speakerLabelForPartyIndex, isSameVconLink, ...) live in vcon-helpers.js and
 * are exposed as globals by that classic script.
 *
 * This page NEVER touches JOSE: the server did all verify/decrypt, so here we
 * only shape DOM from plain JSON. The corrected full<->redacted linkage banner
 * is the headline behavior.
 */

'use strict';

function $(id) { return document.getElementById(id); }

function setStatus(msg, kind) {
  var el = $('status');
  el.textContent = msg || '';
  el.className = 'status' + (kind ? ' ' + kind : '');
}

// Read the run prefix from ?run=... Needed for the backend endpoints.
function runPrefix() {
  var params = new URLSearchParams(window.location.search);
  return params.get('run') || '';
}

var state = {
  run: runPrefix(),
  full: null,       // doctor (full) vCon, cached after first fetch
  redacted: null,   // analyst (redacted) vCon, cached after first fetch
  mode: 'doctor'    // 'doctor' | 'analyst'
};

var audioEl; // the Call audio element, wired up by renderAudio

/* ---------- loading ---------- */

async function fetchView(kind) {
  var url = '/api/records/' + state.run.split('/').map(encodeURIComponent).join('/') +
    '/' + kind;
  var resp = await fetch(url);
  if (!resp.ok) {
    var detail = await resp.text();
    throw new Error('HTTP ' + resp.status + ': ' + detail);
  }
  return resp.json();
}

async function ensureFull() {
  if (!state.full) state.full = await fetchView('full');
  return state.full;
}

async function ensureRedacted() {
  if (!state.redacted) state.redacted = await fetchView('redacted');
  return state.redacted;
}

/* ---------- mode toggle ---------- */

function reflectModeButtons() {
  var doc = $('view-doctor-btn');
  var anal = $('view-analyst-btn');
  var isDoctor = state.mode === 'doctor';
  doc.classList.toggle('active', isDoctor);
  anal.classList.toggle('active', !isDoctor);
  doc.setAttribute('aria-pressed', String(isDoctor));
  anal.setAttribute('aria-pressed', String(!isDoctor));
}

async function switchToDoctor() {
  state.mode = 'doctor';
  reflectModeButtons();
  try {
    await ensureFull();
    setStatus('Showing the full record (doctor view).', 'ok');
    render();
  } catch (err) {
    setStatus('Could not load the full record: ' + err.message, 'error');
  }
}

async function switchToAnalyst() {
  state.mode = 'analyst';
  reflectModeButtons();
  try {
    // Fetch the redacted view; also ensure the full view so the linkage banner
    // can compare redacted.redacted.uuid against full.uuid.
    await ensureRedacted();
    try { await ensureFull(); } catch (e) { /* banner degrades gracefully */ }
    setStatus('Showing the redacted record (analyst view).', 'ok');
    render();
  } catch (err) {
    setStatus('Could not load the redacted record: ' + err.message, 'error');
  }
}

/* ---------- rendering ---------- */

function activeDoc() {
  return state.mode === 'analyst' ? state.redacted : state.full;
}

function render() {
  var doc = activeDoc();
  if (!doc) return;
  $('viewer').hidden = false;

  renderLinkBanner();
  renderMeta(doc);
  renderAudio(doc);
  renderParties(doc);
  renderMeds(doc);
  renderSummary(doc);
  renderClinicalTranscript(doc);
  renderDialog(doc);
}

function renderLinkBanner() {
  var banner = $('link-banner');
  // Only meaningful in analyst view with both docs available.
  if (state.mode !== 'analyst' || !state.full || !state.redacted) {
    banner.hidden = true;
    banner.innerHTML = '';
    return;
  }
  var link = isSameVconLink(state.full, state.redacted);
  banner.hidden = false;
  banner.innerHTML = '';

  // HEADLINE: lead with the redaction story so an analyst immediately reads
  // "this is the de-identified record, PHI is masked" -- analogous to how the
  // doctor view's headline is the medication->audio provenance. The linkage
  // proof (uuids / pointer / content_hash) is demoted below into a collapsed
  // "Verification / provenance" section.
  var badge = document.createElement('div');
  badge.className = 'redaction-badge';
  var title = document.createElement('span');
  title.className = 'redaction-badge-title';
  title.textContent = '🔒 De-identified record — PHI masked';
  badge.appendChild(title);
  var sub = document.createElement('p');
  sub.className = 'redaction-badge-sub';
  sub.textContent =
    'This is the analyst view. Personal health information has been removed ' +
    'and replaced with [REDACTED]; the full recording audio is withheld.';
  badge.appendChild(sub);
  banner.appendChild(badge);

  // The "same vCon" assurance stays, but as a secondary, quieter line beneath
  // the headline (not the lead).
  var tag = document.createElement('span');
  if (link.linked) {
    tag.className = 'same-vcon';
    tag.textContent = '✓ Same vCon, redacted version';
  } else {
    tag.className = 'same-vcon mismatch';
    tag.textContent = '✗ Link mismatch — not confirmed the same vCon';
  }
  banner.appendChild(tag);

  // DEMOTED: the uuid-pair + pointer details live in a collapsed <details> so
  // they read as secondary verification data, not a debug dump. The full
  // copy's s3:// url is NOT surfaced plainly -- it is tucked inside here and
  // clearly labeled as an encrypted reference that is not accessible from this
  // view, so an analyst is never led to the full PHI copy's location.
  var details = document.createElement('details');
  details.className = 'verification';

  var summary = document.createElement('summary');
  summary.textContent = 'Verification / provenance';
  details.appendChild(summary);

  var pair = document.createElement('div');
  pair.className = 'uuid-pair';

  var fullBox = document.createElement('div');
  fullBox.innerHTML = '<span>Full uuid</span>';
  fullBox.appendChild(document.createTextNode(link.fullUuid || '(none)'));

  var redBox = document.createElement('div');
  redBox.innerHTML = '<span>Redacted uuid</span>';
  redBox.appendChild(document.createTextNode(link.redactedUuid || '(none)'));

  var linkBox = document.createElement('div');
  linkBox.innerHTML = '<span>redacted.redacted.uuid &rarr;</span>';
  linkBox.appendChild(document.createTextNode(link.pointerUuid || '(none)'));

  pair.appendChild(fullBox);
  pair.appendChild(redBox);
  pair.appendChild(linkBox);
  details.appendChild(pair);

  // Keep content type + content_hash (the portability/linkage proof). The
  // full-copy s3:// url is intentionally labeled, not shown as a reachable
  // location.
  var detail = document.createElement('p');
  detail.className = 'pointer-detail';
  detail.textContent =
    'pointer.type: ' + (link.pointerType || '(none)') + '\n' +
    'pointer.content_hash: ' + (link.pointerContentHash || '(none)') + '\n' +
    'full copy: encrypted full-copy reference (not accessible from this view)';
  detail.style.whiteSpace = 'pre-wrap';
  details.appendChild(detail);

  banner.appendChild(details);
}

function addMeta(dl, key, value) {
  var dt = document.createElement('dt');
  dt.textContent = key;
  var dd = document.createElement('dd');
  dd.textContent = value == null ? '' : String(value);
  dl.appendChild(dt);
  dl.appendChild(dd);
}

function renderMeta(doc) {
  var dl = $('meta');
  dl.innerHTML = '';
  addMeta(dl, 'vcon', doc.vcon);
  addMeta(dl, 'uuid', doc.uuid);
  addMeta(dl, 'subject', doc.subject);
  addMeta(dl, 'created_at', doc.created_at);
  if (doc.redacted) {
    addMeta(dl, 'redacted.type', doc.redacted.type);
    addMeta(dl, 'redacted.uuid', doc.redacted.uuid);
  }
}

function renderAudio(doc) {
  audioEl = $('audio');
  var rec = findRecordingDialog(doc);
  var note = $('audio-note');
  if (!rec || !rec.url) {
    audioEl.removeAttribute('src');
    note.textContent = 'No recording dialog object in this vCon.';
    return;
  }
  var src = rec.url;
  if (!/^s3:\/\//i.test(src)) {
    // A directly-playable url (e.g. a local fixture clip): use it as-is.
    audioEl.src = src;
    note.textContent = 'Audio source: ' + rec.url;
    return;
  }

  // 🩹 The recording url is an s3:// reference a browser cannot play. In the
  // DOCTOR view we fetch a short-lived presigned GET from /audio and stream
  // THAT; in offline/demo mode the backend has no real object and returns 404,
  // so we show a clean "not available" note.
  //
  // In the ANALYST view we deliberately do NOT request the audio: a presigned
  // GET for the full recording is the un-redacted audio, so serving it in the
  // de-identified view would undercut the redaction story. The audio stays
  // withheld there.
  audioEl.removeAttribute('src');
  if (state.mode === 'analyst') {
    note.textContent = 'Audio is withheld from the de-identified view.';
    return;
  }
  note.textContent = 'Loading audio…';
  fetchAudioUrl().then(function (url) {
    if (url) {
      audioEl.src = url;
      note.textContent = 'Recording ready.';
    } else {
      note.textContent = 'Audio is not available in offline/demo mode.';
    }
  }).catch(function () {
    note.textContent = 'Audio is not available in offline/demo mode.';
  });
}

// Ask the backend for a presigned GET url for this run's recording. Returns
// null on 404 (offline/demo mode or no recording) so renderAudio can show a
// clean note instead of erroring. Reuses the same run prefix the full/redacted
// views are fetched with; the bucket is NEVER hardcoded in the frontend.
async function fetchAudioUrl() {
  var url = '/api/records/' + state.run.split('/').map(encodeURIComponent).join('/') +
    '/audio';
  var resp = await fetch(url);
  if (!resp.ok) return null;
  var data = await resp.json();
  return data && data.url ? data.url : null;
}

function renderParties(doc) {
  var ol = $('parties');
  ol.innerHTML = '';
  var parties = doc.parties || [];
  parties.forEach(function (p, i) {
    var li = document.createElement('li');
    var label = document.createElement('strong');
    label.textContent = speakerLabelForPartyIndex(doc, i);
    li.appendChild(label);
    var meta = document.createElement('span');
    meta.className = 'party-role';
    // Escape first, then highlight any [REDACTED] masked name so the viewer
    // visibly sees which party fields the de-identification removed. Safe:
    // highlightRedactions only acts on the fixed escaped token.
    meta.innerHTML = escapeHtml(
      ' — index ' + i + ', role: ' + (p.role || '?') +
      ', name: ' + (p.name == null ? '' : p.name)
    );
    meta.innerHTML = highlightRedactions(meta.innerHTML);
    li.appendChild(meta);
    ol.appendChild(li);
  });
}

function renderMeds(doc) {
  var wrap = $('meds');
  wrap.innerHTML = '';
  var idx = findEntitiesAnalysisIndex(doc);
  if (idx === -1) {
    wrap.textContent = 'No entities analysis in this vCon.';
    return;
  }
  var entry = doc.analysis[idx];
  var items = Array.isArray(entry.body) ? entry.body : [];
  if (items.length === 0) {
    wrap.textContent = 'No entities extracted.';
    return;
  }
  items.forEach(function (ent) {
    var isMed = ent.category === 'MEDICATION';
    var chip = document.createElement('span');
    chip.className = 'chip ' + (isMed ? 'medication' : 'condition');
    chip.appendChild(document.createTextNode(ent.text || '(unknown)'));
    var cat = document.createElement('span');
    cat.className = 'cat';
    cat.textContent = (ent.category || '') +
      (typeof ent.score === 'number' ? ' · ' + ent.score : '');
    chip.appendChild(cat);
    wrap.appendChild(chip);
  });
}

function fmtTime(t) {
  return typeof t === 'number' ? t.toFixed(1) + 's' : String(t);
}

function renderSummary(doc) {
  var p = $('summary');
  var prov = $('summary-prov');
  var entry = findAnalysisByType(doc, 'summary');
  if (!entry) {
    p.textContent = '(no summary analysis)';
    prov.textContent = '';
    return;
  }
  // The Bedrock summary body is markdown. Render it as HTML via the shared
  // XSS-safe renderer (escape first, THEN apply the markdown subset + [REDACTED]
  // highlighting), so **bold**/bullets/headings display as formatting instead
  // of raw characters -- and model output can never inject markup. The
  // provenance line stays plain textContent.
  p.innerHTML = renderMarkdownSafe(entry.body || '');
  prov.textContent = Array.isArray(entry.dialog)
    ? 'Provenance: dialog turns ' + entry.dialog.join(', ')
    : '';
}

/**
 * Render the AUTHORITATIVE clinical transcript (Transcribe Medical) as the
 * primary transcript the clinician reads. It is a single plain-text blob in the
 * analysis[] transcript entry -- no per-turn/speaker structure -- so we present
 * it as a formatted text block (escape first, preserve newlines, then highlight
 * [REDACTED] so a masked body still shows the pills in the analyst view). The
 * vendor/authoritative badges mark it as the clinical-grade source. If there is
 * no authoritative transcript entry we hide the panel and leave the live
 * transcript below as the only transcript.
 */
function renderClinicalTranscript(doc) {
  var panel = $('clinical-transcript-panel');
  var badges = $('clinical-transcript-badges');
  var body = $('clinical-transcript');
  badges.innerHTML = '';
  body.innerHTML = '';

  var entry = findAuthoritativeTranscript(doc);
  if (!entry || entry.body == null || String(entry.body) === '') {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  if (entry.vendor) {
    var vendor = document.createElement('span');
    vendor.className = 'badge';
    vendor.textContent = entry.vendor;
    badges.appendChild(vendor);
  }
  if (entry.authoritative) {
    var auth = document.createElement('span');
    auth.className = 'badge authoritative';
    auth.textContent = 'authoritative';
    badges.appendChild(auth);
  }

  // The transcript is plain text, not markdown: escape it, keep its line breaks
  // (CSS white-space: pre-wrap preserves them), then highlight [REDACTED] so a
  // masked transcript reads as masked in the analyst view. XSS-safe: only the
  // escaped fixed token is wrapped.
  body.innerHTML = highlightRedactions(escapeHtml(String(entry.body)));
}

function renderDialog(doc) {
  var ol = $('dialog');
  ol.innerHTML = '';
  var dialog = doc.dialog || [];
  dialog.forEach(function (turn, i) {
    if (turn.type !== 'text') return; // the recording object is surfaced as audio
    var li = document.createElement('li');
    li.setAttribute('data-turn', String(i));
    var role = ((doc.parties || [])[turn.party] || {}).role || '';
    li.className = role === 'patient' ? 'patient' : (role === 'agent' ? 'agent' : '');

    var spk = document.createElement('span');
    spk.className = 'speaker';
    spk.textContent = speakerLabelForPartyIndex(doc, turn.party) + ':';
    li.appendChild(spk);

    // Escape the turn body first, then highlight any [REDACTED] token so masked
    // spans are visibly marked in the analyst view. XSS-safe: only the escaped
    // fixed token is wrapped; the rest stays escaped text.
    var bodyText = document.createElement('span');
    bodyText.className = 'turn-body';
    bodyText.innerHTML = highlightRedactions(
      escapeHtml(' ' + (turn.body == null ? '' : turn.body))
    );
    li.appendChild(bodyText);

    var time = document.createElement('span');
    time.className = 'turn-time';
    var win = (typeof turn.start === 'number' && typeof turn.duration === 'number')
      ? fmtTime(turn.start) + '–' + fmtTime(turn.start + turn.duration)
      : fmtTime(turn.start);
    time.textContent = '[' + win + ']';
    li.appendChild(time);

    var idx = document.createElement('span');
    idx.className = 'turn-idx';
    idx.textContent = '#' + i;
    li.appendChild(idx);

    ol.appendChild(li);
  });
}

/* ---------- init ---------- */

function init() {
  $('view-doctor-btn').addEventListener('click', switchToDoctor);
  $('view-analyst-btn').addEventListener('click', switchToAnalyst);

  if (!state.run) {
    setStatus('No record selected. Go back to the Records page and pick one.', 'error');
    return;
  }
  $('run-label').textContent = 'run: ' + state.run;
  reflectModeButtons();
  // Default to the doctor (full) view.
  switchToDoctor();
}

init();
