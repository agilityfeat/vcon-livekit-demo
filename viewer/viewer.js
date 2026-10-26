/*
 * vCon viewer logic — vanilla JS, no framework, no CDN, fully offline.
 *
 * The three pure helpers the headline behaviors rely on are factored out near
 * the top so they are readable and could be unit-checked in isolation:
 *
 *   - speakerLabelForPartyIndex(vcon, partyIndex)
 *   - resolveEntityStartSeconds(vcon, entitiesEntryIndex)
 *   - isSameVconLink(fullDoc, redactedDoc)
 *
 * Everything below those is DOM rendering and event wiring.
 */

'use strict';

/* ------------------------------------------------------------------ *
 * Pure helpers (no DOM, no network) — the testable core.
 * ------------------------------------------------------------------ */

/**
 * Map a dialog turn's party INDEX to a human speaker label.
 *
 * Dialog/attachments reference parties by index (0 = patient, 1 = agent).
 * We label by the party's `role` so a redacted doc (names masked, roles kept)
 * still produces 'Patient' / 'Intake Agent'. Falls back to any name, then to a
 * generic "Party N".
 */
function speakerLabelForPartyIndex(vcon, partyIndex) {
  var parties = (vcon && vcon.parties) || [];
  var party = parties[partyIndex];
  if (!party) {
    return 'Party ' + partyIndex;
  }
  var role = (party.role || '').toLowerCase();
  if (role === 'patient') return 'Patient';
  if (role === 'agent') return 'Intake Agent';
  if (party.name && party.name !== '[REDACTED]') return party.name;
  if (party.role) return party.role;
  return 'Party ' + partyIndex;
}

/**
 * Resolve an `entities` analysis entry to the audio seek time in seconds.
 *
 * Provenance chain: entities entry -> its `dialog` turn index -> that dialog
 * turn's numeric `start`. Returns null if anything is missing or non-numeric so
 * callers can decide not to seek.
 */
function resolveEntityStartSeconds(vcon, entitiesEntryIndex) {
  var analysis = (vcon && vcon.analysis) || [];
  var entry = analysis[entitiesEntryIndex];
  if (!entry || !Array.isArray(entry.dialog) || entry.dialog.length === 0) {
    return null;
  }
  var turnIndex = entry.dialog[0];
  var dialog = (vcon && vcon.dialog) || [];
  var turn = dialog[turnIndex];
  if (!turn || typeof turn.start !== 'number' || !isFinite(turn.start)) {
    return null;
  }
  return turn.start;
}

/**
 * The first dialog turn index an analysis entry pins to (or null).
 * Used for highlighting the provenance turn when seeking.
 */
function firstDialogTurnIndex(entry) {
  if (entry && Array.isArray(entry.dialog) && entry.dialog.length > 0) {
    return entry.dialog[0];
  }
  return null;
}

/**
 * Decide whether a full doc and a redacted doc are the SAME vCon, linked via
 * the standard `redacted` field. The visible invariant:
 *   full.redacted.uuid === redacted.uuid
 * and, when present, the redacted doc's back-reference names the full uuid.
 * Returns a descriptive object so the UI can show WHY it matches (or not).
 */
function isSameVconLink(fullDoc, redactedDoc) {
  var fullLink = (fullDoc && fullDoc.redacted) || {};
  var backLink = (redactedDoc && redactedDoc.redacted) || {};
  var forwardOk = !!fullLink.uuid && fullLink.uuid === (redactedDoc && redactedDoc.uuid);
  var backOk = !backLink.uuid || backLink.uuid === (fullDoc && fullDoc.uuid);
  return {
    linked: forwardOk && backOk,
    forwardOk: forwardOk,
    backOk: backOk,
    fullUuid: fullDoc && fullDoc.uuid,
    redactedUuid: redactedDoc && redactedDoc.uuid,
    forwardTargetUuid: fullLink.uuid || null,
    backReferenceUuid: backLink.uuid || null
  };
}

