/*
 * Record detail rendering + doctor/analyst toggle.
 *
 * Fetches the readable vCon JSON the backend already decrypted/verified:
 *   doctor  -> GET /api/records/{run}/full
 *   analyst -> GET /api/records/{run}/redacted
 * then renders it with the salvaged viewer logic. The pure helpers
 * (speakerLabelForPartyIndex, resolveEntityStartSeconds, isSameVconLink, ...)
 * live in vcon-helpers.js and are exposed as globals by that classic script.
 *
 * This page NEVER touches JOSE: the server did all verify/decrypt, so here we
 * only shape DOM from plain JSON. The medication->audio provenance seek and the
 * corrected full<->redacted linkage banner are the two headline behaviors.
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

var audioEl; // kept for programmatic seeking

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
  renderDialog(doc);
  renderAnalysis(doc);
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

  var h = document.createElement('h2');
  h.textContent = 'Redacted record — linked to the full record via `redacted`';
  banner.appendChild(h);

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
  banner.appendChild(pair);

  var tag = document.createElement('span');
  if (link.linked) {
    tag.className = 'same-vcon';
    tag.textContent = '✓ Same vCon, redacted version';
  } else {
    tag.className = 'same-vcon mismatch';
    tag.textContent = '✗ Link mismatch — not confirmed the same vCon';
  }
  banner.appendChild(tag);

  // Surface the real pointer details: content type, s3:// url, content_hash.
  var detail = document.createElement('p');
  detail.className = 'pointer-detail';
  detail.textContent =
    'pointer.type: ' + (link.pointerType || '(none)') + '\n' +
    'pointer.url: ' + (link.pointerUrl || '(none)') + '\n' +
    'pointer.content_hash: ' + (link.pointerContentHash || '(none)');
  detail.style.whiteSpace = 'pre-wrap';
  banner.appendChild(detail);
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
  if (/^s3:\/\//i.test(src)) {
    // 🩹 Demo: the recording url is an s3:// reference (not playable in a
    // browser) and no real audio is bundled, so the <audio> may have nothing
    // to play. The medication->seek still sets currentTime against the dialog
    // turn's numeric start so the provenance behavior is demonstrable.
    // → Production: the viewer streams the recording from a signed URL.
    audioEl.removeAttribute('src');
    note.textContent =
      '🩹 Recording url is an s3:// reference (' + rec.url + '), not playable ' +
      'in a browser, and no audio is bundled. Medication clicks still seek to ' +
      'the mapped dialog turn start (watch the audio position). → Production: ' +
      'stream from a signed URL.';
  } else {
    audioEl.src = src;
    note.textContent = 'Audio source: ' + rec.url;
  }
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
    meta.textContent = ' — index ' + i + ', role: ' + (p.role || '?') +
      ', name: ' + (p.name == null ? '' : p.name);
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
    var chip = document.createElement(isMed ? 'button' : 'span');
    chip.className = 'chip ' + (isMed ? 'medication' : 'condition');
    chip.appendChild(document.createTextNode(ent.text || '(unknown)'));
    var cat = document.createElement('span');
    cat.className = 'cat';
    cat.textContent = (ent.category || '') +
      (typeof ent.score === 'number' ? ' · ' + ent.score : '');
    chip.appendChild(cat);
    if (isMed) {
      chip.type = 'button';
      chip.title = 'Jump audio to where this was mentioned';
      chip.addEventListener('click', function () { seekToEntity(doc, idx); });
    }
    wrap.appendChild(chip);
  });
}

/**
 * Headline payoff: resolve an entities entry to the dialog turn start and seek
 * the audio element there, highlighting/scrolling to the provenance turn.
 */
function seekToEntity(doc, entitiesEntryIndex) {
  var start = resolveEntityStartSeconds(doc, entitiesEntryIndex);
  var turnIndex = firstDialogTurnIndex(doc.analysis[entitiesEntryIndex]);
  if (start == null) {
    setStatus('Could not resolve a numeric start time for that medication.', 'error');
    return;
  }
  if (audioEl) {
    try {
      audioEl.currentTime = start;
      var playPromise = audioEl.play();
      if (playPromise && typeof playPromise.catch === 'function') {
        playPromise.catch(function () { /* no audio / autoplay blocked — fine */ });
      }
    } catch (e) { /* ignore: media may not be ready */ }
  }
  highlightTurn(doc, turnIndex);
  var turn = (doc.dialog || [])[turnIndex] || {};
  var dur = typeof turn.duration === 'number' ? turn.duration : null;
  setStatus(
    'Seeked audio to ' + start.toFixed(1) + 's' +
    (dur != null ? ' (window ' + start.toFixed(1) + '–' + (start + dur).toFixed(1) + 's)' : '') +
    ' — dialog turn #' + turnIndex + '.',
    'ok'
  );
}

function highlightTurn(doc, turnIndex) {
  var ol = $('dialog');
  var lis = ol.querySelectorAll('li');
  lis.forEach(function (li) { li.classList.remove('highlight'); });
  var target = ol.querySelector('li[data-turn="' + turnIndex + '"]');
  if (target) {
    target.classList.add('highlight');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
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
  p.textContent = entry.body || '';
  prov.textContent = Array.isArray(entry.dialog)
    ? 'Provenance: dialog turns ' + entry.dialog.join(', ')
    : '';
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

    li.appendChild(document.createTextNode(' ' + (turn.body == null ? '' : turn.body)));

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

function renderAnalysis(doc) {
  var wrap = $('analysis');
  wrap.innerHTML = '';
  var analysis = doc.analysis || [];
  if (analysis.length === 0) {
    wrap.textContent = '(no analysis)';
    return;
  }
  analysis.forEach(function (entry) {
    var div = document.createElement('div');
    div.className = 'analysis-entry';

    var head = document.createElement('div');
    var type = document.createElement('span');
    type.className = 'analysis-type';
    type.textContent = entry.type || '(untyped)';
    head.appendChild(type);

    if (entry.vendor) {
      var vendor = document.createElement('span');
      vendor.className = 'badge';
      vendor.textContent = entry.vendor;
      head.appendChild(vendor);
    }
    if (entry.authoritative) {
      var auth = document.createElement('span');
      auth.className = 'badge authoritative';
      auth.textContent = 'authoritative';
      head.appendChild(auth);
    }
    if (entry.degraded) {
      var deg = document.createElement('span');
      deg.className = 'badge degraded';
      deg.textContent = 'degraded: ' + entry.degraded;
      head.appendChild(deg);
    }
    div.appendChild(head);

    if (Array.isArray(entry.dialog)) {
      var prov = document.createElement('div');
      prov.className = 'provenance';
      prov.textContent = 'dialog turn provenance: ' + entry.dialog.join(', ');
      div.appendChild(prov);
    }

    var body = document.createElement('div');
    body.className = 'body-text';
    if (Array.isArray(entry.body)) {
      body.textContent = entry.body.map(function (e) {
        return e.category + ': ' + e.text +
          (typeof e.score === 'number' ? ' (' + e.score + ')' : '');
      }).join('; ');
    } else {
      body.textContent = entry.body == null ? '' : String(entry.body);
    }
    div.appendChild(body);

    wrap.appendChild(div);
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
