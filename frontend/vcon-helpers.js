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

/** The first dialog turn index an analysis entry pins to (or null). */
function firstDialogTurnIndex(entry) {
  if (entry && Array.isArray(entry.dialog) && entry.dialog.length > 0) {
    return entry.dialog[0];
  }
  return null;
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

// Export pure helpers for unit checks under Node (no effect in the browser).
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    VCON_JWS_CONTENT_TYPE: VCON_JWS_CONTENT_TYPE,
    speakerLabelForPartyIndex: speakerLabelForPartyIndex,
    resolveEntityStartSeconds: resolveEntityStartSeconds,
    firstDialogTurnIndex: firstDialogTurnIndex,
    isSameVconLink: isSameVconLink,
    findRecordingDialog: findRecordingDialog,
    findEntitiesAnalysisIndex: findEntitiesAnalysisIndex,
    findAnalysisByType: findAnalysisByType
  };
}
