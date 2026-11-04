/*
 * Record detail rendering.
 *
 * Which role's report renders is decided by the ?view= link param set by the
 * dashboard that linked here (?view=analyst -> redacted; absent/other ->
 * doctor/full default). There is no on-page toggle: the page renders the one
 * report its URL selects.
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

/* ---------- view selection ---------- */

async function switchToDoctor() {
  state.mode = 'doctor';
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
  // The whole Call audio panel is doctor-view-only. The recording is the
  // un-redacted audio, so it has no place in the de-identified (analyst)
  // record: in analyst mode we hide the entire #audio-panel section -- no
  // heading, no <audio> control, no note -- and return before reading the
  // recording url or requesting any presigned GET. Doctor mode re-shows the
  // panel and renders it exactly as below. render() re-runs renderAudio on
  // every toggle, so flipping back to doctor un-hides the panel here.
  var panel = $('audio-panel');
  if (state.mode === 'analyst') {
    if (panel) panel.hidden = true;
    return;
  }
  if (panel) panel.hidden = false;

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
  // so we show a clean "not available" note. (The analyst view never reaches
  // here: renderAudio hides the whole panel and returns up top, so no presigned
  // GET for the un-redacted audio is ever issued from the de-identified view.)
  audioEl.removeAttribute('src');
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
 * Derive a GENERIC speaker label from a Transcribe `spk_N` label.
 *
 * Used only when a structured turn's `party` is null (reconciliation could not
 * resolve patient/agent), so we must NOT guess clinical roles. Maps 'spk_0' ->
 * 'Speaker 1', 'spk_1' -> 'Speaker 2', generally 'spk_N' -> 'Speaker '+(N+1),
 * and falls back to the raw label (or 'Speaker') for anything unexpected.
 */
function genericSpeakerLabel(speaker) {
  var m = /^spk_(\d+)$/.exec(String(speaker == null ? '' : speaker));
  if (m) return 'Speaker ' + (parseInt(m[1], 10) + 1);
  return speaker ? String(speaker) : 'Speaker';
}

/**
 * Render the AUTHORITATIVE clinical transcript (Transcribe Medical) as the
 * primary transcript the clinician reads.
 *
 * When the authoritative analysis[] entry carries a structured `turns` array
 * (speaker-attributed, timestamped segments produced by the pipeline) we render
 * it as speaker-attributed turns -- an <ol>/<li> list mirroring the live
 * transcript (renderDialog) -- so the clinical transcript reads like turns
 * rather than a wall of text. Each turn is labeled by its party
 * (speakerLabelForPartyIndex) when `party` is a number, otherwise by a generic
 * 'Speaker N' derived from the spk_N label; we NEVER guess patient/agent when
 * party is null.
 *
 * When there are no structured turns (older records, the canned flat path, or
 * unmapped output) we fall back to the flat-text `body` block (escape first,
 * preserve newlines via CSS white-space: pre-wrap, then highlight [REDACTED]).
 * The vendor/authoritative badges mark it as the clinical-grade source. The
 * panel is hidden only when there is NEITHER a body NOR structured turns.
 *
 * XSS-safe throughout: untrusted text is escaped FIRST, then highlightRedactions
 * wraps only the fixed escaped token before anything is assigned to innerHTML.
 */
function renderClinicalTranscript(doc) {
  var panel = $('clinical-transcript-panel');
  var badges = $('clinical-transcript-badges');
  var body = $('clinical-transcript');
  badges.innerHTML = '';
  body.innerHTML = '';

  var entry = findAuthoritativeTranscript(doc);
  var turns = entry ? authoritativeTranscriptTurns(entry) : null;
  var hasBody = !!entry && entry.body != null && String(entry.body) !== '';
  if (!entry || (!hasBody && !turns)) {
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

  if (turns) {
    // Structured path: render speaker-attributed turns, reusing the live
    // transcript's <ol>/<li> + .speaker + highlightRedactions(escapeHtml(...))
    // pattern so the clinical transcript reads like turns.
    body.className = 'clinical-transcript';
    var ol = document.createElement('ol');
    ol.className = 'dialog clinical-turns';
    turns.forEach(function (turn) {
      var li = document.createElement('li');
      var label;
      if (typeof turn.party === 'number') {
        var role = ((doc.parties || [])[turn.party] || {}).role || '';
        li.className = role === 'patient' ? 'patient' : (role === 'agent' ? 'agent' : '');
        label = speakerLabelForPartyIndex(doc, turn.party);
      } else {
        // party unresolved: use the generic spk_N label, never a clinical role.
        label = genericSpeakerLabel(turn.speaker);
      }

      var spk = document.createElement('span');
      spk.className = 'speaker';
      spk.textContent = label + ':';
      li.appendChild(spk);

      // Escape the turn text first, then highlight [REDACTED]. XSS-safe: only
      // the fixed escaped token is wrapped; the rest stays escaped text.
      var bodyText = document.createElement('span');
      bodyText.className = 'turn-body';
      bodyText.innerHTML = highlightRedactions(
        escapeHtml(' ' + (turn.text == null ? '' : turn.text))
      );
      li.appendChild(bodyText);

      ol.appendChild(li);
    });
    body.appendChild(ol);
    return;
  }

  // Flat fallback: plain text, not markdown. Escape it, keep its line breaks
  // (CSS white-space: pre-wrap preserves them), then highlight [REDACTED] so a
  // masked transcript reads as masked in the analyst view. XSS-safe: only the
  // escaped fixed token is wrapped.
  body.className = 'clinical-transcript body-text';
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
  if (!state.run) {
    setStatus('No record selected. Go back to your dashboard and pick one.', 'error');
    return;
  }
  $('run-label').textContent = 'run: ' + state.run;
  // The ?view= link param decides which report renders: a link from the analyst
  // dashboard (?view=analyst) arrives straight in the redacted view; any other
  // value (including absent, e.g. the doctor dashboard's &view=doctor) keeps the
  // doctor/full default. There is no on-page toggle to switch after load.
  var view = new URLSearchParams(window.location.search).get('view');
  if (view === 'analyst') {
    switchToAnalyst();
  } else {
    switchToDoctor();
  }
}

init();
