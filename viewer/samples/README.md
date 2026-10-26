# Sample vCon fixtures (Spec 5 viewer + portability CLI)

Static, offline sample data the viewer and the portability CLI load directly
from disk. No build step, no network, no AWS.

## Files

- `vcon-full.sample.json` — a realistic FULL enriched vCon in the Spec 0/3 shape
  (`vcon` 0.0.1, `uuid`, `subject`, `created_at`, `parties` [patient, agent],
  `dialog`, `analysis`, `attachments`). It carries the standard `redacted`
  forward link pointing at the redacted sibling.
- `vcon-redacted.sample.json` — the output of
  `vcon_agent.redaction.build_redacted_pair` run on the full sample. PHI free
  text (party names, dialog bodies, transcript/summary analysis bodies, consent
  script) is masked to `[REDACTED]`, while structure, dialog count, numeric
  `start`/`duration`, analysis order/types, each analysis entry's `dialog`
  turn-index provenance, and the `entities` (meds/conditions) payload are kept.
  It carries a `redacted` back-reference naming the full doc's `uuid`.

The two files stay consistent via `tests/test_redaction.py`, which loads both
and asserts `full.redacted.uuid == redacted.uuid`, the back-reference, and that
the redacted bodies are masked, so the committed assets cannot silently drift
from the helper.

## Numeric timing (why it matters)

Unlike the live `VConBuilder` (which defaults dialog `start` to an ISO-8601
string), these hand-authored fixtures carry NUMERIC `start` (seconds) and a
numeric `duration` on every text turn. That is intentional and required: the
viewer's medication → audio-seek resolves an entity to its analysis entry's
`dialog` turn index, reads that turn's numeric `start`/`duration`, and sets the
audio element's `currentTime`. The `entities` analysis pins to dialog turn index
`1` (the patient turn mentioning "headache" and "ibuprofen"), which is a real
text turn with a numeric `start`, so the click-through resolves to an audio
moment.

## 🩹 Placeholder audio

The `recording` dialog object references `recording.sample.ogg` as a relative
`url`. No real audio is bundled in this repo (there is no recorded patient call
to ship). The viewer still wires the seek to the dialog turn's numeric `start`;
with no audio file present the `<audio>` element simply has nothing to play, but
the seek target computation is exercised. → Production: the `recording` dialog
`url` points at the real call audio in S3 and the viewer streams it.
