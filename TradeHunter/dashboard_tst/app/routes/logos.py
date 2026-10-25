"""``GET /logo/<SYM>`` - a ticker's logo (v4.221, ``services/logos.py``).

The PNG the server stored for the ticker (fetched once from a public logo service), or, when no service has one,
a small SVG with the ticker's first letters - so an ``<img src="/logo/AMD">`` always shows a badge. Signed-in
members only; the browser may keep the answer a week (a day for the letters badge).
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
    png = await run_in_threadpool(logos.logo_png, symbol)
    if png:
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=604800"})
    return Response(logos.initials_svg(symbol), media_type="image/svg+xml",
                    headers={"Cache-Control": "private, max-age=86400"})
