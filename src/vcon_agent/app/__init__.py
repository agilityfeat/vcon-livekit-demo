"""Local end-to-end demo backend for the vCon + LiveKit intake demo.

This subpackage holds the FastAPI backend that ties the whole demo together:
a human patient places a real intake call to the existing bot, and the
doctor / analyst viewers read the produced vCon records.

It is split into two layers so the correctness-critical logic stays
framework-independent and offline-testable, mirroring the rest of the repo
(``vcon.py`` / ``persistence.py`` / ``sealing.py``):

* :mod:`vcon_agent.app.records` -- the PURE business logic. No ``fastapi``,
  no top-level ``boto3`` / ``livekit`` imports; all AWS / LiveKit / JOSE access
  sits behind injected seams and lazy imports, so it imports and unit-tests
  with no network and no boto3 / livekit installed.
* :mod:`vcon_agent.app.main` -- a THIN FastAPI route layer that imports
  ``fastapi`` and the pure helpers, and serves the static ``frontend/`` so the
  whole demo runs from a single ``uvicorn`` process.
"""

__all__ = ["records"]