/**
 * Resolve where to fetch the redacted sibling from, given how the full doc was
 * loaded and the full doc's `redacted.url`. Pure (no DOM/network) so it can be
 * unit-checked. Returns an object describing the resolution:
 *   { ok: true, url: <string> }   — a URL/path to fetch
 *   { ok: false, reason: <code> } — cannot resolve; caller should fall back
 * `source` is state.source: { kind: 'sample' | 'url' | 'file', base?: string }.
 */
function resolveRedactedSiblingUrl(source, linkUrl) {
  if (!linkUrl) {
    return { ok: false, reason: 'no-link' };
  }
  // An absolute URL in the link resolves to itself regardless of source.
  if (/^https?:\/\//i.test(linkUrl) || /^\//.test(linkUrl)) {
    return { ok: true, url: linkUrl };
  }
  var kind = source && source.kind;
  if (kind === 'sample') {
    // Bundled sample: the redacted sibling lives under viewer/samples/.
    return { ok: true, url: 'samples/' + linkUrl };
  }
  if (kind === 'url' && source.base) {
    // Resolve the (relative) sibling against the full doc's own URL.
    return { ok: true, url: resolveRelativeUrl(source.base, linkUrl) };
  }
  if (kind === 'file') {
    // Loaded from a local file: there is no base URL to resolve a relative
    // sibling against, so the caller must fall back to the file picker.
    return { ok: false, reason: 'no-base' };
  }
  return { ok: false, reason: 'unknown-source' };
}

/**
 * Resolve a (possibly relative) url against a base url, string-only so it works
 * under Node without the DOM. Replaces the base's last path segment with the
 * relative url (standard relative-reference resolution for a plain filename).
 */
function resolveRelativeUrl(base, relative) {
  if (/^https?:\/\//i.test(relative) || /^\//.test(relative)) {
    return relative;
  }
  var hash = base.indexOf('#');
  if (hash !== -1) base = base.slice(0, hash);
  var query = base.indexOf('?');
  if (query !== -1) base = base.slice(0, query);
  var slash = base.lastIndexOf('/');
  if (slash === -1) {
    // base is a bare filename with no directory component.
    return relative;
  }
  return base.slice(0, slash + 1) + relative;
}

/** Find the single `recording` dialog object (or null). */
function findRecordingDialog(vcon) {
  var dialog = (vcon && vcon.dialog) || [];
  for (var i = 0; i < dialog.length; i++) {
    if (dialog[i] && dialog[i].type === 'recording') return dialog[i];
  }
  return null;
}

/** Index of the first `entities` analysis entry (or -1). */
function findEntitiesAnalysisIndex(vcon) {
  var analysis = (vcon && vcon.analysis) || [];
  for (var i = 0; i < analysis.length; i++) {
    if (analysis[i] && analysis[i].type === 'entities') return i;
  }
  return -1;
}

/** First analysis entry of a given type (or null). */
function findAnalysisByType(vcon, type) {
  var analysis = (vcon && vcon.analysis) || [];
  for (var i = 0; i < analysis.length; i++) {
    if (analysis[i] && analysis[i].type === type) return analysis[i];
  }
  return null;
}

/* ------------------------------------------------------------------ *
 * App state + DOM wiring.
 * ------------------------------------------------------------------ */

var state = {
  full: null,       // the full vCon doc currently loaded
  redacted: null,   // the redacted sibling (lazily fetched for analyst view)
  mode: 'physician', // 'physician' | 'analyst'
  // Where the current full doc came from, so the `redacted` sibling can be
  // resolved against the right base:
  //   { kind: 'sample' }           — bundled sample under samples/
  //   { kind: 'url', base: <url> } — fetched from a URL/path; resolve siblings against it
  //   { kind: 'file' }             — local file picker; no base URL to resolve against
  source: null
};

var SAMPLE_URL = 'samples/vcon-full.sample.json';

function $(id) { return document.getElementById(id); }

function setStatus(msg, kind) {
  var el = $('status');
  el.textContent = msg || '';
  el.className = 'status' + (kind ? ' ' + kind : '');
}

/** Resolve a (possibly relative) url against the currently loaded doc's base. */
function resolveSampleSibling(relativeUrl) {
  // The samples live under viewer/samples/; the full sample's redacted link is
  // a relative sibling filename, so resolve it against the samples dir.
  return 'samples/' + relativeUrl;
}

