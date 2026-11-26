/* TradeHunter - the Option Finder's price chart (v4.276 / v4.277).
 *
 * Daily candles of a ticker with the Entry / Target / Worst prices as lines the member DRAGS; used by
 * the finder tab (under its price bar) and by the chart's own window (/options/finder-chart), so there
 * is one copy of the chart code. The chart knows nothing about the finder: it is given bars and prices
 * (setBars / setPrices) and reports a drag through onDrag(kind, price); the pages decide what that means.
 *
 *   var c = new THFinderChart(el, {onDrag: function (k, price) {...}, onUnlock: function () {...}});
 *   THFinderChart.load(libUrl).then(function () { c.setBars(bars); c.setPrices({entry, target, worst, lock, levels}); });
 *
 * lock = true means the entry follows the last price (drawn dashed); a drag on it calls onUnlock first.
 * The price scale always includes the three lines, so a far target never falls off the chart.
 */
(function () {
  var COLORS = {entry: '#38bdf8', target: '#34d399', worst: '#fb7185'};
  var WORDS = {entry: 'Entry', target: 'Target', worst: 'Worst'};
  var LVL_COLORS = {matp: '#f97316', mbp: '#16a34a'};
  var libPromise = null;

  function FinderChart(el, opts) {
    this.el = el;
    this.opts = opts || {};
    this.prices = {};
    this.lock = true;
    this.levels = {};
    this.lines = {};
    this.lvl = {};
    this.drag = null;
    this.bars = 0;
    this._build();
    el.thFinderChart = this;                     // the instance from its element (a page's checks, the console)
  }

  FinderChart.load = function (url) {          // the chart library, once per page
    if (window.LightweightCharts) return Promise.resolve();
    if (!libPromise) libPromise = new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = url; s.async = true;
      s.onload = function () { res(); };
      s.onerror = function () { libPromise = null; rej(new Error('the chart library did not load')); };
      document.head.appendChild(s);
    });
    return libPromise;
  };

  FinderChart.prototype._build = function () {
    var self = this, el = this.el, lightMode = document.documentElement.classList.contains('light');
    this.chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: {background: {color: lightMode ? '#ffffff' : '#0b1220'}, textColor: lightMode ? '#334155' : '#94a3b8', fontSize: 11},
      grid: {vertLines: {color: 'rgba(148,163,184,.08)'}, horzLines: {color: 'rgba(148,163,184,.08)'}},
      rightPriceScale: {borderVisible: false}, timeScale: {borderVisible: false, rightOffset: 6}, crosshair: {mode: 0},
    });
    this.series = this.chart.addCandlestickSeries({
      upColor: '#34d399', downColor: '#fb7185', borderVisible: false, wickUpColor: '#34d399', wickDownColor: '#fb7185',
      autoscaleInfoProvider: function (orig) {                 // the scale always shows the three lines
        var r = orig(), vals = self._vals();
        if (!r || !r.priceRange || !vals.length) return r;
        return {priceRange: {minValue: Math.min.apply(null, vals.concat([r.priceRange.minValue])),
                             maxValue: Math.max.apply(null, vals.concat([r.priceRange.maxValue]))}, margins: r.margins};
      },
    });
    ['worst', 'entry', 'target'].forEach(function (k) {
      self.lines[k] = self.series.createPriceLine({price: 0, color: COLORS[k], lineWidth: 2, lineStyle: 0, axisLabelVisible: false, lineVisible: false, title: WORDS[k]});
    });
    // the drag is caught before the chart's own handlers (capture), so a line is dragged and the chart is not panned
    el.addEventListener('pointerdown', function (ev) { self._down(ev); }, true);
    el.addEventListener('pointermove', function (ev) { self._move(ev); }, true);
    el.addEventListener('pointerup', function (ev) { self._up(ev); }, true);
    el.addEventListener('pointercancel', function (ev) { self._up(ev); }, true);
  };

  FinderChart.prototype._vals = function () {
    var p = this.prices;
    return ['entry', 'target', 'worst'].map(function (k) { return p[k]; }).filter(function (v) { return v > 0; });
  };

  FinderChart.prototype.setBars = function (bars) {           // [{time, open, high, low, close}]
    var rows = (bars || []).filter(function (b) { return b && b.time && b.close != null; })
      .map(function (b) { return {time: b.time, open: +b.open, high: +b.high, low: +b.low, close: +b.close}; });
    this.series.setData(rows);
    this.bars = rows.length;
    if (rows.length) this.chart.timeScale().setVisibleLogicalRange({from: Math.max(0, rows.length - 130), to: rows.length + 6});
    this._sync();
    return rows.length;
  };

  FinderChart.prototype.setPrices = function (p) {            // {entry, target, worst, lock, levels: {matp, mbp}}
    p = p || {};
    var before = JSON.stringify(this._vals());
    this.prices = {entry: +p.entry || 0, target: +p.target || 0, worst: +p.worst || 0};
    this.lock = !!p.lock;
    this.levels = p.levels || {};
    if (JSON.stringify(this._vals()) !== before) this._rescale();   // the scale is only recomputed on a data change
    this._sync();
  };

  FinderChart.prototype._rescale = function () {              // make the price scale include the lines again
    var ps = this.chart.priceScale('right');
    try { ps.applyOptions({autoScale: false}); ps.applyOptions({autoScale: true}); } catch (err) { /* ignore */ }
  };

  FinderChart.prototype._sync = function () {
    var self = this, ys = {};
    Object.keys(this.lines).forEach(function (k) {
      var v = self.prices[k], on = v > 0;
      self.lines[k].applyOptions({price: on ? v : 0, lineVisible: on, axisLabelVisible: on, lineStyle: (k === 'entry' && self.lock) ? 2 : 0});
      if (on) { var y = self.series.priceToCoordinate(v); if (y != null) ys[k] = Math.round(y); }
    });
    ['matp', 'mbp'].forEach(function (k) {
      var v = self.levels[k], on = v != null && isFinite(v) && v > 0;
      if (!self.lvl[k]) self.lvl[k] = self.series.createPriceLine({price: 0, color: LVL_COLORS[k], lineWidth: 1, lineStyle: 2, axisLabelVisible: false, lineVisible: false, title: k.toUpperCase()});
      self.lvl[k].applyOptions({price: on ? +v : 0, lineVisible: on, axisLabelVisible: on});
    });
    this.el.dataset.lines = JSON.stringify(ys);                // where the lines sit, for a check from outside
  };

  FinderChart.prototype._y = function (ev) { return ev.clientY - this.el.getBoundingClientRect().top; };

  FinderChart.prototype._hit = function (y) {                 // the line within 8 px of y, the nearest when two overlap
    var self = this, best = null;
    Object.keys(this.prices).forEach(function (k) {
      var v = self.prices[k];
      if (!(v > 0)) return;
      var yy = self.series.priceToCoordinate(v);
      if (yy == null) return;
      var d = Math.abs(yy - y);
      if (d <= 8 && (!best || d < best.d)) best = {k: k, d: d};
    });
    return best ? best.k : null;
  };

  FinderChart.prototype._down = function (ev) {
    if (ev.button !== 0 || !this._vals().length) return;
    var k = this._hit(this._y(ev));
    if (!k) return;
    ev.preventDefault(); ev.stopPropagation();
    if (k === 'entry' && this.lock && this.opts.onUnlock) this.opts.onUnlock();   // v4.277: the Entry is always draggable
    this.drag = k;
    this.chart.applyOptions({handleScroll: false, handleScale: false});
    try { this.el.setPointerCapture(ev.pointerId); } catch (err) { /* ignore */ }
  };

  FinderChart.prototype._move = function (ev) {
    if (this.drag) {
      ev.preventDefault(); ev.stopPropagation();
      var p = this.series.coordinateToPrice(this._y(ev));
      if (p != null && p > 0 && this.opts.onDrag) this.opts.onDrag(this.drag, p);
      return;
    }
    this.el.style.cursor = (this._vals().length && this._hit(this._y(ev))) ? 'ns-resize' : '';
  };

  FinderChart.prototype._up = function (ev) {
    if (!this.drag) return;
    ev.stopPropagation();
    this.drag = null;
    this.chart.applyOptions({handleScroll: true, handleScale: true});
  };

  window.THFinderChart = FinderChart;
})();
