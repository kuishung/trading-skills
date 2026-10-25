"""``GET /logo/<SYM>`` - a ticker's logo (v4.221, ``services/logos.py``).

The PNG the server stored for the ticker (fetched once from a public logo service), or, when no service has one,
a small SVG with the ticker's first letters - so an ``<img src="/logo/AMD">`` always shows a badge. Signed-in
members only; the browser may keep the answer a week. v4.222: the answer never waits for a download (see
``services/logos.py``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..models import User
from ..security import require_user
from ..services import logos

router = APIRouter(tags=["logos"])


@router.get("/logo/{symbol}")
async def ticker_logo(symbol: str, user: User = Depends(require_user)):
    """v4.222: never waits for a download - the stored logo, else the letters badge at once (the browser keeps it two
    minutes, a ticker no service knows a day) with the ticker queued at the front for the background worker."""
    png = await run_in_threadpool(logos.stored_png, symbol)
    if png:
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=604800"})
    sym = logos.clean_symbol(symbol)
    known_none = bool(sym) and logos.is_fresh(sym)          # a recent "no logo anywhere"
    if sym and not known_none:
        logos.enqueue([sym], front=True)
    return Response(logos.initials_svg(symbol), media_type="image/svg+xml",
                    headers={"Cache-Control": "private, max-age=%d" % (86400 if known_none else 120)})
