"""The Volatility Surface page's inline scripts parse (v4.208). The page's JavaScript is edited by hand and
checked by substring tests; this renders /options and hands every inline <script> (not the JSON data blocks)
to `node --check`, so a stray quote or brace fails here rather than in a member's browser. Skipped when Node
is not on the PATH."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.test_options_page import client, fake  # noqa: F401 - the page fixtures live there

_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here")
def test_every_inline_script_on_the_options_page_parses(client, fake, tmp_path):
    html = client.get("/options").text
    assert "function renderSurface(" in html
    checked = 0
    for m in _SCRIPT_RE.finditer(html):
        attrs, body = m.group("attrs"), m.group("body")
        if "src=" in attrs or "application/json" in attrs or not body.strip():
            continue
        path = tmp_path / ("script_%d.js" % checked)
        path.write_text(body, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, "inline script %d does not parse:\n%s" % (checked, r.stderr[:1500])
        checked += 1
    assert checked >= 3
