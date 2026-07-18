// TradeHunter — TV Bridge
// -----------------------------------------------------------------------------
// A tiny, ZERO-DEPENDENCY local sidecar that lets the TradeHunter web app draw
// MATP / MBP levels on the user's OWN TradingView (web, in Chrome).
//
// Why this exists (see CLAUDE.md + resources/tradingview-mcp/bridge/README.md):
//   The TradingView MCP drives TV over Chrome DevTools Protocol (CDP), which is
//   localhost-only. TradeHunter is served from Hermes, so the SERVER can never
//   reach a user's local TV. Instead, each user runs THIS bridge on their own
//   machine; the MATP page fetches http://127.0.0.1:9223/plot?... and the bridge
//   drives that user's own Chrome. => every user draws on their OWN chart; the
//   server never touches a TV.
//
// Zero dependencies on purpose (portability): uses only Node built-ins —
//   node:http + global fetch + global WebSocket (Node >= 22). No `npm install`,
//   so the whole thing travels with the Dropbox-synced repo and runs on any PC
//   with Node >= 22. (Node here is v24; fine.)
//
// It reuses the EXACT chart calls the MCP core uses:
//   chart = window.TradingViewApi._activeChartWidgetWV.value()
//   chart.setSymbol(sym, {})
//   chart.createShape({time, price}, {shape:'horizontal_line', overrides:{...}})
//   chart.getAllShapes() / chart.removeEntity(id)
//
// Idempotent by design: it tracks the shape ids it created per symbol on a page
// global (window.__TH_LINES) and removes ITS OWN previous MATP/MBP lines before
// redrawing — so clicking a ticker again REFRESHES the two lines in place, never
// stacks duplicates, and never touches the user's manual drawings.
// -----------------------------------------------------------------------------

import http from 'node:http';

const BRIDGE_PORT = Number(process.env.TH_TV_BRIDGE_PORT || 9223);
const CDP_PORT = Number(process.env.TH_TV_CDP_PORT || 9222);
const CDP_HOST = process.env.TH_TV_CDP_HOST || '127.0.0.1';

const CHART_API = 'window.TradingViewApi._activeChartWidgetWV.value()';

// --- CDP: find the TradingView chart tab in Chrome and eval JS in it ---------

async function findTvTarget() {
  let list;
  try {
    const r = await fetch(`http://${CDP_HOST}:${CDP_PORT}/json/list`);
    list = await r.json();
  } catch (e) {
    throw new Error(
      `Can't reach Chrome's debug port on ${CDP_HOST}:${CDP_PORT}. ` +
      `Launch Chrome with --remote-debugging-port=${CDP_PORT} (use launch_tv_bridge.bat).`
    );
  }
  const pages = (list || []).filter((t) => t.type === 'page');
  const t =
    pages.find((t) => /tradingview\.com\/chart/i.test(t.url || '')) ||
    pages.find((t) => /tradingview/i.test(t.url || ''));
  if (!t || !t.webSocketDebuggerUrl) {
    throw new Error(
      'No TradingView chart tab found in Chrome. Open https://www.tradingview.com/chart/ ' +
      '(logged in) in the debug Chrome, then try again.'
    );
  }
  return t;
}