/* ---------- loading ---------- */

function loadSample() {
  setStatus('Loading bundled sample…');
  fetch(SAMPLE_URL)
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (doc) { onVconLoaded(doc, 'bundled sample', { kind: 'sample' }); })
    .catch(function (err) {
      setStatus(
        'Could not fetch the bundled sample (' + err.message + '). ' +
        'If you opened this file via file://, the browser blocks the fetch — ' +
        'use the local file picker to open samples/vcon-full.sample.json instead.',
        'error'
      );
    });
}

function loadFromFile(file) {
  var reader = new FileReader();
  reader.onload = function () {
    try {
      var doc = JSON.parse(reader.result);
      onVconLoaded(doc, 'local file: ' + file.name, { kind: 'file' });
    } catch (e) {
      setStatus('Could not parse JSON from ' + file.name + ': ' + e.message, 'error');
    }
  };
  reader.onerror = function () { setStatus('Could not read file ' + file.name, 'error'); };
  reader.readAsText(file);
}

function loadFromUrl(url) {
  if (!url) { setStatus('Enter a URL or path first.', 'error'); return; }
  setStatus('Loading ' + url + '…');
  fetch(url)
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (doc) { onVconLoaded(doc, url, { kind: 'url', base: url }); })
    .catch(function (err) {
      setStatus('Could not load ' + url + ': ' + err.message, 'error');
    });
}

/** Called whenever a (full) vCon is loaded from any of the three sources. */
function onVconLoaded(doc, sourceLabel, source) {
  state.full = doc;
  state.redacted = null; // reset; re-fetched lazily when the analyst view needs it
  state.source = source || { kind: 'file' };
  state.mode = 'physician';
  reflectModeButtons();
  setStatus('Loaded ' + sourceLabel + '.', 'ok');
  $('viewer').hidden = false;
  render();
}

/* ---------- mode toggle ---------- */

function reflectModeButtons() {
  var phys = $('view-physician-btn');
  var anal = $('view-analyst-btn');
  var isPhys = state.mode === 'physician';
  phys.classList.toggle('active', isPhys);
  anal.classList.toggle('active', !isPhys);
  phys.setAttribute('aria-pressed', String(isPhys));
  anal.setAttribute('aria-pressed', String(!isPhys));
}

function switchToPhysician() {
  if (!state.full) { setStatus('Load a vCon first.', 'error'); return; }
  state.mode = 'physician';
  reflectModeButtons();
  render();
}

function switchToAnalyst() {
  if (!state.full) { setStatus('Load a vCon first.', 'error'); return; }
  state.mode = 'analyst';
  reflectModeButtons();
  if (state.redacted) {
    render();
    return;
  }
  // Follow the full record's `redacted` link to fetch the sibling file.
  var link = state.full.redacted;
  if (!link || !link.url) {
    setStatus(
      'The full record has no `redacted` link with a url. Pick/paste the ' +
      'redacted vCon with the file picker or URL box to view it.',
      'error'
    );
    render();
    return;
  }
  var resolved = resolveRedactedSiblingUrl(state.source, link.url);
  if (!resolved.ok) {
    setStatus(
      'The full record links to a redacted sibling (' + link.url + '), but it ' +
      'was loaded from a local file, so there is no base URL to resolve that ' +
      'relative link against. Open the redacted vCon with the file picker, or ' +
      'paste its URL in the URL box. Showing structure only.',
      'error'
    );
    render();
    return;
  }
  var url = resolved.url;
  setStatus('Following `redacted` link to ' + link.url + '…');
  fetch(url)
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (doc) {
      state.redacted = doc;
      setStatus('Loaded redacted record (' + link.url + ').', 'ok');
      render();
    })
    .catch(function (err) {
      setStatus(
        'Could not fetch the redacted sibling (' + err.message + '). Under ' +
        'file:// the browser blocks it — use the file picker to open ' +
        link.url + ', or serve the repo over http. Showing structure only.',
        'error'
      );
      render();
    });
}

/* ---------- rendering ---------- */

/** The doc currently being displayed given the mode. */
function activeDoc() {
  if (state.mode === 'analyst' && state.redacted) return state.redacted;
  return state.full;
}

