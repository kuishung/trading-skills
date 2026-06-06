"""Pipeline — the nightly ingest -> deep-check -> profiles report, plus
moderator-triggered profile regen (full or ad-hoc per-ticker) so nobody has to
log into Hermes.

The report reads the per-run manifests the ingest supervisor writes (see
``services/pipeline_runs``). Triggers spawn ``scripts/regen_profiles.py``
DETACHED with the data-science ``py -3.12`` interpreter (the one that has
pyarrow/numpy — NOT the uvicorn venv), so a 7-minute full regen never ties up
the web worker. The run drops a manifest that shows up on this same page.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..security import require_moderator, require_user
from ..services import pipeline_runs
from ..services import profiles as profiles_svc
from ..services.resources_bridge import TRADEHUNTER_ROOT

router = APIRouter(tags=["pipeline"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_KINDS = ("intraday", "swing", "both")


def _py() -> str:
    """The interpreter that has pyarrow/numpy (data-science), NOT the uvicorn
    venv. On Hermes that's C:\\Windows\\py.exe (used with -3.12)."""
    return shutil.which("py") or r"C:\Windows\py.exe"


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, ticker: str = "", user=Depends(require_user)):
    """Swing/Trend profile lookup — type a ticker, see its swing profile."""
    data = profiles_svc.swing_profile(ticker) if ticker else None
    return templates.TemplateResponse(request, "profile.html", {
        "user": user,
        "configured": profiles_svc.configured(),
        "ticker": (ticker or "").strip().upper(),
        "prof": data,
        "rows": profiles_svc.display_rows(data) if data else [],
        "count": len(profiles_svc.available()),
    })


@router.get("/api/profile/{ticker}")
def api_profile(ticker: str, user=Depends(require_user)):
    """JSON swing profile (also consumed by the Nous agent for the daily brief)."""
    return {"ticker": ticker.strip().upper(), "swing": profiles_svc.swing_profile(ticker)}


@router.get("/api/pipeline-runs")
def api_pipeline_runs(user=Depends(require_user)):
    runs = pipeline_runs.list_runs()
    if runs is None:
        return JSONResponse({"configured": False, "runs": []})
    return {"configured": True, "runs": runs}


@router.post("/pipeline/regen")
def trigger_regen(request: Request,
                  kind: str = Form("swing"),
                  tickers: str = Form(""),
                  user=Depends(require_moderator)):
    """Spawn a detached profile regen. Empty `tickers` => full universe (--all);
    otherwise an ad-hoc per-ticker run."""
    if kind not in _KINDS:
        kind = "intraday"
    runner = TRADEHUNTER_ROOT / "scripts" / "regen_profiles.py"
    syms = [s.strip().upper() for s in tickers.replace(",", " ").split() if s.strip()]
    cmd = [_py(), "-3.12", str(runner), kind]
    cmd += syms if syms else ["--all"]
    cmd += ["--manifest"]
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(cmd, cwd=str(TRADEHUNTER_ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except Exception as exc:
        return RedirectResponse(f"/finviz?err={type(exc).__name__}", status_code=303)
    scope = ",".join(syms) if syms else "all"
    return RedirectResponse(f"/finviz?started={kind}:{scope}", status_code=303)