// Evaluate an expression in the TV page. awaitPromise=true so a Promise result
// resolves before we read it. One fresh WebSocket per call — simple + robust.
function cdpEval(expression, awaitPromise = true, timeoutMs = 9000) {
  return new Promise((resolve, reject) => {
    findTvTarget()
      .then((target) => {
        let settled = false;
        const ws = new WebSocket(target.webSocketDebuggerUrl);
        const finish = (fn, arg) => {
          if (settled) return;
          settled = true;
          try { ws.close(); } catch { /* ignore */ }
          fn(arg);
        };
        const timer = setTimeout(
          () => finish(reject, new Error('CDP eval timed out (chart not ready?)')),
          timeoutMs
        );
        ws.addEventListener('open', () => {
          ws.send(JSON.stringify({
            id: 1,
            method: 'Runtime.evaluate',
            params: { expression, returnByValue: true, awaitPromise },
          }));
        });
        ws.addEventListener('message', (ev) => {
          let msg;
          try { msg = JSON.parse(ev.data); } catch { return; }
          if (msg.id !== 1) return;
          clearTimeout(timer);
          if (msg.error) return finish(reject, new Error(msg.error.message || 'CDP error'));
          const r = msg.result;
          if (r && r.exceptionDetails) {
            const d = r.exceptionDetails;
            return finish(reject, new Error(d.exception?.description || d.text || 'JS eval error'));
          }
          finish(resolve, r && r.result ? r.result.value : undefined);
        });
        ws.addEventListener('error', () => {
          clearTimeout(timer);
          finish(reject, new Error('CDP WebSocket error'));
        });
      })
      .catch(reject);
  });
}

// --- The plot expression (runs inside the TV page) ---------------------------
// Removes our own previous MATP/MBP lines for THIS symbol, sets the symbol if
// needed, then draws the two horizontal lines and records their ids.

function buildPlotExpr({ symbol, matp, mbp }) {
  const S = JSON.stringify(symbol);
  const MATP = matp == null ? 'null' : Number(matp);
  const MBP = mbp == null ? 'null' : Number(mbp);
  return `
(function () {
  return new Promise(function (resolve) {
    try {
      var chart = ${CHART_API};
      if (!chart) return resolve({ ok: false, error: 'TradingView chart API not available on this tab' });
      var SYM = ${S};
      var MATP = ${MATP};
      var MBP = ${MBP};
      window.__TH_LINES = window.__TH_LINES || {};
      // key cleanup on the bare symbol (ignore EXCHANGE: prefix)
      var KEY = SYM.indexOf(':') >= 0 ? SYM.split(':').pop().toUpperCase() : SYM.toUpperCase();

      function draw() {
        var api = ${CHART_API};
        // 1) remove OUR previous lines for this symbol (leaves manual drawings alone)
        var prev = window.__TH_LINES[KEY] || [];
        prev.forEach(function (id) { try { api.removeEntity(id); } catch (e) {} });
        window.__TH_LINES[KEY] = [];
        // 2) pick a visible bar time for the anchor point (line spans the chart anyway)
        var t;
        try { var vr = api.getVisibleRange(); t = Math.floor((vr.from + vr.to) / 2); }
        catch (e) { t = Math.floor(Date.now() / 1000); }
        // 3) snapshot ids, draw, then diff after a settle to capture the new ids
        var before = api.getAllShapes().map(function (s) { return s.id; });
        function line(price, color, label) {
          api.createShape({ time: t, price: price }, {
            shape: 'horizontal_line',
            overrides: { linecolor: color, linewidth: 2, linestyle: 0,
                         showLabel: true, textcolor: color, fontsize: 11,
                         horzLabelsAlign: 'right', text: label },
            text: label
          });
        }
        if (MATP != null) line(MATP, '#f97316', 'MATP ' + MATP);
        if (MBP != null) line(MBP, '#16a34a', 'MBP ' + MBP);
        setTimeout(function () {
          var after = api.getAllShapes().map(function (s) { return s.id; });
          var added = after.filter(function (id) { return before.indexOf(id) < 0; });
          window.__TH_LINES[KEY] = (window.__TH_LINES[KEY] || []).concat(added);
          resolve({ ok: true, symbol: SYM, added: added.length, total: after.length });
        }, 350);
      }

      // set the symbol first if the chart isn't already on it, then draw
      var cur = '';
      try { cur = (chart.symbol() || '').toUpperCase(); } catch (e) {}
      if (cur && cur.indexOf(KEY) >= 0) {
        draw();
      } else {
        try { chart.setSymbol(SYM, {}); } catch (e) { return resolve({ ok: false, error: 'setSymbol failed: ' + e.message }); }
        setTimeout(draw, 900);
      }
    } catch (e) {
      resolve({ ok: false, error: e.message });
    }
  });
})()
`;
}

