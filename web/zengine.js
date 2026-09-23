/* Universe z-scores for every tracked metric, computed in the browser from stocks-data.json.
 *
 * Method, and why:
 *   robust z = (x - median) / (1.4826 * MAD), clipped to +/-5.
 *   Financial ratios are fat-tailed. A classic mean/sd z lets one P/E of 4,000 set
 *   the scale for everyone, so almost the whole universe lands inside +/-0.1.
 *   Median and MAD ignore the tail, and the 1.4826 factor makes the scale match a
 *   standard deviation when the data happen to be normal, so +1 still reads "one sd".
 *   Where more than half the values are identical (MAD = 0, e.g. insider ownership)
 *   the engine falls back to mean/sd and says so in stats[key].method.
 *   market_cap and volume are log10 first: size is multiplicative.
 *
 * Cohort:
 *   "universe": every listing whose security_type is not excluded. The default exclusion is
 *               the pipeline's security_type.NON_OPERATING, handed in by the page (apt.nonop).
 *   "sector":   the same, restricted to the row's own sector, and only when that cohort has
 *               at least apt.minCohort members (MIN_COHORT_FOR_ZSCORE). Otherwise null.
 *
 * Generated pages load this file as it is; tests/ and the render check load it in node.
 *
 * API (window.APTZ):
 *   APTZ.init(apt)                       -> apt = {stocks: {ticker, sector, kind}, vals: {key: []},
 *                                           metrics: [{key, unit, transform}], nonop, minCohort}
 *   APTZ.compute({scope, exclude})       -> {z: {key: Float64Array}, stats: {key: {...}}, cohortMask}
 *   APTZ.filter(result, bands, opts)     -> Int32Array of row indices passing every band
 *   APTZ.hist(result, key, bins, lo, hi, idx) -> {edges, counts}
 *   APTZ.profileDistance(result, row, keys) -> Float64Array distance of every row to `row`
 *   APTZ.fmt(key, value)                 -> display string in the metric's own unit
 *   APTZ.fmtZ(z)                         -> "+1.24" style string
 */
