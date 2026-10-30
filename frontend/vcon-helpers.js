/*
 * Pure, DOM-free vCon helpers shared by the record detail page.
 *
 * These are the correctness-critical bits of the viewer, factored out so they
 * stay readable and could be unit-checked under Node (they take plain objects
 * and return plain values -- no DOM, no network). The page script
 * (record.js) imports the DOM rendering; this file holds only the logic.
 *
 * Adapted from the standalone viewer. The ONE important adaptation is
 * isSameVconLink: in the backend's REAL seal/redacted model, ONLY the redacted
 * copy carries a top-level `redacted` pointer back to the full copy (the full
 * copy has none). That is the INVERSE of the stale standalone viewer, which
 * read full.redacted.uuid. So "same vCon" is confirmed here when
 * redacted.redacted.uuid === full.uuid AND redacted.redacted.type ===
 * 'application/vcon+jws'.
 */

'use strict';

var VCON_JWS_CONTENT_TYPE = 'application/vcon+jws';

/**
 * Map a dialog turn's party INDEX to a human speaker label.
 *
 * Dialog/attachments reference parties by index (0 = patient, 1 = agent). We
 * label by the party's `role` so a redacted doc (names masked, roles kept)
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
 * Decide whether a full doc and a redacted doc are the SAME vCon.
 *
 * REAL model (the inverse of the stale standalone viewer): only the REDACTED
 * copy carries the `redacted` pointer, and it points back at the full copy. So
 * the link is confirmed when:
 *   redacted.redacted.uuid === full.uuid    (pointer targets the full copy)
 *   redacted.redacted.type === 'application/vcon+jws'
 * Returns a descriptive object so the UI can show WHY it matches (or not) and
 * surface the pointer's content_hash + s3:// url.
 */