function render() {
  var doc = activeDoc();
  if (!doc) return;

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
  linkBox.innerHTML = '<span>full.redacted.uuid &rarr;</span>';
  linkBox.appendChild(document.createTextNode(link.forwardTargetUuid || '(none)'));

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

  if (link.backReferenceUuid) {
    var back = document.createElement('p');
    back.className = 'note';
    back.textContent =
      'Redacted doc back-reference (redacted_from) → ' + link.backReferenceUuid +
      (link.backOk ? ' (matches full uuid)' : ' (does NOT match full uuid)');
    banner.appendChild(back);
  }
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

var audioEl; // reference kept for programmatic seeking

function renderAudio(doc) {
  audioEl = $('audio');
  var rec = findRecordingDialog(doc);
  var note = $('audio-note');
  if (!rec || !rec.url) {
    audioEl.removeAttribute('src');
    note.textContent = 'No recording dialog object in this vCon.';
    return;
  }
  // 🩹 Demo: if the recording url is an s3:// uri (unplayable in a browser),
  // fall back to the bundled placeholder path under samples/. In this repo no
  // real audio is bundled, so the <audio> may have nothing to play — the seek
  // still sets currentTime against the dialog turn's numeric start.
  var src = rec.url;
  if (/^s3:\/\//i.test(src)) {
    src = resolveSampleSibling('recording.sample.ogg');
    note.textContent =
      'Recording url is an s3:// reference (not playable in a browser); using ' +
      'the bundled placeholder path. Seeks still target the dialog start time.';
  } else if (!/^https?:\/\//i.test(src) && src.indexOf('/') === -1) {
    // Bare relative filename like "recording.sample.ogg": resolve under samples/.
    src = resolveSampleSibling(src);
    note.textContent =
      '🩹 Placeholder audio (' + rec.url + '): no real patient audio is bundled, ' +
      'so there may be nothing to play. Medication clicks still seek to the ' +
      'mapped dialog turn start (watch the audio position).';
  } else {
    note.textContent = 'Audio source: ' + rec.url;
  }
  audioEl.src = src;
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
    cat.textContent = (ent.category || '') + (typeof ent.score === 'number' ? ' · ' + ent.score : '');
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
  // Seek the audio element even if no file is actually loaded; currentTime is
  // still set (and will be clamped/applied once media metadata is available).
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
      // entities: list category/text pairs
      body.textContent = entry.body.map(function (e) {
        return e.category + ': ' + e.text + (typeof e.score === 'number' ? ' (' + e.score + ')' : '');
      }).join('; ');
    } else {
      body.textContent = entry.body == null ? '' : String(entry.body);
    }
    div.appendChild(body);

    wrap.appendChild(div);
  });
}

/* ------------------------------------------------------------------ *
 * Event wiring.
 * ------------------------------------------------------------------ */

function init() {
  $('load-sample-btn').addEventListener('click', loadSample);
  $('file-input').addEventListener('change', function (e) {
    var f = e.target.files && e.target.files[0];
    if (f) loadFromFile(f);
  });
  $('load-url-btn').addEventListener('click', function () {
    loadFromUrl($('url-input').value.trim());
  });
  $('url-input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') loadFromUrl($('url-input').value.trim());
  });
  $('view-physician-btn').addEventListener('click', switchToPhysician);
  $('view-analyst-btn').addEventListener('click', switchToAnalyst);
  setStatus('Load a vCon to begin (bundled sample, local file, or URL).');
}

// Bootstrap only in a browser; under Node (unit checks) there is no `document`.
if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}

// Export pure helpers for unit checks under Node (no effect in the browser).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    speakerLabelForPartyIndex: speakerLabelForPartyIndex,
    resolveEntityStartSeconds: resolveEntityStartSeconds,
    isSameVconLink: isSameVconLink,
    findRecordingDialog: findRecordingDialog,
    findEntitiesAnalysisIndex: findEntitiesAnalysisIndex,
    findAnalysisByType: findAnalysisByType,
    resolveRedactedSiblingUrl: resolveRedactedSiblingUrl,
    resolveRelativeUrl: resolveRelativeUrl
  };
}
