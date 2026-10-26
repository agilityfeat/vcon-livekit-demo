# Viewer + portability — the payoff

The viewer makes the benefits of capturing a call as a standard [vCon](https://vcon.dev/)
**visible**: one record, provenance you can click, two linked views (full and
redacted), and portability to a second, independent tool.

It ships two pieces, both fully local and offline:

- a **minimal static web viewer** (`index.html` + `viewer.js` + `viewer.css`)
  that loads a vCon JSON and renders parties / dialog / analysis, and
- a tiny **portability CLI** (`portability_cli.py`) — the "second tool" — that
  reads the same record back through the independent `vcon` PyPI library.

> All commands below are written to be run from the **repository root** (the
> parent of this `viewer/` folder), which is why paths are prefixed with
> `viewer/`.

## What it delivers

1. **One object.** A single vCon carries the parties, the dialog, the recording
   reference, and every analysis (transcript, entities, summary) with provenance.
2. **Provenance click-through.** In the physician view, clicking an extracted
   medication jumps the audio to the exact moment it was mentioned.
3. **Redacted ↔ full visibly linked.** The analyst view shows the de-identified
   record and proves it is the *same* vCon via the standard `redacted` link.
4. **Signed vCon usable in a second tool.** The portability CLI loads the record
   with an independent library (and can verify a signed copy), so the vCon "just
   works" outside the tool that produced it.

## The viewer

### How to open it

The viewer fetches the bundled sample over `fetch()`, which browsers block under
`file://`. Serve the repo over a simple static server and open the `viewer/`
path:

```bash
python3 -m http.server 8000      # from the repository root
# then open http://localhost:8000/viewer/ in a browser
```

Once it is open you have **three ways to load a vCon**, all offline:

- **Bundled sample** — click *Load bundled sample* to load
  `viewer/samples/vcon-full.sample.json`.
- **Local file** — use the file picker to open any vCon JSON from disk. This
  path uses `FileReader` (not `fetch`), so it works even under `file://` if you
  opened `index.html` directly.
- **URL** — paste a URL (or server-relative path) to a vCon JSON and load it.

### Physician view — provenance click-through

The physician view renders a **speaker-attributed transcript** (parties labelled
by `role`, so it reads as *Patient* / *Intake Agent*), the physician **summary**,
and the extracted **medications**. Clicking a medication chip is the headline
payoff:

> medication → its `entities` analysis entry → that entry's `dialog` turn index
> → that dialog turn's numeric `start` / `duration` → **seek the audio element**
> to that moment (and highlight the originating turn).

That chain is exactly the turn-index provenance the enrichment pipeline wrote into
`analysis[]`, read straight off the record with no extra bookkeeping.

> ⚠️ The seek depends on a **numeric** dialog `start` (seconds). The bundled
> fixtures carry numeric timing on purpose, but the live `VConBuilder`
> (`src/vcon_agent/vcon.py`) defaults dialog `start` to an **ISO-8601 string**,
> so a vCon straight from the real pipeline would not click through — the seek
> reports "could not resolve a numeric start time" instead. This is the same
> caveat documented in `viewer/samples/README.md`. → *Production:* normalize
> dialog timing to numeric seconds (or carry both) so the click-through works on
> live records too.

> 🩹 The bundled `recording.sample.ogg` is a **placeholder path**: no real
> patient audio is committed, so there may be nothing to play. The seek still
> sets the audio element's `currentTime` to the mapped dialog-turn start, which
> is what the provenance demo is about. → *Production:* the real recording
> (served from access-controlled storage) plays and seeks for real.

### Analyst view — redacted linked to full

Switching to the analyst view follows the full record's `redacted` link
(`redacted.url`) to fetch the redacted sibling and renders the de-identified
record. The sibling is resolved against **how the full doc was loaded**: for the
bundled sample it is fetched from `viewer/samples/`; for a doc loaded by URL a
relative link is resolved against that URL; for a doc loaded from the local file
picker (where there is no base URL to resolve a relative link against) the viewer
asks you to open the redacted sibling with the file picker or URL box instead of
silently guessing a path. With the bundled sample this fetches
`viewer/samples/vcon-redacted.sample.json` and renders the de-identified record:
party names, dialog bodies, and the transcript/summary text are masked to
`[REDACTED]`, while **structure, turn indices, and the meds/conditions payload
are preserved**. A banner proves the two documents are the same vCon:

```
full.redacted.uuid === redacted.uuid
```

and the redacted doc's `redacted_from` back-reference names the full doc's uuid,
so the link holds from both sides. Because the redacted record keeps every
analysis entry's `dialog` turn-index provenance, the medication → audio-seek
click-through still works on the redacted view too.

## The portability CLI (the "second tool")

`portability_cli.py` proves portability by loading the vCon with the independent
`vcon` PyPI library — not with any repo code — and reading the standard fields
back through the library's own accessors (`.uuid`, `.subject`, `.created_at`,
`.parties`, `.dialog`, `.analysis` / `find_analysis_by_type`, `.redacted`). If a
tool that has never seen this repo can map our bytes onto the vCon model and
surface the same fields, the record is genuinely portable.

