#!/usr/bin/env python3
"""Portability demo: the Spec 5 "second tool" that reads a vCon via the library.

The payoff of Spec 5 is portability: once a call is captured as a standard vCon,
you can drop it into ANY vCon-aware tool and it just works. This tiny CLI is that
second tool. It does NOT use any repo code to parse the record; instead it loads
the vCon with the independent ``vcon`` PyPI library (``from vcon import Vcon``)
and reads the STANDARD fields back through the library's own accessors
(``.uuid`` / ``.subject`` / ``.created_at`` / ``.parties`` / ``.dialog`` /
``.analysis`` / ``.find_analysis_by_type`` / ``.redacted``). If the library can
load our record and expose those fields, the record is genuinely portable, not
just readable by the tool that wrote it.

Why read THROUGH the library (not ``json.load``): anyone can re-parse their own
JSON. The point here is that a tool that has never seen this repo, written by a
different author, maps our bytes onto the vCon data model and surfaces the same
fields. That is the portability claim demo.md section 7 asks us to prove.

🩹 Shortcut: this portability demo is a CLI READ, not a full Conserver
deployment, and it REPORTS but does NOT enforce the library's strict
``is_valid()``. The ``vcon`` library's ``is_valid()`` rejects our demo ``0.0.1``
shape (it wants ISO-8601 dialog ``start``, specific dialog mimetypes, and
``encoding`` on attachments/analysis), returning ``(False, [errors...])``. That
is EXPECTED for the demo shape and does NOT mean the record is unreadable: the
library parses it and exposes every standard field fine, which is what
portability means here. So the CLI prints the validation outcome for
transparency but never hard-fails on it.
→ Production: push to a Conserver with real lifecycle / consent workflows, and
emit spec-conformant vCons that pass the library's strict ``is_valid()``.

Run it offline against the bundled sample (install the library to a local target
first, since this sandbox's site-packages is read-only)::

    pip3 install --target .vconlib vcon
    PYTHONPATH=.vconlib python3 viewer/portability_cli.py \
        viewer/samples/vcon-full.sample.json

The optional ``--sign`` flag demonstrates the "signed vCon usable in a second
tool" story using the library's ``generate_key_pair`` + ``sign`` + ``verify``.
It is OPTIONAL so the core read path needs no keys.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# The ``vcon`` PyPI library is an OPTIONAL, external dependency (not a pyproject
# requirement): it is the independent "second tool". Import errors are turned
# into an actionable message pointing at the local-target install the README and
# this module's docstring document, rather than a bare traceback.
try:
    from vcon import Vcon
except ImportError as exc:  # pragma: no cover - exercised only without the lib
    Vcon = None  # type: ignore[assignment]
    _IMPORT_ERROR: Optional[ImportError] = exc
else:
    _IMPORT_ERROR = None


@dataclass
class PortabilityReport:
    """Structured result of reading a vCon back through the ``vcon`` library.

    Every field is read via a library accessor (never by re-parsing the JSON
    ourselves) so the report is evidence the record is portable: the fields exist
    because an independent tool mapped our bytes onto the vCon model. Tests assert
    on this object directly, so the CLI logic is exercised without a subprocess.
    """

    uuid: Optional[str]
    subject: Optional[str]
    created_at: Optional[str]
    party_count: int
    dialog_count: int
    analysis_types: List[str]
    redacted: Optional[Dict[str, Any]]
    is_valid: bool
    validation_errors: List[str] = field(default_factory=list)
    signature_verified: Optional[bool] = None

    @property
    def redacted_uuid(self) -> Optional[str]:
        """The uuid the ``redacted`` link points at, if a link is present.

        On a FULL doc this is the redacted record's uuid (``type: redacted``); on
        a redacted doc it is the origin full record's uuid
        (``type: redacted_from``). Either way it is what lets the viewer prove the
        two documents are the same vCon.
        """
        if isinstance(self.redacted, dict):
            return self.redacted.get("uuid")
        return None


def load_vcon(source: str) -> "Vcon":
    """Load a vCon from a local path or an ``http(s)`` URL via the library.

    The record is read as JSON text and handed to ``Vcon.build_from_json`` so the
    LIBRARY does the parsing and model-mapping, which is the whole point of the
    portability demo. A ``url`` that starts with ``http`` is fetched; anything
    else is treated as a filesystem path (mirroring the viewer's URL-or-file load
    options).
    """
    if Vcon is None:  # pragma: no cover - exercised only without the lib
        raise RuntimeError(
            "The 'vcon' library is not importable. Install it to a local target "
            "and put it on PYTHONPATH:\n"
            "    pip3 install --target .vconlib vcon\n"
            "    PYTHONPATH=.vconlib python3 viewer/portability_cli.py <path>"
        ) from _IMPORT_ERROR

    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source) as resp:  # noqa: S310 - demo tool
            raw = resp.read().decode("utf-8")
    else:
        with open(source, "r", encoding="utf-8") as handle:
            raw = handle.read()

    return Vcon.build_from_json(raw)


def build_report(vcon: "Vcon", *, verify_public_key: Optional[str] = None) -> PortabilityReport:
    """Read the standard vCon fields back through the library into a report.

    This is the importable core the tests call directly (no subprocess). It reads
    uuid / subject / created_at / parties / dialog / analysis / redacted via the
    library's accessors and records the library's ``is_valid()`` outcome WITHOUT
    treating a ``False`` as fatal: the demo ``0.0.1`` shape is expected to fail
    strict validation while remaining fully readable (see the module docstring).
    """
    # The analysis types are read in document order so the report shows the
    # pipeline's deterministic transcript -> entities -> summary ordering. We
    # also exercise find_analysis_by_type below to prove type-addressed lookup
    # works through the library.
    analysis = vcon.analysis or []
    analysis_types = [entry.get("type") for entry in analysis if entry.get("type")]

    # is_valid() returns a (bool, errors) tuple for our 0.0.1 shape; older/other
    # shapes may return a bare bool. Normalize both without ever hard-failing.
    valid_result = vcon.is_valid()
    if isinstance(valid_result, tuple):
        is_valid = bool(valid_result[0])
        errors = list(valid_result[1]) if len(valid_result) > 1 and valid_result[1] else []
    else:
        is_valid = bool(valid_result)
        errors = []

    signature_verified: Optional[bool] = None
    if verify_public_key is not None:
        # Optional: prove a signed vCon verifies in this second tool.
        signature_verified = bool(vcon.verify(verify_public_key))

    return PortabilityReport(
        uuid=vcon.uuid,
        subject=vcon.subject,
        created_at=vcon.created_at,
        party_count=len(vcon.parties or []),
        dialog_count=len(vcon.dialog or []),
        analysis_types=[str(t) for t in analysis_types],
        redacted=vcon.redacted,
        is_valid=is_valid,
        validation_errors=[str(e) for e in errors],
        signature_verified=signature_verified,
    )


def format_report(report: PortabilityReport) -> str:
    """Render a human-readable summary of what the second tool could read.

    Kept separate from ``build_report`` so the structured data and its printable
    form are independently testable.
    """
    lines = [
        "vCon loaded via the `vcon` library (second, independent tool).",
        f"  uuid:        {report.uuid}",
        f"  subject:     {report.subject}",
        f"  created_at:  {report.created_at}",
        f"  parties:     {report.party_count}",
        f"  dialog:      {report.dialog_count} entries",
        f"  analysis:    {', '.join(report.analysis_types) or '(none)'}",
    ]

    if report.redacted:
        link_type = report.redacted.get("type", "redacted")
        lines.append(
            f"  redacted:    {link_type} -> {report.redacted_uuid}"
            + (
                f" (url: {report.redacted['url']})"
                if report.redacted.get("url")
                else ""
            )
        )
    else:
        lines.append("  redacted:    (no link on this record)")

    if report.signature_verified is not None:
        lines.append(
            f"  signature:   {'verified' if report.signature_verified else 'INVALID'}"
        )

    # Report-but-do-not-enforce the library's strict validation (see docstring).
    if report.is_valid:
        lines.append("  is_valid():  True")
    else:
        lines.append(
            "  is_valid():  False "
            f"({len(report.validation_errors)} strict-schema issue(s); "
            "expected for the demo 0.0.1 shape, record still read fine)"
        )

    return "\n".join(lines)


def run(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code.

    Exits non-zero ONLY on genuine failures (the record cannot be loaded, or a
    requested signature does not verify). A ``False`` strict ``is_valid()`` for
    the demo shape is reported, not treated as a failure.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Load a vCon with the `vcon` library and read its standard fields, "
            "proving the record is portable to a second vCon-aware tool."
        )
    )
    parser.add_argument(
        "source",
        help="Path to a vCon JSON file, or an http(s) URL to one.",
    )
    parser.add_argument(
        "--sign",
        action="store_true",
        help=(
            "Demo the signed-vCon story: generate a key pair, sign the loaded "
            "vCon, then verify it in this same tool (optional; the core read "
            "path needs no keys)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        vcon = load_vcon(args.source)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: could not load vCon from {args.source!r}: {exc}", file=sys.stderr)
        return 1

    verify_public_key: Optional[str] = None
    if args.sign:
        # Sign, then verify, round-trip entirely in this second tool to show a
        # signed vCon is usable end to end without the record's original author.
        private_key, public_key = Vcon.generate_key_pair()
        vcon.sign(private_key)
        verify_public_key = public_key

    report = build_report(vcon, verify_public_key=verify_public_key)
    print(format_report(report))

    if report.signature_verified is False:
        print("error: signature did not verify", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - thin shell around run()
    raise SystemExit(run())