function isSameVconLink(fullDoc, redactedDoc) {
  var pointer = (redactedDoc && redactedDoc.redacted) || {};
  var fullUuid = fullDoc && fullDoc.uuid;
  var uuidOk = !!pointer.uuid && pointer.uuid === fullUuid;
  var typeOk = pointer.type === VCON_JWS_CONTENT_TYPE;
  return {
    linked: uuidOk && typeOk,
    uuidOk: uuidOk,
    typeOk: typeOk,
    fullUuid: fullUuid || null,
    redactedUuid: (redactedDoc && redactedDoc.uuid) || null,
    pointerUuid: pointer.uuid || null,
    pointerType: pointer.type || null,
    pointerUrl: pointer.url || null,
    pointerContentHash: pointer.content_hash || null
  };
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

/**
 * Find the AUTHORITATIVE transcript analysis entry (or null).
 *
 * The pipeline emits more than one transcript-like surface: the LIVE dialog
 * turns (Deepgram STT, kept as dialog[] text turns) and the clinical-grade
 * Transcribe Medical transcript (an analysis entry of type 'transcript' with
 * `authoritative` set and a plain-text `body` blob). The record view
 * foregrounds that authoritative transcript, so we pick the transcript entry
 * whose `authoritative` is truthy. If several transcript entries exist and none
 * is marked authoritative, falls back to the first transcript entry so callers
 * still have something to show.
 *
 * The entry MAY now ALSO carry a structured `turns` array (speaker-attributed,
 * timestamped turns reconciled by the pipeline) alongside the flat `body`. The
 * flat `body` is always preserved for back-compat; use
 * authoritativeTranscriptTurns(entry) to read the structured turns when present.
 */
function findAuthoritativeTranscript(vcon) {
  var analysis = (vcon && vcon.analysis) || [];
  var firstTranscript = null;
  for (var i = 0; i < analysis.length; i++) {
    var entry = analysis[i];
    if (entry && entry.type === 'transcript') {
      if (entry.authoritative) return entry;
      if (!firstTranscript) firstTranscript = entry;
    }
  }
  return firstTranscript;
}

/**
 * Return the structured speaker turns on an authoritative transcript entry.
 *
 * The pipeline may up-level the authoritative transcript from a flat text blob
 * into speaker-attributed turns (each ``{party?, speaker, start, end, text}``,
 * as produced by enrichment.reconcile_segments_to_parties). Those turns live on
 * the SAME analysis entry under `turns`. Returns that array when it is present
 * and non-empty, else null so callers fall back to the flat `body` block.
 *
 * `party` may be null when reconciliation could not resolve patient/agent (the
 * live dialog turns had non-numeric starts), in which case only the `speaker`
 * (spk_N) label is meaningful -- callers must NOT guess patient/agent then.
 */
function authoritativeTranscriptTurns(entry) {
  var turns = entry && entry.turns;
  if (Array.isArray(turns) && turns.length > 0) return turns;
  return null;
}

/* ---------- XSS-safe text / markdown rendering ---------- */

/**
 * Escape the five HTML-significant characters so untrusted record/model text
 * can never inject tags, attributes, or scripts when assigned via innerHTML.
 *
 * This is the FIRST step for every innerHTML path in the viewer: escape the raw
 * text, THEN apply safe structural transforms (markdown, [REDACTED] pills) to
 * the already-escaped string. Raw text is never handed to innerHTML directly.
 */
function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Wrap every literal ``[REDACTED]`` token in a highlight pill so an analyst
 * instantly SEES what the de-identification removed.
 *
 * XSS-safe by construction: the caller passes ALREADY-ESCAPED HTML, and we only
 * replace the (equally escaped) literal token with a <span> whose text is the
 * fixed, trusted string '[REDACTED]'. No untrusted text is introduced.
 */
function highlightRedactions(escapedHtml) {
  return String(escapedHtml == null ? '' : escapedHtml).replace(
    /\[REDACTED\]/g,
    '<span class="redacted-token">[REDACTED]</span>'
  );
}

/**
 * Render a SMALL, safe subset of markdown to HTML for the Bedrock summary.
 *
 * The Bedrock summaries use a limited markdown subset, so rather than pull in a
 * CDN renderer + sanitizer (and break the no-build ethos) we handle just that
 * subset inline: headings (#, ##, ###), bold (double-star / double-underscore),
 * italics (single-star / single-underscore), unordered lists (-, *), ordered
 * lists (1.), inline code (backticks), and paragraphs / line breaks.
 *
 * CRITICAL XSS DISCIPLINE: the source text is HTML-escaped FIRST, then the
 * markdown transforms run over the ESCAPED text and only ever emit a fixed set
 * of safe tags. Model output therefore cannot inject markup -- any '<', '>', or
 * '&' it contained is already neutralized before a single tag is added. The
 * resulting string is safe to assign to innerHTML. [REDACTED] tokens are also
 * highlighted so a masked summary reads as masked.
 */
function renderMarkdownSafe(markdown) {
  var escaped = escapeHtml(markdown);
  var lines = escaped.split(/\r?\n/);
  var html = [];
  var listType = null; // 'ul' | 'ol' | null

  function closeList() {
    if (listType) {
      html.push('</' + listType + '>');
      listType = null;
    }
  }

  function inline(text) {
    // Inline code first so emphasis markers inside backticks are left alone.
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold before italics so '**x**' is not eaten by the single-char rules.
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    text = text.replace(/_([^_]+)_/g, '<em>$1</em>');
    // Highlight redaction tokens after emphasis so the pill wraps cleanly.
    return highlightRedactions(text);
  }

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    var trimmed = line.trim();

    if (trimmed === '') {
      closeList();
      continue;
    }

    var heading = /^(#{1,3})\s+(.*)$/.exec(trimmed);
    if (heading) {
      closeList();
      var level = heading[1].length;
      html.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
      continue;
    }

    var ordered = /^\d+\.\s+(.*)$/.exec(trimmed);
    if (ordered) {
      if (listType !== 'ol') {
        closeList();
        html.push('<ol>');
        listType = 'ol';
      }
      html.push('<li>' + inline(ordered[1]) + '</li>');
      continue;
    }

    var unordered = /^[-*]\s+(.*)$/.exec(trimmed);
    if (unordered) {
      if (listType !== 'ul') {
        closeList();
        html.push('<ul>');
        listType = 'ul';
      }
      html.push('<li>' + inline(unordered[1]) + '</li>');
      continue;
    }

    closeList();
    html.push('<p>' + inline(trimmed) + '</p>');
  }
  closeList();
  return html.join('');
}

// Export pure helpers for unit checks under Node (no effect in the browser).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    VCON_JWS_CONTENT_TYPE: VCON_JWS_CONTENT_TYPE,
    speakerLabelForPartyIndex: speakerLabelForPartyIndex,
    isSameVconLink: isSameVconLink,
    findRecordingDialog: findRecordingDialog,
    findEntitiesAnalysisIndex: findEntitiesAnalysisIndex,
    findAnalysisByType: findAnalysisByType,
    findAuthoritativeTranscript: findAuthoritativeTranscript,
    authoritativeTranscriptTurns: authoritativeTranscriptTurns,
    escapeHtml: escapeHtml,
    highlightRedactions: highlightRedactions,
    renderMarkdownSafe: renderMarkdownSafe
  };
}