### Install the library locally and run it

The `vcon` library is **not** a project dependency (it is the external second
tool), and this sandbox's site-packages is read-only, so install it into a local
target directory and put that on `PYTHONPATH`:

```bash
pip3 install --target .vconlib vcon
PYTHONPATH=.vconlib python3 viewer/portability_cli.py \
    viewer/samples/vcon-full.sample.json
```

Example output:

```
vCon loaded via the `vcon` library (second, independent tool).
  uuid:        11111111-1111-4111-8111-111111111111
  subject:     Patient intake call
  created_at:  2024-01-01T15:04:05+00:00
  parties:     2
  dialog:      5 entries
  analysis:    transcript, entities, summary
  redacted:    redacted -> 22222222-2222-4222-8222-222222222222 (url: vcon-redacted.sample.json)
  is_valid():  False (N strict-schema issue(s); expected for the demo 0.0.1 shape, record still read fine)
```

The CLI accepts a filesystem path or an `http(s)` URL, and takes an optional
`--sign` flag that generates a key pair, signs the loaded vCon, and verifies it
in the same tool — the "signed vCon usable in a second tool" story:

```bash
PYTHONPATH=.vconlib python3 viewer/portability_cli.py --sign \
    viewer/samples/vcon-full.sample.json
# ... adds a "signature: verified" line to the output.
```

Importing the library emits a harmless `AuthlibDeprecationWarning` to stderr;
that is expected and does not affect the read.

### Why `is_valid()` reports `False`

The library's strict `is_valid()` rejects our demo `0.0.1` shape (it wants
ISO-8601 dialog `start`, specific dialog mimetypes, and `encoding` on
attachments/analysis). That is **expected** and does **not** mean the record is
unreadable — the library parses it and exposes every standard field fine, which
is what portability means here. The CLI therefore **reports** the validation
outcome for transparency but never hard-fails on it (see the shortcut below).

### Tests

The CLI's core logic is factored into importable functions (`load_vcon`,
`build_report`, `format_report`) so `tests/test_portability_cli.py` exercises it
without a subprocess. Those tests `pytest.importorskip("vcon")`, so the base
offline suite stays green when the library is absent; when it is present they run
against the bundled full + redacted samples:

```bash
# with the library on the path (see install above)
PYTHONPATH=.vconlib:src python3 -m pytest tests/test_portability_cli.py -q
# the base offline suite still passes without the library:
PYTHONPATH=src python3 -m pytest tests/ -q
```

## Acknowledged shortcuts (demo) vs. production

- 🩹 **Single-page local viewer, no auth, reads a vCon from a URL/file.** →
  *Production:* an access-controlled app where the full record vs. the redacted
  record is gated by role (a physician sees PHI, an analyst only the redacted
  view).
- 🩹 **The portability demo is a CLI read, not a full Conserver deployment.** →
  *Production:* push the vCon to a [Conserver](https://vcon.dev/) with real
  lifecycle and consent workflows, rather than a one-shot read.
- 🩹 **Demo-grade, field-level redaction and a hand-authored redacted sample**
  (a fixed field list masked with `[REDACTED]`; the redacted sibling is a
  committed static fixture). → *Production:* a vetted de-identification pipeline
  (e.g. Comprehend Medical `DetectPHI`-driven redaction) with a configurable
  policy that finds PHI embedded anywhere in free text.
- 🩹 **The CLI reports but does not enforce the `vcon` library's strict
  `is_valid()`**, because the demo uses the `0.0.1` shape the strict validator
  rejects (it still loads and reads fine). → *Production:* emit spec-conformant
  vCons that pass strict validation, so a consumer can gate on `is_valid()`.