// --- HTTP server -------------------------------------------------------------

function send(res, code, obj, cors) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': cors || '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function sendHtml(res, html) {
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(html);
}

const server = http.createServer(async (req, res) => {
  const origin = req.headers.origin || '*';
  const url = new URL(req.url, `http://127.0.0.1:${BRIDGE_PORT}`);

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    });
    return res.end();
  }

  // Health: is the bridge up and can it see a TV tab?
  if (url.pathname === '/health') {
    try {
      const t = await findTvTarget();
      return send(res, 200, { ok: true, bridge: 'up', tv_tab: t.url }, origin);
    } catch (e) {
      return send(res, 200, { ok: false, bridge: 'up', tv_tab: null, error: e.message }, origin);
    }
  }

  // Plot MATP / MBP on the user's TV.
  if (url.pathname === '/plot') {
    const symbol = (url.searchParams.get('symbol') || '').trim().toUpperCase();
    const exchange = (url.searchParams.get('exchange') || '').trim().toUpperCase();
    const matpRaw = url.searchParams.get('matp');
    const mbpRaw = url.searchParams.get('mbp');
    const nav = url.searchParams.get('nav') === '1'; // window.open fallback wants HTML
    const matp = matpRaw != null && matpRaw !== '' && matpRaw !== '-' ? Number(matpRaw) : null;
    const mbp = mbpRaw != null && mbpRaw !== '' && mbpRaw !== '-' ? Number(mbpRaw) : null;

    if (!symbol) {
      if (nav) return sendHtml(res, htmlResult(false, 'missing symbol'));
      return send(res, 400, { ok: false, error: 'missing symbol' }, origin);
    }
    const tvSym = exchange ? `${exchange}:${symbol}` : symbol;

    try {
      const result = await cdpEval(buildPlotExpr({ symbol: tvSym, matp, mbp }));
      if (nav) return sendHtml(res, htmlResult(!!(result && result.ok), (result && result.error) || '', symbol));
      return send(res, 200, result || { ok: false, error: 'no result' }, origin);
    } catch (e) {
      if (nav) return sendHtml(res, htmlResult(false, e.message, symbol));
      return send(res, 502, { ok: false, error: e.message }, origin);
    }
  }

  return send(res, 404, { ok: false, error: 'not found' }, origin);
});

// Tiny self-closing page for the window.open() fallback (used only if a browser
// blocks the direct loopback fetch as mixed content).
function htmlResult(ok, err, sym) {
  const msg = ok ? `Plotted ${sym || ''} on TradingView ✓` : `Couldn't plot: ${err || 'error'}`;
  return `<!doctype html><meta charset="utf-8"><title>TV bridge</title>
<body style="font:14px system-ui;background:#0f172a;color:#e2e8f0;margin:0;display:grid;place-items:center;height:100vh">
<div style="text-align:center">${msg}<div style="color:#64748b;font-size:12px;margin-top:8px">this tab closes itself…</div></div>
<script>setTimeout(function(){window.close();},900);</script></body>`;
}

server.listen(BRIDGE_PORT, '127.0.0.1', () => {
  console.log(`[tv-bridge] listening on http://127.0.0.1:${BRIDGE_PORT}`);
  console.log(`[tv-bridge] will drive Chrome CDP at ${CDP_HOST}:${CDP_PORT} (open a tradingview.com/chart tab there)`);
  console.log('[tv-bridge] endpoints: /health  /plot?symbol=NVDA&exchange=NASDAQ&matp=180.5&mbp=165.0');
});

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') {
    console.error(`[tv-bridge] port ${BRIDGE_PORT} already in use — is the bridge already running?`);
    process.exit(1);
  }
  throw e;
});
