"""Thin FastAPI route layer for the local end-to-end demo backend.

This module is intentionally THIN: each route parses the request, calls a PURE
helper in :mod:`vcon_agent.app.records`, and returns JSON. ALL business logic
(S3 listing, server-side decrypt + verify, the offline fixtures fallback, the
LiveKit token claim building) lives in ``records.py`` so it stays framework-
independent and offline-testable. ``fastapi`` is imported HERE only, never in
``records.py``, so the pure core imports and unit-tests with no FastAPI / boto3
/ livekit installed (this module still ``py_compile``s even when FastAPI is
absent in a sandbox).

Run the whole demo from a single process once the ``[app]`` extra is installed:

    pip install -e '.[app]'
    PYTHONPATH=src:infrastructure/functions/seal uvicorn vcon_agent.app.main:app

Endpoints:
  * ``POST /api/token``                      -- mint a patient LiveKit token.
  * ``GET  /api/records``                    -- list call records.
  * ``GET  /api/records/{run_prefix}/full``  -- doctor full (decrypted) vCon.
  * ``GET  /api/records/{run_prefix}/redacted`` -- analyst redacted vCon.
The static ``frontend/`` directory is mounted at ``/`` so the single ``uvicorn``
process serves both the API and the UI.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Reuse the shared cwd-independent .env loader so the backend reads the SAME
# local .env (LiveKit creds, BUCKET_NAME, the demo signing key) as the agent,
# without importing the LiveKit-dependent agent module.
from ..config import ENV_LIVEKIT_URL, load_env
from . import livekit_tokens
from . import records as records_mod

# src/vcon_agent/app/main.py -> repo root is four parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_DIR = _REPO_ROOT / "frontend"


class TokenRequest(BaseModel):
    """Body for ``POST /api/token``: which room the patient joins and as whom."""

    room: str
    identity: str
    name: Optional[str] = None


def create_app(
    env: Optional[Dict[str, str]] = None,
    store: Optional[records_mod.RecordsStore] = None,
) -> FastAPI:
    """Build the FastAPI app, wiring the routes to the pure helpers.

    ``env`` / ``store`` are injectable for testing; by default the local ``.env``
    is loaded and the records store is constructed from the environment (S3 when
    ``BUCKET_NAME`` is set, local fixtures otherwise). The demo signing key is
    constructed ONCE here at startup and reused for every decrypt/verify.
    """
    if env is None:
        load_env()
        env = os.environ
    if store is None:
        store = records_mod.build_records_store(env=env)

    app = FastAPI(title="vCon LiveKit demo backend")

    # 🩹 Shortcut: CORS is wide open so the static frontend can call the API
    # during local dev regardless of host/port. → Production: restrict origins
    # to the known frontend origin(s) and drop the wildcard.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/api/token")
    def mint_token(req: TokenRequest) -> Dict[str, Any]:
        """Mint a LiveKit patient access token for the Start-call flow.

        The LiveKit server ``url`` is returned alongside the token (read from
        the SAME ``.env`` the agent uses) so the browser client never hardcodes
        the server URL and always targets the same LiveKit server as the agent.
        """
        token = livekit_tokens.mint_patient_token(
            req.room, req.identity, name=req.name, env=env
        )
        return {
            "token": token,
            "room": req.room,
            "identity": req.identity,
            "url": env.get(ENV_LIVEKIT_URL) or "",
        }

    @app.get("/api/records")
    def list_records() -> Dict[str, Any]:
        """List available call records (S3 scan or offline fixtures)."""
        descriptors = store.list_records()
        return {"records": [d.to_dict() for d in descriptors]}

    @app.get("/api/records/{run_prefix:path}/full")
    def get_full(run_prefix: str) -> Dict[str, Any]:
        """Return the decoded FULL vCon (doctor view) for a run prefix."""
        try:
            doc = store.full_record(run_prefix)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except records_mod.RecordKeyMismatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return records_mod.shape_record_for_frontend(doc)

    @app.get("/api/records/{run_prefix:path}/redacted")
    def get_redacted(run_prefix: str) -> Dict[str, Any]:
        """Return the verified REDACTED vCon (analyst view) for a run prefix."""
        try:
            doc = store.redacted_record(run_prefix)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except records_mod.RecordKeyMismatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return records_mod.shape_record_for_frontend(doc)

    # Serve the static frontend from the same process. Guarded so the app still
    # imports/starts before the frontend/ directory is authored.
    if _FRONTEND_DIR.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(_FRONTEND_DIR), html=True),
            name="frontend",
        )

    return app


# Module-level app for ``uvicorn vcon_agent.app.main:app``.
app = create_app()