(function () {
  "use strict";
  var A = null, N = 0, METRICS = [], BY_KEY = {};
  var NON_OPERATING = [], MIN_COHORT = 20;

  function median(sorted) {
    var n = sorted.length;
    if (!n) return NaN;
    var h = n >> 1;
    return n % 2 ? sorted[h] : (sorted[h - 1] + sorted[h]) / 2;
  }

  function transform(m, v) {
    if (v === null || v === undefined || !isFinite(v)) return NaN;
    if (m.transform === "log10") return v > 0 ? Math.log10(v) : NaN;
    return v;
  }

  function init(apt) {
    A = apt;
    N = apt.stocks.ticker.length;
    METRICS = apt.metrics;
    if (apt.nonop) NON_OPERATING = apt.nonop.slice();
    if (apt.minCohort) MIN_COHORT = apt.minCohort;
    API.NON_OPERATING = NON_OPERATING;
    BY_KEY = {};
    METRICS.forEach(function (m) { BY_KEY[m.key] = m; });
    return { rows: N, metrics: METRICS.length };
  }

  function stat(values) {
    // values: plain array of finite numbers
    var s = values.slice().sort(function (a, b) { return a - b; });
    var med = median(s);
    var dev = s.map(function (x) { return Math.abs(x - med); }).sort(function (a, b) { return a - b; });
    var mad = median(dev);
    var sum = 0, sq = 0;
    for (var i = 0; i < s.length; i++) { sum += s[i]; }
    var mean = s.length ? sum / s.length : NaN;
    for (i = 0; i < s.length; i++) { sq += (s[i] - mean) * (s[i] - mean); }
    var sd = s.length > 1 ? Math.sqrt(sq / (s.length - 1)) : NaN;
    var method = "robust", center = med, scale = 1.4826 * mad;
    if (!(scale > 0)) { method = "sd"; center = mean; scale = sd; }
    if (!(scale > 0)) { method = "none"; }
    function q(p) { return s.length ? s[Math.min(s.length - 1, Math.max(0, Math.round(p * (s.length - 1))))] : NaN; }
    return { n: s.length, median: med, mad: mad, mean: mean, sd: sd, method: method, center: center,
             scale: scale, p05: q(0.05), p25: q(0.25), p75: q(0.75), p95: q(0.95), min: s[0], max: s[s.length - 1] };
  }

  function compute(opts) {
    opts = opts || {};
    var scope = opts.scope || "universe";
    var exclude = opts.exclude || NON_OPERATING;
    var ex = {};
    exclude.forEach(function (k) { ex[k] = 1; });
    var kind = A.stocks.kind, sector = A.stocks.sector;
    var mask = new Uint8Array(N);
    for (var i = 0; i < N; i++) mask[i] = ex[kind[i]] ? 0 : 1;

    var out = { scope: scope, exclude: exclude.slice(), z: {}, raw: {}, stats: {}, cohortMask: mask, sectorStats: {} };
    METRICS.forEach(function (m) {
      var vals = A.vals[m.key];
      var tv = new Float64Array(N), z = new Float64Array(N);
      for (var i = 0; i < N; i++) { tv[i] = transform(m, vals[i]); z[i] = NaN; }
      out.raw[m.key] = tv;
      var groups = {};
      if (scope === "sector") {
        for (i = 0; i < N; i++) {
          if (!mask[i] || !sector[i] || isNaN(tv[i])) continue;
          (groups[sector[i]] = groups[sector[i]] || []).push(tv[i]);
        }
      } else {
        var all = [];
        for (i = 0; i < N; i++) if (mask[i] && !isNaN(tv[i])) all.push(tv[i]);
        groups["*"] = all;
      }
      var st = {};
      Object.keys(groups).forEach(function (g) {
        if (scope === "sector" && groups[g].length < MIN_COHORT) return;
        st[g] = stat(groups[g]);
      });
      for (i = 0; i < N; i++) {
        if (isNaN(tv[i])) continue;
        // Excluded listings still get a z against the operating cohort, so a user who opts
        // them back in sees where they sit, but they never move the cohort's median or MAD.
        var g = scope === "sector" ? sector[i] : "*";
        var s = st[g];
        if (!s || s.method === "none") continue;
        var zz = (tv[i] - s.center) / s.scale;
        z[i] = Math.max(-5, Math.min(5, zz));
      }
      out.z[m.key] = z;
      out.stats[m.key] = scope === "sector" ? null : st["*"];
      if (scope === "sector") out.sectorStats[m.key] = st;
    });
    if (scope === "sector") {
      // Universe stats are still useful as the reference line in histograms.
      var u = compute({ scope: "universe", exclude: exclude });
      out.stats = u.stats;
    }
    return out;
  }

  /* bands: {key: [lo, hi]} in z units; lo/hi may be -Infinity / Infinity.
     opts.idx: restrict to these rows. opts.missing: "fail" (default) or "pass". */
  function filter(res, bands, opts) {
    opts = opts || {};
    var keys = Object.keys(bands || {});
    var src = opts.idx || null;
    var out = [];
    var n = src ? src.length : N;
    for (var j = 0; j < n; j++) {
      var i = src ? src[j] : j;
      var ok = true;
      for (var k = 0; k < keys.length; k++) {
        var b = bands[keys[k]], z = res.z[keys[k]][i];
        if (isNaN(z)) { if (opts.missing === "pass") continue; ok = false; break; }
        if (z < b[0] || z > b[1]) { ok = false; break; }
      }
      if (ok) out.push(i);
    }
    return Int32Array.from(out);
  }

  function hist(res, key, bins, lo, hi, idx) {
    bins = bins || 40; lo = lo === undefined ? -4 : lo; hi = hi === undefined ? 4 : hi;
    var counts = new Array(bins).fill(0), edges = [];
    for (var b = 0; b <= bins; b++) edges.push(lo + (hi - lo) * b / bins);
    var z = res.z[key];
    var n = idx ? idx.length : N;
    for (var j = 0; j < n; j++) {
      var i = idx ? idx[j] : j;
      if (idx === undefined && !res.cohortMask[i]) continue;
      var v = z[i];
      if (isNaN(v)) continue;
      var k = Math.floor((v - lo) / (hi - lo) * bins);
      if (k < 0) k = 0; if (k >= bins) k = bins - 1;
      counts[k]++;
    }
    return { edges: edges, counts: counts };
  }

  function profileDistance(res, row, keys) {
    keys = keys || METRICS.map(function (m) { return m.key; });
    var d = new Float64Array(N);
    for (var i = 0; i < N; i++) {
      var s = 0, n = 0;
      for (var k = 0; k < keys.length; k++) {
        var a = res.z[keys[k]][row], b = res.z[keys[k]][i];
        if (isNaN(a) || isNaN(b)) continue;
        s += (a - b) * (a - b); n++;
      }
      d[i] = n >= Math.max(4, keys.length * 0.5) ? Math.sqrt(s / n) : NaN;
    }
    return d;
  }

  function fmtNum(v, dp) {
    return v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  }

  function fmtUsd(v) {
    var a = Math.abs(v);
    if (a >= 1e12) return "$" + fmtNum(v / 1e12, 2) + "T";
    if (a >= 1e9) return "$" + fmtNum(v / 1e9, 2) + "B";
    if (a >= 1e6) return "$" + fmtNum(v / 1e6, 1) + "M";
    return "$" + fmtNum(v, 0);
  }

  function fmt(key, v) {
    if (v === null || v === undefined || !isFinite(v)) return "n/a";
    var m = BY_KEY[key] || { unit: "ratio" };
    switch (m.unit) {
      case "pct": return (v >= 0 ? "" : "−") + fmtNum(Math.abs(v) * 100, 1) + "%";
      case "x": return fmtNum(v, 1) + "x";
      case "usd": return fmtUsd(v);
      case "shares": return v >= 1e6 ? fmtNum(v / 1e6, 1) + "M" : v >= 1e3 ? fmtNum(v / 1e3, 0) + "K" : fmtNum(v, 0);
      case "count": return fmtNum(v, 0);
      default: return (v < 0 ? "−" : "") + fmtNum(Math.abs(v), 2);
    }
  }

  function fmtZ(z) {
    if (z === null || z === undefined || isNaN(z)) return "n/a";
    return (z >= 0 ? "+" : "−") + Math.abs(z).toFixed(2);
  }

  var API = { init: init, compute: compute, filter: filter, hist: hist, profileDistance: profileDistance,
              fmt: fmt, fmtZ: fmtZ, NON_OPERATING: NON_OPERATING, metric: function (k) { return BY_KEY[k]; } };
  if (typeof window !== "undefined") window.APTZ = API;
  if (typeof module !== "undefined" && module.exports) module.exports = API;
})();
