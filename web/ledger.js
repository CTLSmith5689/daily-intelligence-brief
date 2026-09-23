/* The Ledger site: every page of the published site, drawn in the browser from data the pipeline writes.
 *
 * One palette and one set of faces everywhere (web/ledger.css). Each page is a static shell from
 * lambda_function.py (masthead, footer, and a JSON blob in window.APT_PAGE with what that page needs);
 * this file draws the body into #ld-main. body[data-page] picks the page:
 *   home      the brief's top stories, the screen of the day (computed by the pipeline), the research calls
 *   today     the latest brief by section, repeats folded together
 *   stories   the story library, searchable
 *   stocks    every listing and every tracked metric in one grid, tinted by universe z, filtered by a
 *             small query language in the command bar (stocks.html#q=gm>1 pe<-0.5)
 *   company   company.html#TICKER: price, sector-relative scores, the full thesis when one exists, where
 *             each metric sits in the universe, closest profiles, business, history, filings, headlines
 *   research  an index of theses, each row leading to its company page, and the record
 * stocks and company load stocks-data.json and compute robust z with web/zengine.js (window.APTZ).
 * Which fields are placeholders for a listing's type is read from the row itself: the pipeline withholds
 * them and stamps status "not_applicable" (apply_security_types), so nothing here is hardcoded per type.
 * Ported from the approved prototype, direction D.
 */
(function () {
  "use strict";

  var MINUS = "−", SIGMA = "σ", MID = "·", TIMES = "×", GE = "≥", LE = "≤", ARROW = "→";
  var CFG = window.APT_PAGE || {};
  var ASOF = CFG.asof || "";
  /* The panel is dated ASOF, but its prices are the latest close each price file holds, which can be an
     earlier session. CFG.close is the majority price_date across rows with a price; a company page uses
     its own row's price_date. */
  var PRICE_DATE = CFG.close || ASOF;
  function closeLabel(d) {
    d = d || PRICE_DATE;
    if (!d) return "Last close";
    return "Last close, " + dateShort3(d) + (ASOF && d !== ASOF ? " (panel as of " + dateShort(ASOF) + ")" : "");
  }

  var ctx = null, A = null, S = null, N = 0, rootEl = null;
  var cur = { page: null, arg: null };
  var redrawers = [];
  var resizeTimer = 0, storyTimer = 0;

  /* ---------------------------------------------------------------- site context
   * What the prototype's shell provided: links between pages, memoized z, the row index, and the
   * security_type vocabulary. Links are real URLs, so every page works from a bookmark. */

  var PAGE_FILE = { home: "index.html", today: "today.html", stories: "stories.html", stocks: "stocks.html",
                    research: "research.html", company: "company.html" };
  function makeCtx() {
    var zCache = {}, rowMap = null;
    return {
      APTZ: window.APTZ,
      href: function (page, arg) {
        var f = PAGE_FILE[page] || "index.html";
        if (page === "company" && arg) return f + "#" + encodeURIComponent(arg);
        if (page === "stocks" && arg) return f + "#q=" + encodeURIComponent(arg);
        return f;
      },
      go: function (page, arg) { location.href = this.href(page, arg); },
      z: function (scope, exclude) {
        exclude = exclude || ctx.NON_OPERATING;
        var key = (scope || "universe") + "|" + exclude.slice().sort().join(",");
        if (!zCache[key]) zCache[key] = window.APTZ.compute({ scope: scope || "universe", exclude: exclude });
        return zCache[key];
      },
      row: function (tk) {
        if (!rowMap) { rowMap = {}; if (S) for (var i = 0; i < N; i++) rowMap[S.ticker[i]] = i; }
        return Object.prototype.hasOwnProperty.call(rowMap, tk) ? rowMap[tk] : -1;
      },
      KIND_LABEL: { operating: "Company", lp: "Partnership (LP)", bdc: "BDC", royalty_trust: "Royalty trust",
                    spac: "SPAC shell", debt: "Exchange-listed note", structured: "Trust certificate",
                    equity_units: "Corporate units", cef: "Closed-end fund" },
      KIND_SHORT: { operating: "", lp: "LP", bdc: "BDC", royalty_trust: "TRUST", spac: "SPAC", debt: "NOTE",
                    structured: "CERT", equity_units: "UNITS", cef: "CEF" },
      NON_OPERATING: (CFG.nonop || []).slice(),
    };
  }
  /* A field the pipeline withheld because it does not apply to this listing's type. */
  function na(i, key) { var st = S.status[i]; return !!(st && st[key] === "not_applicable"); }

  /* stocks-data.json, one object per listing, into the columns zengine reads. */
  var NAME_SUFFIX = /\s*(-\s*)?(Class [A-C] )?(Common Stock|Common Shares|Ordinary Shares?|American Depositary Shares?|Common units representing[^|]*|Common Units[^|]*|Limited Partnership Units|Closed End Fund|New Common Stock|Class [A-C] Ordinary Shares?|Class [A-C] Common Stock|Class [A-C] Common Shares|Registered Shares|Units)\s*\.?\s*$/i;
  function cleanName(name, kind) {
    var n = String(name || "").trim();
    if (kind === "debt" || kind === "structured" || kind === "equity_units") return n;
    for (var k = 0; k < 2; k++) {
      var n2 = n.replace(NAME_SUFFIX, "").trim().replace(/[,-]+$/, "").trim();
      if (n2 === n) break;
      n = n2;
    }
    return n || String(name || "");
  }
  function num0(v) { return typeof v === "number" && isFinite(v) ? v : null; }
  function buildData(rows) {
    var cols = { ticker: [], name: [], full_name: [], sector: [], sub: [], index: [], kind: [], price: [], chg: [],
                 price_date: [], earn: [], score: [], g: [], v: [], q: [], mom: [], status: [], mcap_raw: [] };
    var vals = {};
    CFG.metrics.forEach(function (m) { vals[m.key] = []; });
    var kinds = {};
    rows.forEach(function (r) {
      var kind = r.security_type || "operating";
      kinds[kind] = (kinds[kind] || 0) + 1;
      cols.ticker.push(r.ticker);
      cols.full_name.push(r.name || "");
      cols.name.push(cleanName(r.name, kind));
      cols.sector.push(r.sector || "");
      cols.sub.push(r.sub_industry || "");
      cols.index.push(r.index || "");
      cols.kind.push(kind);
      cols.price.push(num0(r.price));
      cols.chg.push(num0(r.change_pct));
      cols.price_date.push(r.price_date || "");
      cols.earn.push(r.earnings_date || "");
      var dims = [r.g, r.v, r.q, r.m].filter(function (d) { return typeof d === "number" && isFinite(d); });
      // The composite: the mean of the dimension z-scores, when the pipeline calls the row scorable.
      cols.score.push(r.scorable && dims.length ? dims.reduce(function (a, b) { return a + b; }, 0) / dims.length : null);
      cols.g.push(num0(r.g)); cols.v.push(num0(r.v)); cols.q.push(num0(r.q)); cols.mom.push(num0(r.m));
      cols.status.push(r.status || 0);
      cols.mcap_raw.push(num0(r.market_cap));
      CFG.metrics.forEach(function (m) { vals[m.key].push(num0(r[m.key])); });
    });
    var groups = [];
    CFG.metrics.forEach(function (m) { if (groups.indexOf(m.group) < 0) groups.push(m.group); });
    return { asof: ASOF, metrics: CFG.metrics, groups: groups, stocks: cols, vals: vals, kinds: kinds,
             nonop: CFG.nonop, minCohort: CFG.minCohort };
  }
  function loadUniverse() {
    return fetch(CFG.data || "stocks-data.json").then(function (r) {
      if (!r.ok) throw new Error("stocks-data.json answered " + r.status);
      return r.json();
    }).then(function (rows) {
      A = buildData(rows);
      S = A.stocks; N = S.ticker.length;
      window.APTZ.init(A);
      // The majority close date of the rows actually loaded, which is what the grid shows.
      var cnt = {}, best = "", bn = 0;
      for (var i = 0; i < N; i++) if (S.price[i] != null && S.price_date[i]) cnt[S.price_date[i]] = (cnt[S.price_date[i]] || 0) + 1;
      Object.keys(cnt).forEach(function (d) { if (cnt[d] > bn || (cnt[d] === bn && d > best)) { best = d; bn = cnt[d]; } });
      if (best) PRICE_DATE = best;
      return A;
    });
  }
  function getJSON(url) {
    return fetch(url).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  /* The on-disk name of a ticker's file under company/, thesis/, prices/ and news/ (_news_filename). */
  var WIN_RESERVED = ["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                      "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"];
  function tickerFile(tk) { var b = String(tk).toUpperCase(); return (WIN_RESERVED.indexOf(b) >= 0 ? "_" + b : b) + ".json"; }

  /* ---------------------------------------------------------------- helpers */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function dashFree(s) { return String(s == null ? "" : s).replace(/\s*\u2014\s*/g, ", ").replace(/\u2013/g, " to "); }
  function cap1(t) { t = String(t || ""); return t.charAt(0).toUpperCase() + t.slice(1); }
  function num(v, dp) { return v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp }); }
  function int(v) { return Math.round(v).toLocaleString("en-US"); }
  function signed(v, dp) { return (v >= 0 ? "+" : MINUS) + num(Math.abs(v), dp); }
  function money(v) { return v == null || !isFinite(v) ? "n/a" : "$" + num(v, 2); }
  function chgTxt(c) { return c == null || !isFinite(c) ? "n/a" : Math.abs(c) < 0.005 ? "0.00%" : signed(c, 2) + "%"; }
  function chgCls(c) { return c == null || !isFinite(c) || Math.abs(c) < 0.005 ? "" : c > 0 ? "ld-up" : "ld-dn"; }
  /* z as text. The engine clips at +/-5, so a clipped value reads "at least +5" rather than a false +5.00. */
  function zTxt(z) {
    if (z == null || isNaN(z)) return "n/a";
    if (z >= 5) return GE + " +5" + SIGMA;
    if (z <= -5) return LE + " " + MINUS + "5" + SIGMA;
    if (Math.abs(z) < 0.005) return "0.00" + SIGMA;
    return signed(z, 2) + SIGMA;
  }
  function zShort(z) {
    if (z >= 5) return GE + "+5" + SIGMA;
    if (z <= -5) return LE + MINUS + "5" + SIGMA;
    if (Math.abs(z) < 0.05) return "0" + SIGMA;
    return (z >= 0 ? "+" : MINUS) + num(Math.abs(z), 1) + SIGMA;
  }
  function zClipNote(z) { return Math.abs(z) >= 5 ? ' title="Clipped: the engine caps z at ' + String.fromCharCode(177) + '5' + SIGMA + '"' : ""; }
  /* Headlines sometimes carry their own source at the end ("... - The Courier-Journal", "... from Kyiv Post").
     Strip that tail when it names the source, so the source shows once, on its own line. */
  function normSrc(t) { return String(t || "").toLowerCase().replace(/^the\s+/, "").replace(/[^a-z0-9]+/g, " ").trim(); }
  function cleanHead(h, src) {
    h = String(h || "");
    var ns = normSrc(src);
    if (!ns) return h;
    for (var pass = 0; pass < 3; pass++) {
      var h2 = stripTail(h, ns);
      if (h2 === h) break;
      h = h2;
    }
    return h;
  }
  function stripTail(h, ns) {
    var m = h.match(/^(.{5,})(?:\s+[-|\u00b7]\s+|\s+from\s+)((?:(?!\s[-|\u00b7]\s).){2,70})$/i);
    if (!m) return h;
    var tail = normSrc(m[2]);
    if (tail && (tail === ns || (" " + tail + " ").indexOf(" " + ns + " ") >= 0 || (" " + ns + " ").indexOf(" " + tail + " ") >= 0)) return m[1].trim();
    return h;
  }
  function srcTxt(src) { return src && String(src).trim() ? String(src).trim().replace(/\s*[\u2013\u2014]\s*/g, ", ").replace(/\s+,\s+/g, ", ") : "Source not named"; }
  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  function dt(s) { return new Date(String(s).slice(0, 10) + "T12:00:00Z"); }
  function dateLong(s) { var d = dt(s); return DAYS[d.getUTCDay()] + " " + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear(); }
  function dateMid(s) { var d = dt(s); return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear(); }
  function dateShort(s) { var d = dt(s); return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()].slice(0, 3); }
  function dateShort3(s) { var d = dt(s); return DAYS[d.getUTCDay()].slice(0, 3) + " " + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()].slice(0, 3) + " " + d.getUTCFullYear(); }
  function daysBetween(a, b) { return Math.round((dt(b) - dt(a)) / 86400000); }
  function kindTag(kind) {
    var t = ctx.KIND_SHORT[kind];
    return t ? '<span class="ld-kt ld-kt-' + kind + (isNonOp(kind) ? " ld-kt-nonop" : "") + '" title="' + esc(ctx.KIND_LABEL[kind]) + '">' + t + "</span>" : "";
  }
  function sectorName(s) { return s || "Unclassified"; }
  var KIND_WHY = { debt: "debt listing", structured: "trust certificate", equity_units: "unit listing", spac: "SPAC shell",
                   cef: "closed-end fund", bdc: "BDC", lp: "partnership", royalty_trust: "royalty trust" };
  /* Short forms of FIELD_STATUS, for a cell; the long text goes in the title. */
  var STATUS_WHY = { awaiting_filing: "awaiting next filing", no_coverage: "no coverage at the source",
                     insufficient_history: "not enough history", cohort_too_small: "too few sector peers",
                     deferred_budget: "deferred to the next run", not_meaningful: "not meaningful",
                     source_error: "source error", not_applicable: "does not apply" };

  var GROUPS = [];
  function metricsByGroup() {
    var out = {};
    if (!GROUPS.length) A.metrics.forEach(function (m) { if (GROUPS.indexOf(m.group) < 0) GROUPS.push(m.group); });
    GROUPS.forEach(function (g) { out[g] = []; });
    A.metrics.forEach(function (m) { out[m.group].push(m); });
    return out;
  }
  function metric(k) { return ctx.APTZ.metric(k); }
  function betterTxt(m) { return m.better > 0 ? "higher reads better" : m.better < 0 ? "lower reads better" : "neither end is better"; }
  /* The pipeline's own description of a field (FIELD_METHODS), for a title attribute. */
  function methodTxt(key) {
    var fm = (CFG.fieldMethods || {})[key];
    return fm ? (fm.formula ? fm.formula + ". " : "") + (fm.note || "") : "";
  }

  /* Value cell for a row and metric, honest about withheld or missing fields. */
  function valueOf(i, key) {
    var kind = S.kind[i];
    if (na(i, key)) return { txt: "n/a", why: "n/a, " + (KIND_WHY[kind] || kind), blank: true };
    var v = A.vals[key][i];
    if (v == null || !isFinite(v)) {
      var st = S.status[i] && S.status[i][key];
      return { txt: "n/a", why: st ? STATUS_WHY[st] || "not reported" : "not reported", blank: true };
    }
    return { txt: ctx.APTZ.fmt(key, v).replace(/^(\$?)-/, MINUS + "$1"), v: v };
  }
  function capOf(i) {
    if (na(i, "market_cap")) return "n/a";
    var v = A.vals.market_cap[i];
    return v == null ? "n/a" : ctx.APTZ.fmt("market_cap", v);
  }
  function median(arr) {
    if (!arr.length) return NaN;
    var s = arr.slice().sort(function (a, b) { return a - b; }), h = s.length >> 1;
    return s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2;
  }

  function isNonOp(k) { return ctx.NON_OPERATING.indexOf(k) >= 0; }
  /* Plural nouns used in counts. */
  var KIND_PLURAL = { operating: "companies", lp: "partnerships", bdc: "BDCs", royalty_trust: "royalty trusts", spac: "SPAC shells",
                      debt: "exchange-listed notes", structured: "trust certificates", equity_units: "unit listings", cef: "closed-end funds" };

  /* ---------------------------------------------------------------- theme
   * The masthead's head script applies a stored choice before first paint; with none, the system
   * preference decides. The key is the one the site has always used, so a deliberate choice carries over. */

  var THEME_KEY = "apt-theme-v2";
  function effectiveTheme() {
    var t = document.documentElement.dataset.theme;
    if (t === "light" || t === "dark") return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  var MOON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 10.2A6 6 0 0 1 5.8 2.5a6 6 0 1 0 7.7 7.7z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  var SUN = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6L13 13M3 13l1.4-1.4M11.6 4.4L13 3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
  function paintThemeBtn() {
    var b = document.querySelector(".ld-theme");
    if (!b) return;
    var dark = effectiveTheme() === "dark";
    b.innerHTML = dark ? SUN : MOON;
    b.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    b.title = b.getAttribute("aria-label");
  }
  function toggleTheme() {
    var next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* storage unavailable */ }
    paintThemeBtn();
  }
  /* ---------------------------------------------------------------- screens (presets)
   * The ready-made screens are LEDGER_SCREENS in lambda_function.py: the pipeline computes the home page's
   * screen of the day from the same definition the Stocks page runs, so the two counts agree. A band's
   * open end arrives as null. */

  var INF = Infinity;
  function bandOf(b) { return [b[0] == null ? -INF : b[0], b[1] == null ? INF : b[1]]; }
  var SHORT_LABEL = { earnings_consistency: "Consist.", net_debt_ebitda: "ND/ EBITDA", revenue_growth_yoy: "Rev. growth", return_12_2: "12-2 mo.", volatility_1y: "Vol. 1y", operating_margin: "Op. margin", neglect_score: "Neglect", fcf_yield: "FCF yield", revenue_acceleration: "Rev. accel." };

  function bandTxt(b) {
    if (b[0] === -INF && b[1] === INF) return "any";
    if (b[0] === -INF) return LE + " " + zShort(b[1]);
    if (b[1] === INF) return GE + " " + zShort(b[0]);
    return zShort(b[0]) + " to " + zShort(b[1]);
  }
  /* Raw value that sits at z, from universe stats handed in (the home page has no rows loaded). */
  function zToRawWith(st, key, z) {
    if (!st || st.method === "none" || !(st.scale > 0) || !isFinite(z)) return null;
    var x = st.center + z * st.scale;
    if (metric(key).transform === "log10") x = Math.pow(10, x);
    return ctx.APTZ.fmt(key, x);
  }
  function bandRawTxt(key, b, st) {
    var lo = b[0] === -INF ? null : zToRawWith(st, key, b[0]), hi = b[1] === INF ? null : zToRawWith(st, key, b[1]);
    if (lo && hi) return lo + " to " + hi;
    if (lo) return GE + " " + lo;
    if (hi) return LE + " " + hi;
    return "";
  }
  function byScore(a, b) {
    var x = S.score[a], y = S.score[b];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return y - x;
  }

  /* ---------------------------------------------------------------- HOME
   * CFG.brief is the newest brief. CFG.trend carries the recent headlines and per-day counts rather than the
   * whole library; CFG.sod is the screen of the day as the pipeline computed it. */

  function dedupedBrief() {
    var seen = {}, out = [];
    (CFG.brief.sections || []).forEach(function (sec) {
      var list = [];
      (sec.stories || []).forEach(function (s) {
        var h = cleanHead(s.h, s.src), k = h.toLowerCase();
        if (seen[k]) { if (seen[k].also.indexOf(sec.name) < 0 && seen[k].sec !== sec.name) seen[k].also.push(sec.name); return; }
        var item = { h: h, src: srcTxt(s.src), link: s.link, sec: sec.name, also: [] };
        seen[k] = item;
        list.push(item);
      });
      out.push({ name: sec.name, stories: list, filed: (sec.stories || []).length });
    });
    return out;
  }

  var STOP = ("the a an and or but of to in on for with at by from as is are was were be been it its this that these those after before over under into about than more most new says say said will would could can may might not no yes his her their our your you we they he she them who what why how when where which while amid up down out off per vs via us u.s. just year years week day days first last one two three four five six ten top back also still says report reports update updates live news today" +
    " all any some much many other only own same so too very has have had do does did make makes made get gets got set sets take takes new first time high higher low lower amid near next ahead since against between during without through across plus stock stocks market markets shares").split(" ");
  var STOPSET = {};
  STOP.forEach(function (w) { STOPSET[w] = 1; });

  /* Trend window: the 7 calendar days ending on the newest story, against the 7 days before. */
  function isoAdd(d, n) { return new Date(dt(d).getTime() + n * 86400000).toISOString().slice(0, 10); }
  function trendWindow(stories) {
    var last = "";
    stories.forEach(function (s) { if (s.d > last) last = s.d; });
    return { last: last, curStart: isoAdd(last, -6), prevStart: isoAdd(last, -13), prevEnd: isoAdd(last, -7) };
  }
  function trendTerms(stories, win) {
    var cur = {}, prev = {}, bi = {};
    function toks(h) {
      return String(h).toLowerCase().replace(/[‘’']s\b/g, "").split(/[^a-z0-9&.]+/).map(function (w) {
        return w.replace(/^\.+|\.+$/g, "");
      });
    }
    function ok(w) { return w.length >= 3 && !STOPSET[w] && !/^\d+$/.test(w); }
    stories.forEach(function (s) {
      var d = s.d;
      var bucket = d >= win.curStart && d <= win.last ? cur : d >= win.prevStart && d <= win.prevEnd ? prev : null;
      if (!bucket) return;
      var t = toks(cleanHead(s.h, s.src)), words = {}, pairs = {};
      t.forEach(function (w, j) {
        if (!ok(w)) return;
        words[w] = 1;
        if (bucket === cur && j + 1 < t.length && ok(t[j + 1])) pairs[w + " " + t[j + 1]] = 1;
      });
      Object.keys(words).forEach(function (w) { bucket[w] = (bucket[w] || 0) + 1; });
      Object.keys(pairs).forEach(function (w) { bi[w] = (bi[w] || 0) + 1; });
    });
    // Fold a two-word phrase ("white house") into one entry when it accounts for most uses of both words.
    Object.keys(bi).forEach(function (pair) {
      var n = bi[pair], ws = pair.split(" ");
      if (n < 6 || n < 0.6 * (cur[ws[0]] || 0) || n < 0.6 * (cur[ws[1]] || 0)) return;
      cur[pair] = n;
      prev[pair] = 0;
      delete cur[ws[0]]; delete cur[ws[1]];
    });
    var NICE = { "u.s": "U.S.", "u.k": "U.K.", "ai": "AI", "fed": "Fed", "nato": "NATO", "gdp": "GDP" };
    return Object.keys(cur).map(function (w) { return { w: NICE[w] || w, n: cur[w], p: prev[w] || 0 }; })
      .sort(function (a, b) { return b.n - a.n || a.w.localeCompare(b.w); }).slice(0, 10);
  }

  function renderHome(main) {
    var b = CFG.brief || { sections: [] }, secs = dedupedBrief(), T = CFG.trend || { stories: [], days: {} };
    var unique = 0; secs.forEach(function (s) { unique += s.stories.length; });
    var research = CFG.research || [];
    var quotes = (CFG.quotes || []).map(function (q) {
      return '<div class="ld-q"><div class="ld-kicker">' + esc(q.y ? q.label : q.t + " " + MID + " " + q.label) + '</div><div class="p">' + esc(q.price) + "</div>" +
        (q.y ? '<div class="c ld-muted">7-day yield</div>' : '<div class="c ' + chgCls(q.chg) + '">' + chgTxt(q.chg) + "</div>") + "</div>";
    }).join("");

    var withStories = secs.filter(function (s) { return s.stories.length; });
    var lead = withStories.length ? withStories[0].stories[0] : null;
    var tops = [];
    withStories.forEach(function (s, si) {
      s.stories.slice(si === 0 ? 1 : 0, si === 0 ? 2 : 1).forEach(function (x) { tops.push(x); });
    });
    tops = tops.slice(0, 7);

    // Screen of the day, computed by the pipeline over the same stocks-data.json the Stocks page loads.
    var P = CFG.sod, sodHTML = "";
    if (P) {
      var keys = Object.keys(P.bands), bands = {};
      keys.forEach(function (k) { bands[k] = bandOf(P.bands[k]); });
      var screenRows = P.hits.map(function (h) {
        return '<tr data-t="' + esc(h.t) + '"><td class="co"><a class="ld-tk" href="' + ctx.href("company", h.t) + '">' + esc(h.t) + "</a>" + kindTag(h.kind) +
          '<span class="nm">' + esc(cleanName(h.name, h.kind)) + "</span></td>" +
          keys.map(function (k) { var z = h.z[k]; return '<td class="r ld-num"' + zClipNote(z) + ">" + (z == null ? "n/a" : zShort(z).replace(SIGMA, "")) + "</td>"; }).join("") +
          '<td class="r ld-num sc">' + (h.score == null ? "n/a" : signed(h.score, 2)) + "</td></tr>";
      }).join("");
      sodHTML = '<section aria-labelledby="ld-sod-h"><div class="ld-panel"><div class="ld-kicker"><b>Screen of the day</b> ' + MID + " universe z</div>" +
        '<h2 class="ld-h2" id="ld-sod-h" style="margin-top:8px">' + esc(P.name) + "</h2>" +
        '<p style="margin:6px 0 0;color:var(--ink2);font-size:15px">' + esc(P.blurb) + "</p>" +
        '<div class="ld-bands">' + keys.map(function (k) {
          var raw = bandRawTxt(k, bands[k], P.stats[k]);
          return '<span class="ld-band"><b>' + esc(metric(k).label) + "</b> " + bandTxt(bands[k]) + (raw ? ' <span class="ld-muted">(' + raw + ")</span>" : "") + "</span>";
        }).join("") + "</div>" +
        '<p style="margin:0 0 10px;font:400 20px/1.3 var(--serif)"><b style="color:var(--accent-ink);font-weight:500">' + int(P.match) + "</b> of " + int(P.of) + " operating companies match</p>" +
        (P.hits.length ? '<div class="ld-tbl-wrap"><table class="ld-mini ld-sodt" id="ld-sod"><thead><tr><th>Company</th>' +
        keys.map(function (k) { return '<th class="r" title="' + esc(metric(k).label) + ', universe z">' + esc(SHORT_LABEL[k] || metric(k).label) + ' <span class="lc">' + SIGMA + "</span></th>"; }).join("") +
        '<th class="r sc" title="Composite score, a z within the company’s own sector">Score (sector)</th></tr></thead><tbody>' + screenRows + "</tbody></table></div>" : "") +
        '<p class="ld-muted" style="font-size:13px;margin:10px 0 14px">Top ' + (P.hits.length === 8 ? "eight" : P.hits.length) + " by composite score. The metric columns are universe z, measured against all " + int(P.of) + " operating companies (SPAC shells, notes and closed-end funds excluded); the score is sector-relative. Panel as of " + dateMid(ASOF) + ".</p>" +
        '<a class="ld-link" href="' + ctx.href("stocks", P.q) + '">Open this screen ' + ARROW + "</a></div></section>";
    }

    // Research calls
    var calls = research.map(function (r) {
      var v = viewOf(r);
      return '<li><a class="ld-tk" href="' + ctx.href("company", r.ticker) + '#thesis">' + esc(r.ticker) + "</a>" +
        '<span class="ld-muted" style="font-size:14.5px">' + esc(r.name || r.ticker) + "</span>" +
        '<span class="ld-view ' + (v.open ? "open" : "") + '">' + v.label + "</span>" +
        '<span class="meta">' + esc(callLine(r)) + "</span></li>";
    }).join("");

    // Trends
    var win = trendWindow(T.stories), terms = trendTerms(T.stories, win), tmax = terms.length ? terms[0].n : 1;
    var dayCounts = T.days || {};
    var allDays = Object.keys(dayCounts).sort(), days = allDays.slice(-14), dmax = 0;
    days.forEach(function (d) { dmax = Math.max(dmax, dayCounts[d]); });

    main.innerHTML =
      '<div class="ld-wrap">' +
      '<div class="ld-head" style="border-bottom:0;padding-bottom:14px"><div><div class="ld-kicker"><b>' + (b.type === "daily" || !b.type ? "Daily edition" : esc(cap1(b.type)) + " edition") + "</b>" +
      (b.date ? " " + MID + " " + dateLong(b.date) : "") + (b.ts ? " " + MID + " filed " + esc(b.ts) : "") + "</div>" +
      '<h1 class="ld-h1">The brief, the screen, and the calls</h1></div>' +
      '<p class="ld-deck" style="margin:0;max-width:44ch">One brief a day across ' + withStories.length + ' sections, a library of ' + int(T.total || 0) +
      " stories, and a screener over " + int(CFG.nListings || 0) + " US listings where every metric is placed against the whole universe.</p></div>" +
      (quotes ? '<div class="ld-quotes" aria-label="Index quotes carried in the brief of ' + esc(b.date ? dateMid(b.date) : "") + '" style="border-top:1px solid var(--rule)">' + quotes + "</div>" : "") +
      '<div class="ld-front">' +
      '<section aria-labelledby="ld-top-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-top-h">Top of the brief</h2><a class="ld-link" href="' + ctx.href("today") + '">All ' + unique + " stories " + ARROW + "</a></div>" +
      (lead ? '<article class="ld-lead" style="padding-top:14px"><div class="ld-kicker"><b>' + esc(lead.sec) + "</b></div>" +
      '<h2><a href="' + esc(lead.link) + '" target="_blank" rel="noopener">' + esc(lead.h) + "</a></h2><div class=\"ld-src\">" + esc(lead.src) + "</div></article>" : '<p class="ld-empty">No brief has been filed yet.</p>') +
      '<ul class="ld-tops">' + tops.map(function (s) {
        return '<li><span class="ld-kicker">' + esc(s.sec) + '</span><div><div class="h"><a href="' + esc(s.link) + '" target="_blank" rel="noopener">' + esc(s.h) + '</a></div><div class="ld-src" style="margin-top:4px">' + esc(s.src) + "</div></div></li>";
      }).join("") + "</ul></section>" +
      sodHTML +
      "</div>" +
      '<div class="ld-front" style="padding-top:40px">' +
      '<section aria-labelledby="ld-calls-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-calls-h">Research calls</h2><a class="ld-link" href="' + ctx.href("research") + '">All theses ' + ARROW + "</a></div>" +
      (calls ? '<ul class="ld-calls">' + calls + "</ul>" : '<p class="ld-empty">No thesis has been written yet.</p>') + "</section>" +
      '<section aria-labelledby="ld-tr-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-tr-h">Recent trends</h2>' + (win.last ? '<span class="ld-kicker">7 days to ' + dateShort(win.last) + "</span>" : "") + "</div>" +
      (win.last ? '<p style="margin:10px 0 0;font-size:14px;color:var(--ink2)">Words that recur most in headlines from ' + dateShort(win.curStart) + " to " + dateMid(win.last) + ", counted once per story, against the 7 days before (" + dateShort(win.prevStart) + " to " + dateShort(win.prevEnd) + ").</p>" : "") +
      '<ul class="ld-terms">' + terms.map(function (t) {
        return "<li><span>" + esc(t.w) + '</span><span class="bar"><i style="width:' + (100 * t.n / tmax).toFixed(1) + '%"></i></span><span class="n">' + t.n + " (prior " + t.p + ")</span></li>";
      }).join("") + "</ul>" +
      (days.length ? '<div class="ld-kicker" style="margin-top:22px">Stories per brief day</div>' +
      '<div class="ld-cols" role="img" aria-label="Stories filed per day for the last ' + days.length + " brief days, from " + esc(dateMid(days[0])) + " to " + esc(dateMid(days[days.length - 1])) + '">' +
      days.map(function (d, j) {
        return '<div class="' + (j === days.length - 1 ? "last" : "") + '" style="height:' + (100 * dayCounts[d] / dmax).toFixed(1) + '%" title="' + esc(dateMid(d)) + ": " + dayCounts[d] + ' stories"></div>';
      }).join("") + '</div><div class="ld-cols-x">' + days.map(function (d) { return "<span>" + dt(d).getUTCDate() + "</span>"; }).join("") + "</div>" : "") +
      '<div class="ld-stats"><div class="ld-stat"><div class="ld-kicker">Stories</div><div class="v">' + int(T.total || 0) + '</div><div class="d">across ' + allDays.length + " brief days</div></div>" +
      '<div class="ld-stat"><div class="ld-kicker">Sources</div><div class="v">' + int(T.sources || 0) + '</div><div class="d">publications</div></div>' +
      '<div class="ld-stat"><div class="ld-kicker">Cadence</div><div class="v">Daily</div><div class="d">' + cadenceTxt(T.cadence) + "</div></div></div>" +
      "</section></div>" +
      '<section class="ld-sec" aria-labelledby="ld-pages-h" style="padding-top:48px"><div class="ld-sec-h" style="border-bottom:0"><h2 class="ld-h2" id="ld-pages-h">Five places to land</h2></div>' +
      '<div class="ld-pages">' +
      pageCard("today", "Today", (b.date ? "The " + dateMid(b.date) + " brief" : "The latest brief") + " by section, " + unique + " stories with repeats folded together.") +
      pageCard("stories", "Stories", "Search and filter all " + int(T.total || 0) + " stories, grouped by the day they ran.") +
      pageCard("stocks", "Stocks", int(CFG.nListings || 0) + " US listings: the S&P 500, 400 and 600 plus every Nasdaq, NYSE and NYSE American name. Filter on universe z.") +
      pageCard("company", "A company", "One page per ticker: price, where each metric sits in the universe, filings and headlines.", CFG.sampleTicker || "AAPL") +
      pageCard("research", "Research", research.length + " written " + (research.length === 1 ? "thesis" : "theses") + " with a target, a date to check, and what would prove them wrong.") +
      "</div></section></div>";

    var sod = main.querySelector("#ld-sod tbody");
    if (sod) sod.addEventListener("click", rowClick);
  }
  /* Honest cadence line: when the one-a-day edition started, and what ran before it. */
  function cadenceTxt(c) {
    if (!c || !c.dailyN) return "brief days vary";
    var t = "one brief a day since " + dateShort(c.dailyFirst) + " (" + c.dailyN + " briefs)";
    if (c.olderN) t += "; " + dateShort(c.olderFirst) + " to " + dateShort(c.olderLast) + " ran morning, midday and evening editions (" + int(c.olderN) + " stories)";
    return t;
  }
  function pageCard(p, t, d, arg) {
    return '<a href="' + ctx.href(p, arg) + '"><div class="ld-kicker">' + (p === "company" ? "Company" : t) + '</div><div class="ld-h3" style="margin-top:6px">' + t + "</div><p>" + d + "</p></a>";
  }
  function rowClick(e) {
    if (e.target.closest("a")) return;
    var tr = e.target.closest("tr[data-t]");
    if (tr) ctx.go("company", tr.getAttribute("data-t"));
  }
  /* ---------------------------------------------------------------- research helpers
   * r is a thesis record: the research index rows the pipeline embeds, or docs/thesis/TICKER.json. Both carry
   * ticker, direction, written_on, entry_price, target_price, review_by and last_close {date, close}. */

  var VIEW = CFG.viewWords || { long: "Own it", short: "Bet against it", avoid: "Stay away", watch: "Keep watching", "no view": "No view" };
  function viewOf(r) {
    var open = r.direction === "long" || r.direction === "short" || r.direction === "avoid";
    return { label: VIEW[r.direction] || r.direction || "No view", open: open, status: open ? "Open call" : "Watching" };
  }
  function callNums(r) {
    var e = parseFloat(r.entry_price), t = parseFloat(r.target_price), l = r.last_close ? +r.last_close.close : NaN;
    var note = r.notes && r.notes[0] || {};
    var wr = r.if_wrong_price != null ? r.if_wrong_price : note.if_wrong_price;
    var w = wr != null ? parseFloat(wr) : NaN;
    return { e: e, t: t, l: l, w: w, move: (l - e) / e, need: (t - e) / e };
  }
  function callLine(r) {
    var v = viewOf(r), n = callNums(r);
    var since = r.last_close && isFinite(n.l)
      ? "last close " + money(n.l) + " on " + dateShort(r.last_close.date) + (isFinite(n.move) ? ", " + signed(100 * n.move, 1) + "% since written" : "")
      : "no close held since";
    var written = "Written " + (r.written_on ? dateShort(r.written_on) : "on an unrecorded date") + (isFinite(n.e) ? " at " + money(n.e) : "");
    if (!v.open) return written + (isFinite(n.t) ? ", reference target " + money(n.t) + " (not in force while the view is " + v.label.toLowerCase() + ")" : ", no target") + ", " + since;
    var toward = n.need !== 0 ? n.move / n.need : 0;
    return written + (isFinite(n.t) ? ", target " + money(n.t) : ", no target") + (r.review_by ? " by " + dateShort(r.review_by) : "") + ", " + since +
      (!isFinite(n.move) || !isFinite(n.t) ? "" : Math.abs(n.move) < 0.0005 ? ", unmoved" : toward >= 0 ? ", toward target" : ", away from target");
  }

  /* ---------------------------------------------------------------- TODAY */

  function renderToday(main) {
    var b = CFG.brief || { sections: [] }, secs = dedupedBrief().filter(function (s) { return s.stories.length; });
    var unique = 0, filed = 0;
    secs.forEach(function (s) { unique += s.stories.length; filed += s.filed; });
    var others = (CFG.editions || []).filter(function (e) { return e.key !== b.key; });
    var edName = b.type && b.type !== "daily" ? esc(b.type) + " edition" : "daily edition";
    main.innerHTML = '<div class="ld-wrap">' +
      '<div class="ld-head" style="grid-template-columns:minmax(0,1fr)"><div><div class="ld-kicker"><b>Today</b> ' + MID + " " + edName + (b.ts ? " " + MID + " filed " + esc(b.ts) : "") +
      (b.key ? " " + MID + ' <a class="ld-inl" href="' + esc(b.key) + '">the full brief</a>' : "") + "</div>" +
      '<h1 class="ld-h1">' + (b.date ? dateLong(b.date) : "No brief yet") + "</h1>" +
      (secs.length ? '<p class="ld-deck">' + unique + " stories in " + secs.length + " sections." + (filed > unique ? " " + (filed - unique) + " stories were filed under two sections; each appears once, under the first, with a note of where else it ran." : "") + "</p>"
        : '<p class="ld-deck">The brief has not been filed yet. The next scheduled run will populate this page.</p>') +
      (others.length ? '<p class="ld-muted" style="font-size:14px;margin:10px 0 0">Other editions that day: ' + others.map(function (e) {
        return '<a class="ld-inl" href="' + esc(e.key) + '">' + esc(cap1(e.type)) + "</a>";
      }).join(", ") + ".</p>" : "") +
      '<nav class="ld-jump" aria-label="Sections">' + secs.map(function (s, i) {
        return '<button type="button" class="ld-chip" data-jump="ld-bs-' + i + '">' + esc(s.name) + ' <span class="c">' + s.stories.length + "</span></button>";
      }).join("") + "</nav></div></div>" +
      secs.map(function (s, i) {
        return '<section class="ld-brief-sec" id="ld-bs-' + i + '" aria-labelledby="ld-bsh-' + i + '"><div><h2 class="ld-h2" id="ld-bsh-' + i + '">' + esc(s.name) + '</h2><div class="n">' + s.stories.length + " " + (s.stories.length === 1 ? "story" : "stories") + "</div></div><div>" +
          s.stories.map(function (x, j) {
            return '<div class="ld-story' + (j === 0 ? " lead" : "") + '"><a class="h" href="' + esc(x.link) + '" target="_blank" rel="noopener">' + esc(x.h) + "</a>" +
              '<div class="m"><span class="ld-src">' + esc(x.src) + "</span>" + (x.also.length ? ' <span class="also">' + MID + " also filed under " + esc(x.also.join(", ")) + "</span>" : "") + "</div></div>";
          }).join("") + "</div></section>";
      }).join("") + "</div>";
    main.querySelectorAll("[data-jump]").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        var t = document.getElementById(a.getAttribute("data-jump"));
        if (t) { t.scrollIntoView({ block: "start" }); var h = t.querySelector("h2"); if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); } }
      });
    });
  }

  /* ---------------------------------------------------------------- STORIES */

  var sst = { q: "", sec: "", ed: "", shown: 150 };

  function renderStories(main) {
    var stories = CFG.stories || [];
    var secs = {}, eds = {};
    stories.forEach(function (s) { secs[s.sec] = (secs[s.sec] || 0) + 1; eds[s.ed] = (eds[s.ed] || 0) + 1; });
    var secOrder = (CFG.sectionOrder || []).filter(function (n) { return secs[n]; });
    Object.keys(secs).forEach(function (n) { if (secOrder.indexOf(n) < 0) secOrder.push(n); });
    main.innerHTML = '<div class="ld-wrap">' +
      '<div class="ld-head"><div><div class="ld-kicker"><b>Library</b>' + (stories.length ? " " + MID + " " + dateMid(stories[stories.length - 1].d) + " to " + dateMid(stories[0].d) : "") + "</div>" +
      '<h1 class="ld-h1">Story library</h1><p class="ld-deck">Every story the brief has carried, ' + int(stories.length) + " in all, newest first and grouped by the day it ran. Search matches headline, summary and source.</p></div></div>" +
      '<div class="ld-filters">' +
      '<div class="row"><label for="ld-sq" class="ld-sr">Search stories</label><input class="ld-input" id="ld-sq" type="search" placeholder="Search headlines and sources" style="flex:1 1 260px;max-width:520px" value="' + esc(sst.q) + '">' +
      '<label for="ld-sed">Edition</label><select class="ld-select" id="ld-sed"><option value="">All editions</option>' +
      ["daily", "morning", "midday", "evening"].filter(function (e) { return eds[e]; }).map(function (e) {
        return '<option value="' + e + '"' + (sst.ed === e ? " selected" : "") + ">" + e.charAt(0).toUpperCase() + e.slice(1) + " (" + eds[e] + ")</option>";
      }).join("") + "</select></div>" +
      '<div class="row" role="group" aria-label="Section"><label>Section</label>' +
      '<button type="button" class="ld-chip" data-sec="" aria-pressed="' + (!sst.sec) + '">All <span class="c">' + int(stories.length) + "</span></button>" +
      secOrder.map(function (n) {
        return '<button type="button" class="ld-chip" data-sec="' + esc(n) + '" aria-pressed="' + (sst.sec === n) + '">' + esc(n) + ' <span class="c">' + secs[n] + "</span></button>";
      }).join("") + "</div></div>" +
      '<div id="ld-slist" aria-live="polite"></div></div>';
    var q = main.querySelector("#ld-sq");
    q.addEventListener("input", function () {
      clearTimeout(storyTimer);
      storyTimer = setTimeout(function () { sst.q = q.value; sst.shown = 150; drawStoryList(); }, 120);
    });
    main.querySelector("#ld-sed").addEventListener("change", function (e) { sst.ed = e.target.value; sst.shown = 150; drawStoryList(); });
    main.querySelectorAll("[data-sec]").forEach(function (bt) {
      bt.addEventListener("click", function () {
        sst.sec = bt.getAttribute("data-sec"); sst.shown = 150;
        main.querySelectorAll("[data-sec]").forEach(function (x) { x.setAttribute("aria-pressed", String(x === bt)); });
        drawStoryList();
      });
    });
    drawStoryList();
  }

  function drawStoryList() {
    var el = document.getElementById("ld-slist");
    if (!el) return;
    var q = sst.q.trim().toLowerCase();
    // Section chip counts follow the search and edition filters, so they always describe what a click would show.
    var secCount = {}, allCount = 0;
    var pre = (CFG.stories || []).filter(function (s) {
      if (sst.ed && s.ed !== sst.ed) return false;
      if (q && (s.h + " " + (s.src || "") + " " + (s.sum || "")).toLowerCase().indexOf(q) < 0) return false;
      secCount[s.sec] = (secCount[s.sec] || 0) + 1; allCount++;
      return true;
    });
    document.querySelectorAll("#ld-main [data-sec]").forEach(function (bt) {
      var c = bt.querySelector(".c"), n = bt.getAttribute("data-sec") ? secCount[bt.getAttribute("data-sec")] || 0 : allCount;
      if (c) c.textContent = int(n);
    });
    var list = sst.sec ? pre.filter(function (s) { return s.sec === sst.sec; }) : pre;
    var shown = list.slice(0, sst.shown);
    var groups = [], cur = null;
    shown.forEach(function (s) {
      if (!cur || cur.d !== s.d) { cur = { d: s.d, items: [] }; groups.push(cur); }
      cur.items.push(s);
    });
    var dayTotals = {};
    list.forEach(function (s) { dayTotals[s.d] = (dayTotals[s.d] || 0) + 1; });
    el.innerHTML = '<p class="ld-more" style="margin:14px 0 0">' + (list.length ? "Showing " + int(shown.length) + " of " + int(list.length) + " matching " + (list.length === 1 ? "story" : "stories") : "") + "</p>" +
      (list.length ? groups.map(function (g) {
        var tot = dayTotals[g.d], part = g.items.length < tot;
        return '<section class="ld-day"><div class="dh"><div class="d">' + dateLong(g.d) + '</div><div class="ld-kicker" style="margin-top:4px">' +
          (part ? g.items.length + " of " + tot + " shown, more below" : tot + " " + (tot === 1 ? "story" : "stories")) + "</div></div><div>" +
          g.items.map(function (s) {
            var meta = [srcTxt(s.src), s.sec, s.ed === "daily" ? "" : s.ed + " edition"].filter(Boolean).map(esc).join(" " + MID + " ");
            return '<div class="ld-story"><a class="h" href="' + esc(s.link) + '" target="_blank" rel="noopener">' + esc(cleanHead(s.h, s.src)) + '</a><div class="m ld-src">' + meta + "</div></div>";
          }).join("") + "</div></section>";
      }).join("") : '<p class="ld-empty">No story matches. Try fewer words, or clear the section and edition filters.</p>') +
      (list.length > shown.length ? '<div class="ld-more"><button type="button" class="ld-btn" id="ld-smore">Show ' + Math.min(150, list.length - shown.length) + " more</button><span>" + int(list.length - shown.length) + " not yet shown</span></div>" : "");
    var more = document.getElementById("ld-smore");
    if (more) more.addEventListener("click", function () {
      sst.shown += 150; drawStoryList();
      var m2 = document.getElementById("ld-smore"); if (m2) m2.focus();
    });
  }

  /* ---------------------------------------------------------------- STOCKS: the grid screener
   * Direction B's screener in A's clothes: every listing, all tracked metrics in groups, each cell tinted by
   * its z, a histogram per column, and a command bar that takes a small query language. */

  /* Short names for the command bar, from LEDGER_METRICS in lambda_function.py (alias). */
  var ALIAS = {};
  (CFG.metrics || []).forEach(function (m) { ALIAS[m.key] = m.alias || m.key; });
  var EXTRA_ALIAS = { cap: "market_cap", size: "market_cap", volatility: "volatility_1y", mom: "return_12_2",
    margin: "operating_margin", growth: "revenue_growth_yoy", dd: "max_drawdown_1y", sharpe: "sharpe_1y" };
  var SECTOR_SHORT = {
    "Information Technology": "Info Tech", "Health Care": "Health Care", "Financials": "Financials",
    "Industrials": "Industrials", "Utilities": "Utilities", "Materials": "Materials",
    "Consumer Discretionary": "Cons Disc", "Consumer Staples": "Cons Staples", "Real Estate": "Real Estate",
    "Communication Services": "Comm Svcs", "Energy": "Energy", "": "Unclassified"
  };
  var SECTOR_WORDS = { it: "Information Technology", hc: "Health Care", fin: "Financials", ind: "Industrials",
    util: "Utilities", mat: "Materials", disc: "Consumer Discretionary", re: "Real Estate", comm: "Communication Services" };
  var IDX_TOK = { sp500: "S&P 500", sp400: "S&P 400", sp600: "S&P 600", nasdaq: "Nasdaq", nyse: "NYSE", amex: "NYSE American" };
  /* The switches that show or hide listing types. "notes" covers every claim on a parent: notes, trust
     certificates and unit listings (security_type.DEBT_LIKE). */
  var KIND_SWITCH = { spac: ["spac"], spacs: ["spac"], notes: ["debt", "structured", "equity_units"],
    debt: ["debt", "structured", "equity_units"], note: ["debt", "structured", "equity_units"], cef: ["cef"], cefs: ["cef"],
    lp: ["lp"], bdc: ["bdc"], trust: ["royalty_trust"], royalty: ["royalty_trust"] };
  var PRESET_Q = {}, PRESETS = CFG.presets || [];
  PRESETS.forEach(function (p) { PRESET_Q[p.id] = p.q; });

  var gst = { query: "", sort: { k: "cap", dir: -1 }, limit: 150, cursor: 0, cells: "raw" };
  var M = [], BYKEY = {}, A2K = {};
  var gHistCache = {}, sortedCache = {}, PX = null;
  var screen = null, gridNarrow = false, chunkId = 0, debounceId = 0;
  var helpOpen = false, helpReturn = null, pageKeys = null;

  function initAliases() {
    if (M.length) return;
    M = A.metrics;
    M.forEach(function (m) { BYKEY[m.key] = m; A2K[m.key] = m.key; A2K[ALIAS[m.key] || m.key] = m.key; });
    Object.keys(EXTRA_ALIAS).forEach(function (a) { if (!A2K[a] && BYKEY[EXTRA_ALIAS[a]]) A2K[a] = EXTRA_ALIAS[a]; });
    KIND_SWITCH.nonop = KIND_SWITCH.all = ctx.NON_OPERATING.slice();
  }

  /* Fast fixed-decimal formatter with thousands separators (toLocaleString is too slow for 5,000 cells). */
  function fnum(v, dp) {
    var s = Math.abs(v).toFixed(dp), k = s.indexOf("."), ip = k < 0 ? s : s.slice(0, k), fp = k < 0 ? "" : s.slice(k);
    if (ip.length > 3) ip = ip.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (v < 0 && /[1-9]/.test(s) ? MINUS : "") + ip + fp;
  }
  function fsgn(v, dp) {
    var r = +(+v).toFixed(dp);
    return (r > 0 ? "+" : r < 0 ? MINUS : "") + fnum(Math.abs(r), dp);
  }
  function fpct(v, dp) { return v == null || !isFinite(v) ? "n/a" : fsgn(v, dp == null ? 2 : dp) + "%"; }
  function zsig(z) { return z == null || isNaN(z) ? "n/a" : fsgn(z, 2) + SIGMA; }
  function fcap(v) {
    if (v == null || !isFinite(v)) return "n/a";
    var a = Math.abs(v);
    if (a >= 1e12) return fnum(v / 1e12, 2) + "T";
    if (a >= 1e9) return fnum(v / 1e9, a >= 1e11 ? 0 : 1) + "B";
    if (a >= 1e6) return fnum(v / 1e6, 0) + "M";
    return fnum(v, 0);
  }
  function upDn(v) { return v == null || !isFinite(v) || v === 0 ? "" : v > 0 ? "ld-up" : "ld-dn"; }
  /* Diverging tint class: seven steps of half a sigma, neutral at zero, slate below and ochre above. */
  function zClass(z) {
    if (z == null || isNaN(z)) return "";
    var b = Math.min(6, Math.floor(Math.abs(z) / 0.5));
    return b === 0 ? "" : (z < 0 ? "ld-zn" : "ld-zp") + b;
  }
  function nonopKind(kind) { return ctx.NON_OPERATING.indexOf(kind) >= 0; }
  function blankReason(kind) { return "n/a, " + (KIND_WHY[kind] || kind); }
  var cohortCount = 0;
  function cohortN() {
    if (!cohortCount) { var mk = ctx.z("universe").cohortMask; for (var i = 0; i < mk.length; i++) cohortCount += mk[i]; }
    return cohortCount;
  }

  /* z for the grid. The prototype masked placeholder fields here; the pipeline now withholds them at the
     source (a withheld field is absent from the row), so they already have no z and no filter, sort or
     histogram can see them. */
  function mz(scope) { return ctx.z(scope || "universe"); }
  /* Cap, 1D and price per row, NaN where absent. */
  function pxArrays() {
    if (PX) return PX;
    var cp = new Float64Array(N), chg = new Float64Array(N), price = new Float64Array(N);
    for (var i = 0; i < N; i++) {
      cp[i] = S.mcap_raw[i] == null ? NaN : S.mcap_raw[i];
      chg[i] = S.chg[i] == null ? NaN : S.chg[i];
      price[i] = S.price[i] == null ? NaN : S.price[i];
    }
    PX = { cap: cp, chg: chg, price: price };
    return PX;
  }
  /* Compact value for a dense grid cell (full formatting lives on the company page). */
  function cellFmt(m, v) {
    if (v == null || !isFinite(v)) return null;
    var a, s, neg = v < 0 ? MINUS : "";
    switch (m.unit) {
      case "pct":
        a = Math.abs(v * 100);
        s = a >= 10000 ? fnum(a / 1000, 0) + "k" : a >= 1000 ? fnum(a / 1000, 1) + "k" : a >= 100 ? fnum(a, 0) : fnum(a, 1);
        return neg + s + "%";
      case "x":
        a = Math.abs(v);
        s = a >= 10000 ? fnum(a / 1000, 0) + "k" : a >= 1000 ? fnum(a / 1000, 1) + "k" : a >= 100 ? fnum(a, 0) : fnum(a, 1);
        return neg + s + "x";
      case "usd": return fcap(v);
      case "shares": return v >= 1e6 ? fnum(v / 1e6, 1) + "M" : v >= 1e3 ? fnum(v / 1e3, 0) + "K" : fnum(v, 0);
      case "count": return fnum(v, 0);
      default:
        a = Math.abs(v);
        return neg + (a >= 100 ? fnum(a, 0) : a >= 10 ? fnum(a, 1) : fnum(a, 2));
    }
  }

  /* ---------- query language ---------- */
  var NUMRE = "([+-]?(?:\\d+\\.?\\d*|\\.\\d+))";
  var CMP = new RegExp("^([a-z0-9_]+)(>=|<=|>|<|=)" + NUMRE + "$");
  var RANGE = new RegExp("^([a-z0-9_]+)[:=]" + NUMRE + "\\.\\." + NUMRE + "$");

  function normQuery(q) {
    return (q || "").replace(/−/g, "-").replace(/σ/g, "").replace(/≥/g, ">=").replace(/≤/g, "<=")
      .replace(/\s*(>=|<=|>|<)\s*/g, "$1").replace(/\s*\.\.\s*/g, "..");
  }
  function tokenize(q) { var t = normQuery(q).trim(); return t ? t.split(/\s+/) : []; }

  function parseQuery(q) {
    var toks = tokenize(q);
    var p = { bands: {}, sectors: [], idx: [], include: {}, hideKinds: {}, text: [], scope: "universe", chips: [] };
    var sectors = Object.keys(SECTOR_SHORT);
    toks.forEach(function (raw, ti) {
      var t = raw.toLowerCase(), m, key;
      if ((m = t.match(RANGE))) {
        key = A2K[m[1]];
        if (!key) return p.chips.push({ ti: ti, err: 1, html: "unknown metric <b>" + esc(m[1]) + "</b>" });
        addBand(p, key, Math.min(+m[2], +m[3]), Math.max(+m[2], +m[3]), ti);
        return;
      }
      if ((m = t.match(CMP))) {
        key = A2K[m[1]];
        if (!key) return p.chips.push({ ti: ti, err: 1, html: "unknown metric <b>" + esc(m[1]) + "</b>, press ? for the names" });
        var v = +m[3];
        if (m[2] === ">" || m[2] === ">=") addBand(p, key, v, Infinity, ti);
        else if (m[2] === "<" || m[2] === "<=") addBand(p, key, -Infinity, v, ti);
        else addBand(p, key, v - 0.25, v + 0.25, ti);
        return;
      }
      if (t.charAt(0) === "+" || t.charAt(0) === "-") {
        var k = t.slice(1), kinds = KIND_SWITCH[k];
        if (!kinds) return p.chips.push({ ti: ti, err: 1, html: "unknown switch <b>" + esc(raw) + "</b>" });
        kinds.forEach(function (kd) { if (t.charAt(0) === "+") p.include[kd] = 1; else p.hideKinds[kd] = 1; });
        p.chips.push({ ti: ti, inc: 1, html: '<span class="cl">' + (t.charAt(0) === "+" ? "show" : "hide") + "</span> <b>" +
          kinds.map(function (kd) { return ctx.KIND_LABEL[kd]; }).join(", ") + "</b>" });
        return;
      }
      if ((m = t.match(/^(sector|sec|s):(.+)$/))) {
        var val = m[2].replace(/[^a-z0-9]/g, ""), hits = [];
        if (val === "none" || val === "unclassified") hits = [""];
        else if (SECTOR_WORDS[val]) hits = [SECTOR_WORDS[val]];
        else sectors.forEach(function (s) { if (s && s.toLowerCase().replace(/[^a-z0-9]/g, "").indexOf(val) >= 0) hits.push(s); });
        if (!hits.length) return p.chips.push({ ti: ti, err: 1, html: "no sector matches <b>" + esc(m[2]) + "</b>" });
        hits.forEach(function (h) { if (p.sectors.indexOf(h) < 0) p.sectors.push(h); });
        p.chips.push({ ti: ti, html: '<span class="cl">sector</span> <b>' + esc(hits.map(function (h) { return SECTOR_SHORT[h]; }).join(" or ")) + "</b>" });
        return;
      }
      if ((m = t.match(/^(idx|index):(.+)$/))) {
        var iv = m[2].replace(/[^a-z0-9]/g, "");
        var list = iv === "sp1500" ? ["S&P 500", "S&P 400", "S&P 600"] : IDX_TOK[iv] ? [IDX_TOK[iv]] : null;
        if (!list) return p.chips.push({ ti: ti, err: 1, html: "unknown index <b>" + esc(m[2]) + "</b>" });
        list.forEach(function (x) { if (p.idx.indexOf(x) < 0) p.idx.push(x); });
        p.chips.push({ ti: ti, html: '<span class="cl">index</span> <b>' + esc(list.join(" or ")) + "</b>" });
        return;
      }
      if ((m = t.match(/^scope:(sector|universe|sec|uni|u|s)$/))) {
        p.scope = m[1].charAt(0) === "s" ? "sector" : "universe";
        p.chips.push({ ti: ti, html: '<span class="cl">scope</span> <b>' + (p.scope === "sector" ? "vs own sector" : "vs universe") + "</b>" });
        return;
      }
      if (t.indexOf(":") >= 0 || /[<>=]/.test(t)) return p.chips.push({ ti: ti, err: 1, html: "could not read <b>" + esc(raw) + "</b>" });
      p.text.push(t);
      p.chips.push({ ti: ti, html: '<span class="cl">text</span> <b>' + esc(raw) + "</b>" });
    });
    Object.keys(p.hideKinds).forEach(function (k) { delete p.include[k]; });
    return p;
  }
  function addBand(p, key, lo, hi, ti) {
    var b = p.bands[key];
    if (b) { b[0] = Math.max(b[0], lo); b[1] = Math.min(b[1], hi); } else p.bands[key] = [lo, hi];
    var txt = lo === -Infinity ? LE + " " + zsig(hi) : hi === Infinity ? GE + " " + zsig(lo) : zsig(lo) + " to " + zsig(hi);
    p.chips.push({ ti: ti, key: key, html: "<b>" + esc(ALIAS[key]) + '</b> <span class="cl">' + esc(BYKEY[key].label) + "</span> " + txt });
  }
  function removeToken(ti) { var toks = tokenize(gst.query); toks.splice(ti, 1); gst.query = toks.join(" "); }
  function setToken(test, tok) {
    var toks = tokenize(gst.query).filter(function (t) { return !test(t.toLowerCase()); });
    if (tok) toks.push(tok);
    gst.query = toks.join(" ");
  }

  /* ---------- screen computation ---------- */
  function runScreen() {
    var p = parseQuery(gst.query);
    var res = mz(p.scope);
    var hidden = {}, hiddenCount = {};
    ctx.NON_OPERATING.forEach(function (k) { if (!p.include[k]) hidden[k] = 1; });
    Object.keys(p.hideKinds).forEach(function (k) { hidden[k] = 1; });
    var base = [], universeN = 0, secSet = null, idxSet = null, incCount = 0;
    if (p.sectors.length) { secSet = {}; p.sectors.forEach(function (s) { secSet[s] = 1; }); }
    if (p.idx.length) { idxSet = {}; p.idx.forEach(function (s) { idxSet[s] = 1; }); }
    for (var i = 0; i < N; i++) {
      var k = S.kind[i];
      if (hidden[k]) { hiddenCount[k] = (hiddenCount[k] || 0) + 1; continue; }
      universeN++;
      if (nonopKind(k)) incCount++;
      if (secSet && !secSet[S.sector[i]]) continue;
      if (idxSet && !idxSet[S.index[i]]) continue;
      if (p.text.length) {
        var tk = S.ticker[i].toLowerCase(), nm = S.name[i].toLowerCase(), ok = true;
        for (var j = 0; j < p.text.length; j++) {
          var w = p.text[j];
          if (tk.indexOf(w) !== 0 && nm.indexOf(w) < 0) { ok = false; break; }
        }
        if (!ok) continue;
      }
      base.push(i);
    }
    var baseIdx = Int32Array.from(base);
    var match = ctx.APTZ.filter(res, p.bands, { idx: baseIdx });
    /* Rows a band cannot judge because the banded field does not apply to their type (withheld by the
       pipeline, status not_applicable). Counted so the page can say what it left out. */
    var keys = Object.keys(p.bands), drop = [], dropKinds = {}, dropKeys = {};
    if (keys.length) for (j = 0; j < base.length; j++) {
      var ii = base[j], kd = S.kind[ii], hit = false;
      for (var q = 0; q < keys.length; q++) if (na(ii, keys[q])) { dropKeys[keys[q]] = 1; hit = true; }
      if (hit) { drop.push(ii); dropKinds[kd] = (dropKinds[kd] || 0) + 1; }
    }
    var order = Array.from(match);
    gridSort(order, res);
    screen = { p: p, res: res, match: match, order: order, universeN: universeN, hidden: hidden, hiddenCount: hiddenCount,
               incCount: incCount, dropN: drop.length, dropKinds: dropKinds, dropKeys: Object.keys(dropKeys) };
    return screen;
  }

  function gridSort(order, res) {
    var k = gst.sort.k, d = gst.sort.dir, get, px = pxArrays();
    if (k === "cap") get = function (i) { return px.cap[i]; };
    else if (k === "chg") get = function (i) { return px.chg[i]; };
    else if (k === "price") get = function (i) { return px.price[i]; };
    else if (k === "tk" || k === "name" || k === "sector") {
      var col = k === "tk" ? S.ticker : k === "name" ? S.name : S.sector;
      order.sort(function (a, b) { return d * String(col[a]).localeCompare(String(col[b])) || a - b; });
      return;
    } else { var z = res.z[k]; get = function (i) { return z[i]; }; }
    order.sort(function (a, b) {
      var x = get(a), y = get(b), xn = x == null || isNaN(x), yn = y == null || isNaN(y);
      if (xn && yn) return a - b;
      if (xn) return 1;
      if (yn) return -1;
      return d * (x - y) || a - b;
    });
  }

  /* ---------- keys and help ---------- */
  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target, tag = t && t.tagName, typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable);
    if (e.key === "Escape") {
      if (helpOpen) { e.preventDefault(); closeHelp(); return; }
      if (typing && cur.page === "stocks") { t.blur(); return; }
    }
    if (typing || cur.page !== "stocks") return;
    if (e.key === "?") { e.preventDefault(); toggleHelp(document.activeElement); return; }
    if (e.key === "/") {
      e.preventDefault();
      var q = document.getElementById("ld-q");
      if (q) { q.focus(); q.select(); }
      return;
    }
    if (pageKeys && pageKeys(e)) e.preventDefault();
  }

  function helpHTML() {
    var groups = A.groups.map(function (g) {
      return "<h3>" + esc(g) + '</h3><div class="cols">' + M.filter(function (m) { return m.group === g; }).map(function (m) {
        return '<div title="' + esc(m.label + ", " + betterTxt(m) + ". " + methodTxt(m.key)) + '"><code>' + esc(ALIAS[m.key]) + "</code> " + esc(m.label) + "</div>";
      }).join("") + "</div>";
    }).join("");
    return '<div class="ld-help" id="ld-help" role="dialog" aria-modal="false" aria-labelledby="ld-help-h">' +
      '<button type="button" class="ld-btn x" data-close aria-label="Close help">Close</button>' +
      '<h2 id="ld-help-h">The screen language</h2>' +
      "<p>Thresholds are in sigma: robust z (median and MAD, clipped at " + String.fromCharCode(177) + "5), measured against the " +
      int(cohortN()) + " operating companies in the " + dateMid(ASOF) + " panel. Tokens combine with AND; a row with no value for a filtered metric does not pass.</p>" +
      "<dl><dt><code>gm&gt;1</code></dt><dd>gross margin z at or above +1" + SIGMA + " (<code>&gt;=</code> reads the same)</dd>" +
      "<dt><code>pe&lt;-0.5</code></dt><dd>P/E z at or below " + MINUS + "0.5" + SIGMA + " (cheaper than typical)</dd>" +
      "<dt><code>roe:1..3</code></dt><dd>a band, both ends included</dd>" +
      "<dt><code>vol=0</code></dt><dd>within " + String.fromCharCode(177) + "0.25" + SIGMA + " of the value</dd>" +
      "<dt><code>sector:energy</code></dt><dd>any part of a sector name (tech, health, staples, realestate); repeat for OR</dd>" +
      "<dt><code>idx:sp500</code></dt><dd>sp500, sp400, sp600, sp1500, nasdaq, nyse, amex</dd>" +
      "<dt><code>+spac +notes +cef</code></dt><dd>show SPAC shells, debt listings or closed-end funds (hidden by default; <code>+nonop</code> for all three)</dd>" +
      "<dt><code>-lp -bdc -trust</code></dt><dd>hide partnerships, BDCs or royalty trusts (shown with a tag by default)</dd>" +
      "<dt><code>scope:sector</code></dt><dd>z against the row's own sector (sectors of 20 or more)</dd>" +
      "<dt><code>apple</code></dt><dd>any other word matches a ticker prefix or a name</dd></dl>" +
      "<h3>Keys</h3><dl><dt><kbd>/</kbd></dt><dd>focus the screen bar</dd><dt><kbd>j</kbd> <kbd>k</kbd></dt><dd>move the row cursor</dd>" +
      "<dt><kbd>Enter</kbd></dt><dd>open the cursor company</dd><dt><kbd>s</kbd></dt><dd>switch universe and sector scope</dd>" +
      "<dt><kbd>z</kbd></dt><dd>cells show raw values or z</dd><dt><kbd>m</kbd></dt><dd>draw the next 150 rows</dd>" +
      "<dt><kbd>b</kbd></dt><dd>add a " + String.fromCharCode(177) + "0.5" + SIGMA + " band around the cursor row on the sorted column (or shift-click a cell)</dd>" +
      "<dt><kbd>Esc</kbd></dt><dd>leave the screen bar, close this help</dd><dt><kbd>?</kbd></dt><dd>this help</dd></dl>" +
      "<h3>Colour</h3><p>Cells are tinted by z: slate below the cohort median, ochre above, plain paper near zero. The tint marks position, not merit: for P/E a high z means expensive.</p>" +
      '<h2 style="margin-top:18px">Metric names</h2>' + groups + "</div>";
  }
  function toggleHelp(ret) { if (helpOpen) closeHelp(); else openHelp(ret); }
  function openHelp(ret) {
    closeHelp();
    helpReturn = ret || null;
    var wrap = document.createElement("div");
    wrap.innerHTML = helpHTML();
    var el = wrap.firstChild;
    rootEl.appendChild(el);
    el.querySelector("[data-close]").addEventListener("click", closeHelp);
    el.querySelector("[data-close]").focus();
    helpOpen = true;
  }
  function closeHelp() {
    var el = document.getElementById("ld-help");
    if (el) el.remove();
    if (helpOpen && helpReturn && helpReturn.focus && document.contains(helpReturn)) helpReturn.focus();
    helpOpen = false; helpReturn = null;
  }

  /* ---------- STOCKS page ---------- */
  /* The query lives in the URL (stocks.html#q=gm>1 pe<-0.5), so a screen can be bookmarked or linked.
     A bare preset id (stocks.html#qarp) opens that ready-made screen. */
  function queryFromHash() {
    var h = (location.hash || "").replace(/^#/, "");
    if (!h) return "";
    if (h.slice(0, 2) === "q=") { try { return decodeURIComponent(h.slice(2).replace(/\+/g, " ")); } catch (e) { return h.slice(2); } }
    return PRESET_Q[h] || "";
  }
  function writeHash() {
    var want = gst.query.trim() ? "#q=" + encodeURIComponent(gst.query.trim()) : "";
    if ((location.hash || "") !== want) {
      try { history.replaceState(null, "", want || location.pathname + location.search); } catch (e) { /* file: or sandboxed */ }
    }
  }
  function renderStocks(main) {
    initAliases();
    gst.query = queryFromHash();
    var narrow = window.innerWidth < 560;
    var presets = PRESETS.map(function (p) { return [p.name, p.q]; });
    main.innerHTML = '<div class="ld-wrap">' +
      '<div class="ld-shead"><h1>Screener</h1><span class="ld-kicker">' + int(N) + " listings " + MID + " " + A.metrics.length + " metrics " + MID + " close " + dateShort(PRICE_DATE) + " " + MID + " panel " + dateShort(ASOF) + "</span></div>" +
      '<section class="ld-cmd" aria-label="Screen">' +
      '<div class="ld-cmdrow"><label for="ld-q">Screen</label>' +
      '<input id="ld-q" name="ld-q" autocomplete="off" spellcheck="false" autocapitalize="off" value="' + esc(gst.query) + '" placeholder="' +
      (narrow ? "gm>1 pe<-0.5 sector:tech" : "gm>1 pe<-0.5 vol<0 sector:tech") + '" aria-describedby="ld-qchips">' +
      '<button type="button" class="ld-btn" data-help-local aria-label="Screen language and keys">? Help</button></div>' +
      '<div class="ld-qchips" id="ld-qchips" aria-live="polite"></div>' +
      '<div class="ld-qpre" aria-label="Ready-made screens"><span class="ld-kicker">Ready-made</span>' + presets.map(function (x) {
        return '<button type="button" class="ld-chip" data-preset="' + esc(x[1]) + '" title="' + esc(x[1]) + '">' + esc(x[0]) + "</button>";
      }).join("") + "</div></section>" +
      '<div class="ld-stat" id="ld-stat"></div>' +
      '<div class="ld-read" id="ld-read" aria-live="off">' + (isTouch()
        ? "Tap a row to open the company. The header histograms show each metric's spread across the operating universe."
        : "Hover a cell to read its value, z and percentile. Click a row to open the company; shift-click a metric cell to add a " + String.fromCharCode(177) + "0.5" + SIGMA + " band around it.") + "</div>" +
      '<div class="ld-gwrap" id="ld-grid" role="region" aria-label="Screener grid, scrolls both ways" tabindex="0"></div>' +
      '<p class="ld-gfoot">Panel dated ' + dateMid(ASOF) + "; prices and 1D are the " + dateMid(PRICE_DATE) + " close. z is robust (median and MAD); market cap and volume are log-scaled first. " +
      "A dot marks a missing value; n/a marks a field that is only a placeholder for the listing's type.</p></div>";

    var q = main.querySelector("#ld-q");
    q.addEventListener("input", function () {
      if (debounceId) clearTimeout(debounceId);
      debounceId = setTimeout(function () { debounceId = 0; gst.query = q.value; gst.limit = 150; gst.cursor = 0; updateScreen(true); }, 90);
    });
    q.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      if (debounceId) { clearTimeout(debounceId); debounceId = 0; gst.query = q.value; gst.limit = 150; gst.cursor = 0; updateScreen(true); }
      var v = q.value.trim().toUpperCase();
      if (v && ctx.row(v) >= 0 && tokenize(v).length === 1) { ctx.go("company", v); return; }
      q.blur();
    });
    main.querySelector("[data-help-local]").addEventListener("click", function (e) { toggleHelp(e.currentTarget); });
    main.querySelectorAll("[data-preset]").forEach(function (b) {
      b.addEventListener("click", function () { gst.query = b.getAttribute("data-preset"); q.value = gst.query; gst.limit = 150; gst.cursor = 0; updateScreen(true); });
    });
    main.querySelector("#ld-qchips").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-ti]");
      if (!b) return;
      removeToken(+b.getAttribute("data-ti"));
      q.value = gst.query; gst.limit = 150; gst.cursor = 0;
      updateScreen(true);
      var next = main.querySelector("#ld-qchips button");
      (next || q).focus();
    });
    main.querySelector("#ld-stat").addEventListener("click", onStatClick);
    var grid = main.querySelector("#ld-grid");
    grid.addEventListener("click", onGridClick);
    grid.addEventListener("mouseover", onGridHover);
    pageKeys = stocksKeys;
    redrawers.push(function () { if ((window.innerWidth < 560) !== gridNarrow) renderGrid({ keepScroll: true }); });
    listen(window, "hashchange", function () {
      var nq = queryFromHash();
      if (nq === gst.query.trim()) return;
      gst.query = nq; syncQuery(); updateScreen(true);
    });
    updateScreen(false);
  }

  function onStatClick(e) {
    var b = e.target.closest("button");
    if (!b) return;
    var a = b.getAttribute("data-act");
    if (a === "scope") toggleScope(b.getAttribute("data-v"));
    else if (a === "cells") { gst.cells = b.getAttribute("data-v"); renderGrid({ keepScroll: true }); renderStat(); }
    else if (a === "nonop") {
      var on = b.getAttribute("aria-pressed") === "true";
      setToken(function (t) { return /^\+(spac|spacs|notes|note|debt|cef|cefs|nonop|all)$/.test(t); }, on ? "" : "+nonop");
      syncQuery(); updateScreen(true);
    } else if (a === "reset") { gst.query = ""; gst.sort = { k: "cap", dir: -1 }; syncQuery(); updateScreen(true); }
    var again = document.querySelector('#ld-stat [data-act="' + a + '"]' + (b.getAttribute("data-v") ? '[data-v="' + b.getAttribute("data-v") + '"]' : ""));
    if (again) again.focus();
  }
  function syncQuery() { var q = document.getElementById("ld-q"); if (q) q.value = gst.query; gst.limit = 150; gst.cursor = 0; }
  function toggleScope(v) {
    var now = screen ? screen.p.scope : "universe";
    var next = v || (now === "sector" ? "universe" : "sector");
    setToken(function (t) { return /^scope:/.test(t); }, next === "sector" ? "scope:sector" : "");
    var q = document.getElementById("ld-q"); if (q) q.value = gst.query;
    updateScreen(false);
  }
  function updateScreen(resetScroll) {
    runScreen();
    writeHash();
    if (gst.cursor >= screen.order.length) gst.cursor = Math.max(0, screen.order.length - 1);
    renderChips();
    renderStat();
    renderGrid({ keepScroll: !resetScroll });
    if (resetScroll) { var g = document.getElementById("ld-grid"); if (g) g.scrollTop = 0; }
  }

  function renderChips() {
    var el = document.getElementById("ld-qchips");
    if (!el) return;
    var ch = screen.p.chips;
    el.innerHTML = ch.length ? ch.map(function (c) {
      return '<span class="ld-qchip' + (c.err ? " err" : "") + (c.inc ? " inc" : "") + '"><span class="ct">' + c.html + "</span>" +
        '<button type="button" data-ti="' + c.ti + '" aria-label="Remove this filter">' + TIMES + "</button></span>";
    }).join("") : '';
  }

  function renderStat() {
    var el = document.getElementById("ld-stat");
    if (!el) return;
    var s = screen, p = s.p, hc = s.hiddenCount;
    var hiddenNon = 0, parts = [];
    ctx.NON_OPERATING.forEach(function (k) { hiddenNon += hc[k] || 0; });
    var debtLike = (hc.debt || 0) + (hc.structured || 0) + (hc.equity_units || 0);
    if (hc.spac) parts.push(int(hc.spac) + " SPAC shells");
    if (debtLike) parts.push(int(debtLike) + " exchange-listed notes and certificates");
    if (hc.cef) parts.push(int(hc.cef) + " closed-end funds");
    var what = s.incCount ? "listings" : "operating companies";
    var hid = hiddenNon ? int(hiddenNon) + " non-operating listings hidden (" + parts.join(", ") + "); turn on Non-operating to include them." :
      "Non-operating listings are shown with a tag (" + int(s.incCount) + "); their z is still measured against operating companies.";
    var extra = [];
    if (hc.lp) extra.push(int(hc.lp) + " partnerships");
    if (hc.bdc) extra.push(int(hc.bdc) + " BDCs");
    if (hc.royalty_trust) extra.push(int(hc.royalty_trust) + " royalty trusts");
    var drop = "";
    if (s.dropN) {
      var dk = Object.keys(s.dropKinds);
      drop = " " + int(s.dropN) + " " + (s.dropN === 1 ? "listing" : "listings") + " (" + dk.map(function (k) { return int(s.dropKinds[k]) + " " + (s.dropKinds[k] === 1 ? KIND_WHY[k] || k : KIND_PLURAL[k] || k); }).join(", ") +
        ") cannot be judged on " + esc(s.dropKeys.map(function (k) { return BYKEY[k].label.toLowerCase(); }).join(" or ")) +
        ", which does not apply to their type, so they are left out.";
    }
    var scope = p.scope;
    el.innerHTML = '<p class="cnt" id="ld-count" data-n="' + s.match.length + '" data-m="' + s.universeN + '"><b>' + int(s.match.length) + "</b> of " + int(s.universeN) + " " + what + " match</p>" +
      '<div class="tools">' +
      '<div class="ld-seg" role="group" aria-label="Measure z against"><button type="button" data-act="scope" data-v="universe" aria-pressed="' + (scope === "universe") + '">Universe</button>' +
      '<button type="button" data-act="scope" data-v="sector" aria-pressed="' + (scope === "sector") + '" title="Switch scope (s)">Sector</button></div>' +
      '<div class="ld-seg" role="group" aria-label="Cells show"><button type="button" data-act="cells" data-v="raw" aria-pressed="' + (gst.cells === "raw") + '">Raw</button>' +
      '<button type="button" data-act="cells" data-v="z" aria-pressed="' + (gst.cells === "z") + '" title="Switch cells (z)">z</button></div>' +
      '<button type="button" class="ld-btn" data-act="nonop" aria-pressed="' + (hiddenNon === 0) + '">Non-operating</button>' +
      '<button type="button" class="ld-btn" data-act="reset">Reset</button>' +
      '<span class="ld-zleg" aria-label="Tint scale, from 3 sigma below the median to 3 sigma above"><span>' + MINUS + "3" + SIGMA + '</span><i class="ld-zn6"></i><i class="ld-zn4"></i><i class="ld-zn2"></i><i class="z0"></i><i class="ld-zp2"></i><i class="ld-zp4"></i><i class="ld-zp6"></i><span>+3' + SIGMA + "</span></span></div>" +
      '<p class="hid" title="' + esc(hid) + '">' + (hiddenNon ? int(hiddenNon) + " non-operating hidden" : int(s.incCount) + " non-operating shown, tagged") + " " + MID + " " + (extra.length ? " Hidden by your query: " + extra.join(", ") + "." : "") +
      (scope === "sector" ? "z within each sector." : "z vs " + int(cohortN()) + " operating companies.") +
      (Object.keys(p.bands).length ? " A row with no value for a filtered metric does not pass." : "") + drop + "</p>";
  }

  function cohortHist(scope, key) {
    var ck = scope + "|" + key;
    if (!gHistCache[ck]) gHistCache[ck] = ctx.APTZ.hist(mz(scope), key, 24, -4, 4);
    return gHistCache[ck];
  }
  function histSVG(key, band, scope) {
    var h = cohortHist(scope, key), W = 60, H = 16, n = h.counts.length, bw = W / n, mx = Math.max.apply(null, h.counts) || 1;
    var s = '<svg width="' + W + '" height="' + (H + 2) + '" viewBox="0 0 ' + W + " " + (H + 2) + '" aria-hidden="true" data-k="' + key + '">';
    for (var b = 0; b < n; b++) {
      var c = h.counts[b];
      if (!c) continue;
      var hh = Math.max(1, Math.round(c / mx * H)), lo = h.edges[b], hi = h.edges[b + 1], mid = (lo + hi) / 2;
      var inb = band && hi > band[0] && lo < band[1];
      var cls = inb ? (mid < 0 ? "h-blo" : "h-bhi") : mid < -0.5 ? "h-lo" : mid > 0.5 ? "h-hi" : "h-bar";
      s += '<rect class="' + cls + '" x="' + (b * bw + 0.3).toFixed(1) + '" y="' + (H - hh) + '" width="' + (bw - 0.6).toFixed(1) + '" height="' + hh + '"/>';
    }
    s += '<line class="h-z" x1="' + W / 2 + '" x2="' + W / 2 + '" y1="' + H + '" y2="' + (H + 2) + '"/>';
    s += '<line class="h-cur" data-cur x1="-5" x2="-5" y1="0" y2="' + (H + 2) + '"/>';
    return s + "</svg>";
  }

  /* opts.syncTo: draw at least up to this row now (keyboard cursor). opts.keepScroll: keep the scroll position. */
  function renderGrid(opts) {
    opts = opts || {};
    var wrap = document.getElementById("ld-grid");
    if (!wrap || !screen) return;
    var keepTop = opts.keepScroll ? wrap.scrollTop : -1;
    var s = screen, res = s.res, p = s.p, sortK = gst.sort.k, arrow = gst.sort.dir < 0 ? "▾" : "▴";
    var groups = A.groups, cols = [];
    groups.forEach(function (g) { M.forEach(function (m) { if (m.group === g) cols.push(m); }); });
    var fixed = [["tk", "Ticker"], ["name", "Name"], ["sector", "Sector"], ["price", "Last"], ["cap", "Cap"], ["chg", "1D"]];
    gridNarrow = window.innerWidth < 560;
    var W = { tk: 92, name: gridNarrow ? 124 : 200, sector: 104, price: 84, cap: 70, chg: 72 }, MW = 76;
    var tw = W.tk + W.name + W.sector + W.price + W.cap + W.chg + MW * cols.length;
    var h = '<table class="ld-g" style="width:' + tw + 'px"><caption class="ld-sr">Matching listings with ' + cols.length + " metrics, " + (gst.cells === "z" ? "shown as z" : "shown as raw values") + ", tinted by z</caption><colgroup>" +
      fixed.map(function (f) { return '<col style="width:' + W[f[0]] + 'px">'; }).join("") +
      cols.map(function () { return '<col style="width:' + MW + 'px">'; }).join("") + '</colgroup><thead><tr class="grp"><th class="sl" scope="col"></th><th colspan="5" scope="colgroup">Listing</th>';
    groups.forEach(function (g) {
      var n = cols.filter(function (m) { return m.group === g; }).length;
      if (n) h += '<th colspan="' + n + '" class="gs" scope="colgroup">' + esc(g) + "</th>";
    });
    h += '</tr><tr class="hdr">';
    fixed.forEach(function (f, fi) {
      var on = sortK === f[0];
      h += '<th scope="col" class="' + (fi === 0 ? "sl " : "") + (fi >= 3 ? "r " : "") + (on ? "on" : "") + '"' + (on ? ' aria-sort="' + (gst.sort.dir < 0 ? "descending" : "ascending") + '"' : "") +
        '><button type="button" class="colbtn" data-sort="' + f[0] + '"><span class="al">' + f[1] + (on ? '<span class="ar">' + arrow + "</span>" : "") + "</span></button></th>";
    });
    var prevG = null;
    cols.forEach(function (m) {
      var on = sortK === m.key, gs = m.group !== prevG ? " gs" : "";
      prevG = m.group;
      h += '<th scope="col" class="' + (on ? "on" : "") + gs + '"' + (on ? ' aria-sort="' + (gst.sort.dir < 0 ? "descending" : "ascending") + '"' : "") +
        '><button type="button" class="colbtn" data-sort="' + m.key + '" title="' + esc(m.label + " (" + betterTxt(m) + "). Click to sort by z.") + '">' +
        '<span class="al">' + esc(ALIAS[m.key]) + (on ? '<span class="ar">' + arrow + "</span>" : "") + "</span>" +
        '<span class="ld-sr">' + esc(m.label) + "</span>" + histSVG(m.key, p.bands[m.key], p.scope) + "</button></th>";
    });
    h += "</tr></thead><tbody>";
    var lim = Math.min(gst.limit, s.order.length);
    /* The first rows now, the rest on the next frame, so the page paints quickly. */
    var need = Math.max(50, (opts.syncTo || 0) + 12, keepTop > 0 ? Math.ceil((keepTop + wrap.clientHeight) / 26) + 6 : 0);
    var first = Math.min(lim, need), rest = "";
    for (var r = 0; r < first; r++) h += rowHTML(r, s.order[r], cols, res);
    for (r = first; r < lim; r++) rest += rowHTML(r, s.order[r], cols, res);
    h += "</tbody></table>";
    if (!s.order.length) h += '<div class="ld-gmore">No rows match. Remove a filter, widen a band, or turn on non-operating listings.</div>';
    else if (lim < s.order.length) {
      var nx = Math.min(150, s.order.length - lim);
      h += '<div class="ld-gmore"><span>Rows 1 to ' + int(lim) + " of " + int(s.order.length) + ' shown.</span><button type="button" class="ld-btn" data-more>Show the next ' + nx + "</button></div>";
    } else h += '<div class="ld-gmore"><span>All ' + int(s.order.length) + " matching rows shown.</span></div>";
    var sl = wrap.scrollLeft;
    wrap.innerHTML = h;
    wrap.scrollLeft = sl;
    if (keepTop >= 0) wrap.scrollTop = keepTop;
    wrap.__cols = cols;
    markCursor(!!opts.syncTo);
    if (keepTop >= 0) keepTop = wrap.scrollTop;
    if (chunkId) { cancelAnimationFrame(chunkId); chunkId = 0; }
    if (rest) {
      var tb = wrap.querySelector("tbody");
      chunkId = requestAnimationFrame(function () {
        chunkId = 0;
        if (!document.contains(tb)) return;
        tb.insertAdjacentHTML("beforeend", rest);
        if (keepTop >= 0) wrap.scrollTop = keepTop;
        markCursor(false);
      });
    }
  }

  function rowHTML(r, i, cols, res) {
    var tag = kindTag(S.kind[i]);
    var capBlank = na(i, "market_cap"), chgBlank = false;
    var s = '<tr data-r="' + r + '" data-i="' + i + '" class="' + (r === gst.cursor ? "cur" : "") + '">' +
      '<td class="sl tkc"><a class="ld-gtk" href="' + ctx.href("company", S.ticker[i]) + '">' + esc(S.ticker[i]) + "</a>" + tag + "</td>" +
      '<td class="nm" title="' + esc(S.name[i]) + '">' + esc(S.name[i]) + "</td>" +
      '<td class="sc">' + esc(SECTOR_SHORT[S.sector[i]] || S.sector[i]) + "</td>" +
      "<td>" + (S.price[i] == null ? '<span class="ld-faint">' + MID + "</span>" : fnum(S.price[i], 2)) + "</td>" +
      (capBlank ? '<td class="na">n/a</td>' : "<td>" + fcap(S.mcap_raw[i]) + "</td>") +
      (chgBlank ? '<td class="na">n/a</td>' : '<td class="' + upDn(S.chg[i]) + '">' + fpct(S.chg[i]) + "</td>");
    var prevG = null, zmode = gst.cells === "z";
    for (var c = 0; c < cols.length; c++) {
      var m = cols[c], gs = m.group !== prevG ? " gs" : "";
      prevG = m.group;
      if (na(i, m.key)) { s += '<td class="na' + gs + '" data-na="' + c + '">n/a</td>'; continue; }
      var z = res.z[m.key][i], v = A.vals[m.key][i];
      var txt = zmode ? (isNaN(z) ? null : fsgn(z, 2)) : cellFmt(m, v);
      if (txt == null) { s += '<td class="ms' + gs + '" data-c="' + c + '">' + MID + "</td>"; continue; }
      var zc = zClass(z);
      s += '<td class="' + (zc + gs).trim() + '" data-c="' + c + '">' + txt + "</td>";
    }
    return s + "</tr>";
  }

  function onGridClick(e) {
    var b = e.target.closest("button");
    if (b && b.hasAttribute("data-sort")) {
      var k = b.getAttribute("data-sort");
      if (gst.sort.k === k) gst.sort.dir = -gst.sort.dir;
      else gst.sort = { k: k, dir: k === "tk" || k === "name" || k === "sector" ? 1 : -1 };
      gridSort(screen.order, screen.res);
      gst.cursor = 0;
      renderGrid();
      var g = document.getElementById("ld-grid"); if (g) g.scrollTop = 0;
      var nb = document.querySelector('#ld-grid [data-sort="' + k + '"]'); if (nb) nb.focus();
      return;
    }
    if (b && b.hasAttribute("data-more")) { showMore(); return; }
    if (e.target.closest("a")) return;
    var td = e.target.closest("td"), tr = e.target.closest("tr[data-r]");
    if (!tr) return;
    var i = +tr.getAttribute("data-i"), cols = e.currentTarget.__cols;
    gst.cursor = +tr.getAttribute("data-r");
    if ((e.shiftKey || e.altKey) && td && (td.hasAttribute("data-c") || td.hasAttribute("data-na"))) {
      markCursor(false);
      if (td.hasAttribute("data-na")) { setReadout("<b>" + esc(S.ticker[i]) + "</b> " + esc(cols[+td.getAttribute("data-na")].label) + ": " + blankReason(S.kind[i]) + ". A placeholder cannot seed a band."); return; }
      addBandAround(cols[+td.getAttribute("data-c")].key, i);
      return;
    }
    ctx.go("company", S.ticker[i]);
  }
  function isTouch() { return !!(window.matchMedia && window.matchMedia("(hover: none)").matches); }

  function addBandAround(key, i) {
    if (na(i, key)) { setReadout(esc(S.ticker[i]) + " " + esc(BYKEY[key].label) + " is " + blankReason(S.kind[i]) + ", so no band was added."); return; }
    var z = screen.res.z[key][i];
    if (isNaN(z)) { setReadout(esc(S.ticker[i]) + " has no " + esc(BYKEY[key].label) + " z, so no band was added."); return; }
    var lo = Math.round((z - 0.5) * 100) / 100, hi = Math.round((z + 0.5) * 100) / 100, a = ALIAS[key];
    setToken(function (t) { var m = t.match(/^([a-z0-9_]+)(?:[<>=:]|$)/); return m && A2K[m[1]] === key; }, a + ":" + lo + ".." + hi);
    syncQuery();
    updateScreen(true);
    setReadout("Added the band <b>" + esc(a) + "</b> " + zsig(lo) + " to " + zsig(hi) + " around " + esc(S.ticker[i]) + ".");
  }
  function setReadout(html) { var r = document.getElementById("ld-read"); if (r) r.innerHTML = html; }

  function onGridHover(e) {
    if (isTouch()) return;
    var tr = e.target.closest("tr[data-i]"), td = e.target.closest("td[data-c],td[data-na]");
    if (!td || !tr) return;
    var c = td.hasAttribute("data-c") ? td.getAttribute("data-c") : td.getAttribute("data-na");
    cellReadout(e.currentTarget.__cols[+c], +tr.getAttribute("data-i"));
  }
  function cellReadout(m, i) {
    var kind = S.kind[i], tk = "<b>" + esc(S.ticker[i]) + "</b> " + esc(m.label) + ": ";
    if (na(i, m.key)) { setReadout(tk + blankReason(kind)); return; }
    var v = A.vals[m.key][i], zU = mz("universe").z[m.key][i], zS = screen.p.scope === "sector" ? screen.res.z[m.key][i] : NaN;
    var stt = S.status[i] && S.status[i][m.key];
    if (v == null) { setReadout(tk + "no value" + (stt ? " (" + (STATUS_WHY[stt] || stt) + ")" : "")); return; }
    var pc = pctl(m.key, i);
    setReadout(tk + "<b>" + esc(ctx.APTZ.fmt(m.key, v)) + "</b>, universe z <b>" + zsig(zU) + "</b>" +
      (screen.p.scope === "sector" ? ", sector z <b>" + zsig(zS) + "</b>" : "") +
      (isNaN(pc) ? "" : ", percentile " + fnum(pc, 0)) + (stt ? ", " + (STATUS_WHY[stt] || stt) : "") + ", " + betterTxt(m));
  }
  function showMore(syncTo) { gst.limit += 150; renderGrid({ keepScroll: true, syncTo: syncTo || 0 }); }

  function markCursor(scroll) {
    var wrap = document.getElementById("ld-grid");
    if (!wrap) return;
    var old = wrap.querySelector("tr.cur"); if (old) old.classList.remove("cur");
    var tr = wrap.querySelector('tr[data-r="' + gst.cursor + '"]');
    var i = tr ? +tr.getAttribute("data-i") : -1;
    wrap.querySelectorAll("svg[data-k]").forEach(function (svg) {
      var ln = svg.querySelector("[data-cur]"), z = i >= 0 ? screen.res.z[svg.getAttribute("data-k")][i] : NaN;
      var x = isNaN(z) ? -5 : Math.max(0.5, Math.min(59.5, (Math.max(-4, Math.min(4, z)) + 4) / 8 * 60));
      ln.setAttribute("x1", x.toFixed(1)); ln.setAttribute("x2", x.toFixed(1));
    });
    if (!tr) return;
    tr.classList.add("cur");
    if (scroll) {
      var head = wrap.querySelector("thead").offsetHeight, top = tr.offsetTop, hgt = tr.offsetHeight;
      if (top - head < wrap.scrollTop) wrap.scrollTop = top - head;
      else if (top + hgt > wrap.scrollTop + wrap.clientHeight - 4) wrap.scrollTop = top + hgt - wrap.clientHeight + 4;
    }
  }

  function stocksKeys(e) {
    var k = e.key, n = screen ? screen.order.length : 0, inGrid = e.target && e.target.closest && e.target.closest("#ld-grid");
    if (k === "j" || (k === "ArrowDown" && inGrid)) {
      if (!n) return true;
      gst.cursor = Math.min(n - 1, gst.cursor + 1);
      if (gst.cursor >= gst.limit) showMore(gst.cursor);
      markCursor(true); cursorReadout(); return true;
    }
    if (k === "k" || (k === "ArrowUp" && inGrid)) { gst.cursor = Math.max(0, gst.cursor - 1); markCursor(true); cursorReadout(); return true; }
    if (k === "Enter" && n && !(e.target && e.target.closest && e.target.closest("a,button"))) { ctx.go("company", S.ticker[screen.order[gst.cursor]]); return true; }
    if (k === "s") { toggleScope(); return true; }
    if (k === "z") { gst.cells = gst.cells === "z" ? "raw" : "z"; renderGrid({ keepScroll: true }); renderStat(); return true; }
    if (k === "m") { if (gst.limit < n) showMore(); return true; }
    if (k === "b" && n) {
      var key = BYKEY[gst.sort.k] ? gst.sort.k : null;
      if (!key) { setReadout("Sort by a metric column first: <b>b</b> bands the sorted metric around the cursor row."); return true; }
      addBandAround(key, screen.order[gst.cursor]); return true;
    }
    return false;
  }
  function cursorReadout() {
    if (!screen || !screen.order.length) return;
    var i = screen.order[gst.cursor];
    setReadout("Cursor on <b>" + esc(S.ticker[i]) + "</b> " + esc(S.name[i]) + ", row " + (gst.cursor + 1) + " of " + int(screen.order.length) +
      ". Enter opens it; the red tick in each header histogram marks its z.");
  }

  /* Universe percentile, exact, from the cohort's own values. */
  function pctl(key, i) {
    var U = ctx.z("universe"), arr = sortedCache[key];
    if (!arr) {
      var raw = U.raw[key], mk = U.cohortMask, a = [];
      for (var j = 0; j < N; j++) if (mk[j] && !isNaN(raw[j])) a.push(raw[j]);
      arr = sortedCache[key] = Float64Array.from(a).sort();
    }
    var v = U.raw[key][i];
    if (isNaN(v) || !arr.length) return NaN;
    var lo = 0, hi = arr.length, mid;
    while (lo < hi) { mid = (lo + hi) >> 1; if (arr[mid] < v) lo = mid + 1; else hi = mid; }
    var l = lo; hi = arr.length;
    while (lo < hi) { mid = (lo + hi) >> 1; if (arr[mid] <= v) lo = mid + 1; else hi = mid; }
    return (l + (lo - l) / 2) / arr.length * 100;
  }
  /* ---------------------------------------------------------------- charts */

  function niceStep(range, n) {
    var raw = range / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
  }

  function priceChart(host, pts, note) {
    function draw() {
      var W = Math.max(280, host.clientWidth || 640), H = W < 520 ? 210 : 280;
      var pl = 4, pr = 58, pt = 10, pb = 26, iw = W - pl - pr, ih = H - pt - pb;
      var vs = pts.map(function (p) { return p.v; });
      var mn = Math.min.apply(null, vs), mx = Math.max.apply(null, vs);
      if (mn === mx) { mn *= 0.98; mx *= 1.02; }
      var step = niceStep(mx - mn, 4), y0 = Math.floor(mn / step) * step, y1 = Math.ceil(mx / step) * step;
      function X(j) { return pl + iw * j / Math.max(1, pts.length - 1); }
      function Y(v) { return pt + ih * (1 - (v - y0) / (y1 - y0)); }
      var g = [];
      for (var y = y0; y <= y1 + step / 2; y += step) {
        g.push('<line class="gl" x1="' + pl + '" x2="' + (pl + iw) + '" y1="' + Y(y).toFixed(1) + '" y2="' + Y(y).toFixed(1) + '"/>');
        g.push('<text x="' + (pl + iw + 8) + '" y="' + (Y(y) + 3.5).toFixed(1) + '">' + (y < 0 ? MINUS : "") + "$" + num(Math.abs(y), step < 1 ? 2 : 0) + "</text>");
      }
      var lastM = -1, xl = [], lastX = -99;
      pts.forEach(function (p, j) {
        var d = dt(p.d), m = d.getUTCMonth();
        if (m !== lastM) {
          if (lastM >= 0 && X(j) - lastX > 38 && X(j) < pl + iw - 20) { xl.push('<text x="' + X(j).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle">' + MONTHS[m].slice(0, 3) + (m === 0 ? " " + String(d.getUTCFullYear()).slice(2) : "") + "</text>"); lastX = X(j); }
          lastM = m;
        }
      });
      var line = pts.map(function (p, j) { return (j ? "L" : "M") + X(j).toFixed(1) + " " + Y(p.v).toFixed(1); }).join("");
      var area = line + "L" + X(pts.length - 1).toFixed(1) + " " + (pt + ih) + "L" + pl + " " + (pt + ih) + "Z";
      var last = pts[pts.length - 1];
      host.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" height="' + H + '" role="img" aria-label="' + esc(note) + '">' +
        g.join("") + '<path class="ar" d="' + area + '"/><path class="ln" d="' + line + '"/>' +
        '<line class="bl" x1="' + pl + '" x2="' + (pl + iw) + '" y1="' + (pt + ih) + '" y2="' + (pt + ih) + '"/>' + xl.join("") +
        '<circle class="dot" cx="' + X(pts.length - 1).toFixed(1) + '" cy="' + Y(last.v).toFixed(1) + '" r="4"/>' +
        '<line class="xh" id="ld-xh" x1="0" x2="0" y1="' + pt + '" y2="' + (pt + ih) + '" style="display:none"/>' +
        '<circle class="dot" id="ld-xd" r="4.5" cx="0" cy="0" style="display:none"/>' +
        '<rect x="' + pl + '" y="' + pt + '" width="' + iw + '" height="' + ih + '" fill="transparent" id="ld-hit"/></svg>' +
        '<div class="ld-tip" id="ld-tip"></div>';
      var svg = host.querySelector("svg"), tip = host.querySelector("#ld-tip"), xh = host.querySelector("#ld-xh"), xd = host.querySelector("#ld-xd");
      function move(ev) {
        var r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * W / r.width;
        var j = Math.round((sx - pl) / iw * (pts.length - 1));
        j = Math.max(0, Math.min(pts.length - 1, j));
        var x = X(j), yv = Y(pts[j].v);
        xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.style.display = "";
        xd.setAttribute("cx", x); xd.setAttribute("cy", yv); xd.style.display = "";
        tip.textContent = dateMid(pts[j].d) + "  " + money(pts[j].v);
        tip.style.left = Math.max(60, Math.min(r.width - 60, x * r.width / W)) + "px";
        tip.style.top = (yv * r.height / H) + "px";
        tip.style.opacity = "1";
      }
      function leave() { xh.style.display = "none"; xd.style.display = "none"; tip.style.opacity = "0"; }
      svg.addEventListener("pointermove", move);
      svg.addEventListener("pointerleave", leave);
    }
    draw();
    redrawers.push(draw);
  }

  function sparkSvg(vals) {
    var W = 200, H = 54, v = vals.filter(function (x) { return x != null && isFinite(x); });
    if (v.length < 2) return "";
    var mn = Math.min.apply(null, v), mx = Math.max.apply(null, v);
    var lo = Math.min(mn, 0 < mn ? mn : 0), hi = Math.max(mx, 0 > mx ? mx : 0);
    if (mn > 0) lo = mn; if (mx < 0) hi = mx;
    if (hi === lo) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    function X(j) { return W * j / (vals.length - 1); }
    function Y(x) { return H * (1 - (x - lo) / (hi - lo)); }
    var d = "", started = false;
    vals.forEach(function (x, j) {
      if (x == null || !isFinite(x)) { started = false; return; }
      d += (started ? "L" : "M") + X(j).toFixed(1) + " " + Y(x).toFixed(1); started = true;
    });
    var zero = lo < 0 && hi > 0 ? '<line class="zero" x1="0" x2="' + W + '" y1="' + Y(0).toFixed(1) + '" y2="' + Y(0).toFixed(1) + '"/>' : "";
    var li = vals.length - 1; while (li > 0 && (vals[li] == null || !isFinite(vals[li]))) li--;
    return '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" aria-hidden="true">' + zero + '<path class="ln" d="' + d + '"/></svg>';
  }

  /* ---------------------------------------------------------------- COMPANY
   * company.html#TICKER (and #TICKER/thesis to open at the thesis). The row comes from stocks-data.json; the
   * rest from the per-ticker files the pipeline writes beside it, each fetched only when CFG.have says it
   * exists, so a ticker without one costs no request and logs no 404:
   *   company/T.json  reported history, filings held, peer share, business excerpt (write_company_views)
   *   thesis/T.json   the current view and every note (write_thesis_views)
   *   prices/T.json   daily closes for the chart; news/T.json the latest headlines */

  var cst = { scope: "universe" };
  var HAVE = null;
  function have(kind, tk) {
    if (!HAVE) {
      HAVE = {};
      var h = CFG.have || {};
      ["company", "thesis", "noPrices", "noNews"].forEach(function (k) {
        var m = {}; (h[k] || []).forEach(function (t) { m[t] = 1; }); HAVE[k] = m;
      });
    }
    if (kind === "prices") return !HAVE.noPrices[tk];
    if (kind === "news") return !HAVE.noNews[tk];
    return !!HAVE[kind][tk];
  }
  function parseCompanyHash() {
    var h = (location.hash || "").replace(/^#/, ""), parts;
    try { h = decodeURIComponent(h); } catch (e) { /* keep it raw */ }
    parts = h.split("/");
    return { tk: (parts[0] || "").trim().toUpperCase(), thesis: parts[1] === "thesis" };
  }

  function renderCompany(main) {
    var route = parseCompanyHash(), tk = route.tk || CFG.sampleTicker || "AAPL";
    document.title = tk + ", Apterreon";
    var i = ctx.row(tk);
    if (i < 0) {
      main.innerHTML = '<div class="ld-wrap"><div class="ld-head"><div><div class="ld-kicker"><b>Company</b></div><h1 class="ld-h1">No listing called ' + esc(tk) + '</h1><p class="ld-deck">The ' + esc(ASOF ? dateMid(ASOF) + " " : "") + 'panel has no row for that ticker. <a class="ld-inl" href="' + ctx.href("stocks") + '">Search the screener</a> instead.</p></div></div></div>';
      return;
    }
    document.title = tk + ", " + S.name[i] + ", Apterreon";
    var f = tickerFile(tk);
    Promise.all([
      have("company", tk) ? getJSON("company/" + f) : null,
      have("thesis", tk) ? getJSON("thesis/" + f) : null,
      have("prices", tk) ? getJSON("prices/" + f) : null,
      have("news", tk) ? getJSON("news/" + f) : null,
    ]).then(function (got) {
      if (parseCompanyHash().tk !== route.tk && route.tk) return;   // the reader moved on
      drawCompany(main, tk, i, { company: got[0], thesis: got[1], prices: got[2], news: got[3] });
      if (route.thesis) setTimeout(scrollToThesis, 0);
    });
  }

  function drawCompany(main, tk, i, files) {
    var kind = S.kind[i], det = files.company, r = files.thesis;
    var chg = S.chg[i];
    var sec = S.sector[i], opN = cohortN();
    var pdate = S.price_date[i] || PRICE_DATE;

    var capTxt = na(i, "market_cap") ? "n/a, " + (KIND_WHY[kind] || kind) : capOf(i);
    var head = '<div class="ld-co-head"><div><div class="ld-kicker"><a class="ld-inl" href="' + ctx.href("stocks") + '">Screener</a> ' + MID + " " + esc(sectorName(S.sector[i])) + (S.sub[i] ? " " + MID + " " + esc(S.sub[i]) : "") + "</div>" +
      "<h1>" + esc(S.name[i]) + "</h1>" +
      '<div><span class="ld-co-tk">' + esc(tk) + "</span>" + (kind !== "operating" ? '<span class="ld-kt ld-kt-' + kind + (isNonOp(kind) ? " ld-kt-nonop" : "") + '">' + esc(ctx.KIND_LABEL[kind] || kind) + "</span>" : "") +
      ' <span class="ld-kicker" style="margin-left:8px">' + esc(S.index[i]) + "</span></div>" +
      (S.full_name[i] && S.full_name[i] !== S.name[i] ? '<div class="ld-muted" style="font-size:13px;margin-top:6px">Exchange name: ' + esc(S.full_name[i]) + "</div>" : "") +
      "</div>" +
      '<div class="ld-px"><div class="ld-kicker">' + (S.price[i] == null ? "No current close" : closeLabel(pdate)) + '</div><div class="p" style="margin-top:6px">' + money(S.price[i]) + "</div>" +
      '<div class="c ' + chgCls(chg) + '">' + chgTxt(chg) + " that session</div>" +
      '<div class="cap">Market cap ' + esc(capTxt) + "</div></div></div>";

    // chart: every stored daily close
    var pts = [], chartNote = "";
    var closes = files.prices && files.prices.closes || [];
    closes.forEach(function (c) { if (c && c.length >= 2 && c[1] != null && isFinite(c[1])) pts.push({ d: c[0], v: +c[1] }); });
    if (pts.length > 5) chartNote = "Daily closes, " + dateMid(pts[0].d) + " to " + dateMid(pts[pts.length - 1].d);
    else pts = [];
    var perf = pts.length ? (pts[pts.length - 1].v / pts[0].v - 1) * 100 : null;

    var dims = [["Growth", S.g[i]], ["Value", S.v[i]], ["Quality", S.q[i]], ["Momentum", S.mom[i]]];
    var facts = '<ul class="ld-facts">' +
      '<li><span class="k">Composite score</span><span class="v">' + (S.score[i] == null ? "n/a, not scorable" : signed(S.score[i], 2)) + "</span></li>" +
      dims.map(function (d) {
        var v = d[1];
        var bar = v == null ? "" : '<span class="ld-dimbar" aria-hidden="true"><i style="' + (v >= 0 ? "left:50%;width:" : "right:50%;width:") + Math.max(2, Math.min(50, Math.abs(v) * 50)).toFixed(1) + '%"></i></span>';
        return '<li><span class="k">' + d[0] + '</span><span class="v">' + bar + (v == null ? "n/a" : signed(v, 2)) + "</span></li>";
      }).join("") +
      '<li><span class="k">Next earnings</span><span class="v">' + (S.earn[i] ? dateMid(S.earn[i]) : "not scheduled") + "</span></li>" +
      '<li><span class="k">Security type</span><span class="v">' + esc(ctx.KIND_LABEL[kind] || kind) + "</span></li>" +
      (perf != null ? '<li><span class="k">Change over chart</span><span class="v ' + chgCls(perf) + '">' + signed(perf, 1) + "%</span></li>" : "") +
      "</ul>" +
      '<p class="ld-scale">' + (sec
        ? "<b>Two yardsticks on this page.</b> These scores are sector-relative: each is a z within " + esc(sec) + ", so +1 is one sigma better than a typical " + esc(sec) + " company. The strips further down are universe-relative, against all " + int(opN) + " operating companies, so a score here and a strip there can point different ways."
        : "This listing has no GICS sector, so it has no sector-relative scores. The strips further down place it against all " + int(opN) + " operating companies.") +
      " Each bar runs from 0 at the centre to " + String.fromCharCode(177) + "1 at its ends.</p>";

    var nonop = isNonOp(kind);
    var near = nearestProfiles(i), nearest = near.list;

    main.innerHTML = '<div class="ld-wrap">' + head +
      (nonop ? '<p class="ld-note" style="margin-top:16px">' + esc(ctx.KIND_LABEL[kind] || kind) + ": this listing is left out of the operating cohort, so it never moves a median. Its z-scores are still measured against operating companies, and fields that do not apply to a " + esc(KIND_WHY[kind] || kind) + " read n/a with the reason.</p>" : "") +
      '<div class="ld-co-top"><section aria-labelledby="ld-ch-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-ch-h">Price</h2><span class="ld-kicker">' + esc(chartNote || "no price history") + "</span></div>" +
      (pts.length ? '<div class="ld-chart" id="ld-chart"></div>' : '<p class="ld-note">No price history is held for ' + esc(tk) + "." + (S.price[i] != null ? " The panel has a single close of " + money(S.price[i]) + (pdate ? " on " + dateMid(pdate) : "") + "." : "") + "</p>") +
      '</section><section aria-labelledby="ld-gl-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-gl-h">At a glance</h2><span class="ld-vs">' + (sec ? "vs " + esc(sec) + " peers" : "no sector peers") + "</span></div>" + facts + "</section></div>" +
      thesisSection(tk, r) +
      '<section class="ld-sec" id="ld-where" aria-labelledby="ld-wh-h"></section>' +
      '<div class="ld-two"><section class="ld-sec" aria-labelledby="ld-np-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-np-h">Closest profiles in the universe</h2></div>' +
      '<p style="font-size:14px;color:var(--ink2);margin:10px 0 6px">Operating companies whose z-scores sit nearest to ' + esc(tk) + "’s (root mean square distance, in sigma), " +
      profileScopeTxt(near.keys, kind) + "</p>" +
      (nearest.length ? '<ul class="ld-peers">' + nearest.map(function (p) {
        return '<li><a class="ld-tk" href="' + ctx.href("company", S.ticker[p.i]) + '">' + esc(S.ticker[p.i]) + '</a><span class="n">' + esc(S.name[p.i]) + " " + MID + " " + esc(sectorName(S.sector[p.i])) + '</span><span class="d">' + num(p.d, 2) + SIGMA + "</span></li>";
      }).join("") + "</ul>" : '<p class="ld-note">Too few metrics have values to compare this listing with others.</p>') +
      "</section>" + businessBlock(tk, det) + "</div>" +
      companyDetail(tk, det, files.news) + "</div>";

    if (pts.length) priceChart(document.getElementById("ld-chart"), pts, chartNote);
    main.querySelectorAll("[data-prog]").forEach(function (h) { if (r) progressLine(h, r); });
    drawWhere(i);
  }

  /* Nearest operating profiles by z distance, using only the fields that apply to this listing (a debt
     listing's issuer-level fields are withheld). A candidate's own withheld fields are absent, so they
     drop out pair by pair. */
  function profileKeys(i) {
    return A.metrics.map(function (m) { return m.key; }).filter(function (k) { return !na(i, k); });
  }
  function profileScopeTxt(keys, kind) {
    if (keys.length === A.metrics.length) return "across every metric both report.";
    var why = KIND_WHY[kind] || kind;
    var left = A.metrics.filter(function (m) { return keys.indexOf(m.key) < 0; });
    function labs(ms) { return esc(ms.map(function (m) { return m.label; }).join(", ")); }
    if (keys.length <= left.length) return "compared only on the " + keys.length + " of " + A.metrics.length + " metrics that apply to a " + esc(why) + ": " + labs(keys.map(metric)) + ".";
    return "leaving out " + labs(left) + ", which do not apply to a " + esc(why) + ".";
  }
  function nearestProfiles(i) {
    var U = ctx.z("universe"), keys = profileKeys(i);
    var need = Math.max(4, keys.length * 0.5);
    var own = keys.map(function (k) { return U.z[k][i]; });
    var out = [];
    for (var j = 0; j < N; j++) {
      if (j === i || !U.cohortMask[j]) continue;
      var s2 = 0, n = 0;
      for (var q = 0; q < keys.length; q++) {
        var a = own[q];
        if (isNaN(a)) continue;
        var b = U.z[keys[q]][j];
        if (isNaN(b)) continue;
        s2 += (a - b) * (a - b); n++;
      }
      if (n >= need) out.push({ i: j, d: Math.sqrt(s2 / n), n: n });
    }
    out.sort(function (a, b) { return a.d - b.d; });
    return { list: out.slice(0, 8), keys: keys };
  }

  function sectorMedians(i) {
    // Median of each metric within the company's sector (operating cohort), in transformed units.
    var U = ctx.z("universe"), sec = S.sector[i], out = {};
    if (!sec) return out;
    var rows = [];
    for (var j = 0; j < N; j++) if (U.cohortMask[j] && S.sector[j] === sec) rows.push(j);
    A.metrics.forEach(function (m) {
      var raw = U.raw[m.key], vals = [];
      rows.forEach(function (j) { if (!isNaN(raw[j])) vals.push(raw[j]); });
      out[m.key] = vals.length >= (CFG.minCohort || 20) ? median(vals) : NaN;
    });
    out.__n = rows.length;
    return out;
  }

  var WL = -4, WH = 4;
  function pctPos(z) { return ((Math.max(WL, Math.min(WH, z)) - WL) / (WH - WL) * 100); }

  function drawWhere(i) {
    var host = document.getElementById("ld-where");
    if (!host) return;
    var U = ctx.z("universe"), sector = S.sector[i];
    var noSector = !sector;
    var useSector = cst.scope === "sector" && !noSector;
    var R = useSector ? ctx.z("sector") : U;
    var secMed = useSector ? null : sectorMedians(i);
    var groups = metricsByGroup();
    var minC = CFG.minCohort || 20;
    var axis = '<div class="ld-wax" aria-hidden="true">' + [-4, -3, -2, -1, 0, 1, 2, 3, 4].map(function (z) {
      return '<span style="left:' + pctPos(z) + '%">' + (z === 0 ? "0" : (z > 0 ? "+" : MINUS) + Math.abs(z)) + (Math.abs(z) === 4 ? SIGMA : "") + "</span>";
    }).join("") + "</div>";
    var rows = GROUPS.map(function (g) {
      return '<div class="ld-wgrp">' + esc(g) + "</div>" + groups[g].map(function (m) {
        var k = m.key, v = valueOf(i, k);
        var st0 = useSector ? (R.sectorStats[k] && R.sectorStats[k][sector]) : U.stats[k];
        var strip = '<div class="ld-strip"><span class="ax"></span>';
        if (st0 && st0.method !== "none") {
          var zf = function (x) { return (x - st0.center) / st0.scale; };
          var p05 = zf(st0.p05), p25 = zf(st0.p25), p75 = zf(st0.p75), p95 = zf(st0.p95), md = zf(st0.median);
          strip += '<span class="wh" style="left:' + pctPos(p05).toFixed(2) + "%;width:" + (pctPos(p95) - pctPos(p05)).toFixed(2) + '%"></span>';
          strip += '<span class="bx" style="left:' + pctPos(p25).toFixed(2) + "%;width:" + Math.max(0.6, pctPos(p75) - pctPos(p25)).toFixed(2) + '%"></span>';
          strip += '<span class="md" style="left:' + pctPos(md).toFixed(2) + '%"></span>';
          if (p95 > WH) strip += '<span class="cap" style="right:-2px">›</span>';
          if (p05 < WL) strip += '<span class="cap" style="left:-2px">‹</span>';
          var tickZ = NaN;
          if (!useSector && secMed && isFinite(secMed[k]) && U.stats[k] && U.stats[k].scale > 0) tickZ = (secMed[k] - U.stats[k].center) / U.stats[k].scale;
          if (useSector && U.stats[k]) tickZ = (U.stats[k].median - st0.center) / st0.scale;
          if (isFinite(tickZ)) strip += '<span class="sm" style="left:' + pctPos(tickZ).toFixed(2) + '%" title="' + (useSector ? "Universe median" : "Sector median") + '"></span>';
        }
        var z = v.blank ? NaN : R.z[k][i];
        if (isFinite(z)) strip += '<span class="dt' + (Math.abs(z) > WH ? " off" : "") + '" style="left:' + pctPos(z).toFixed(2) + '%"></span>';
        strip += "</span></div>";
        var noStats = useSector && !st0;
        return '<div class="ld-wrow"><div class="lab" title="' + esc(methodTxt(k)) + '">' + esc(m.label) + "<small>" + betterTxt(m) + "</small></div>" +
          '<div class="strip-cell" role="img" aria-label="' + esc(m.label) + ": " + (isFinite(z) ? "z " + zTxt(z) : "no z") + '">' + strip + "</div>" +
          '<div class="rv' + (v.blank ? " na" : "") + '">' + esc(v.blank ? v.why : v.txt) + "</div>" +
          '<div class="zv">' + (noStats ? '<span class="ld-muted" style="font-size:11px">sector &lt; ' + minC + "</span>" : zTxt(z)) + "</div></div>";
      }).join("");
    }).join("");
    var U0 = U.stats.market_cap;
    var secN = 0;
    if (useSector) for (var j = 0; j < N; j++) if (U.cohortMask[j] && S.sector[j] === sector) secN++;
    host.innerHTML = '<div class="ld-sec-h ld-where-h"><div><h2 class="ld-h2" id="ld-wh-h">Where it sits in the ' + (useSector ? "sector" : "universe") + "</h2>" +
      '<div class="ld-vs" style="margin-top:8px">' + (useSector ? "vs " + int(secN) + " " + esc(sector) + " operating companies" : "vs all " + int(cohortN()) + " operating companies") + "</div></div>" +
      '<div class="ld-seg-w"><div class="ld-seg" role="group" aria-label="Measure against"><button type="button" id="ld-cw-u" data-cs="universe" aria-pressed="' + !useSector + '">Universe</button><button type="button" id="ld-cw-s" data-cs="sector" aria-pressed="' + useSector + '"' + (noSector ? ' disabled aria-describedby="ld-cw-why" title="No GICS sector for this listing"' : "") + ">Sector</button></div>" +
      (noSector ? '<span class="ld-kicker" id="ld-cw-why">Sector view off: no GICS sector</span>' : "") + "</div></div>" +
      '<p style="font-size:14.5px;color:var(--ink2);margin:12px 0 0;max-width:80ch">' + (useSector
        ? "Each strip is the distribution inside " + esc(sectorName(sector)) + " (operating companies only), on a common sigma axis. The dot is " + esc(S.ticker[i]) + "’s sector z; the black tick is where the universe median falls."
        : "Each strip is the distribution across the operating companies that report the metric (about " + int(Math.round((U0 ? U0.n : 0) / 100) * 100) + " for market cap), on a common sigma axis (robust z: median and MAD). The thin line at 0 is the universe median. The dot is " + esc(S.ticker[i]) + (sector ? "; the black tick is the median of its sector, " + esc(sector) + "." : ". It has no GICS sector, so there is no sector tick.")) +
      " Arrows mark tails that run past " + String.fromCharCode(177) + "4" + SIGMA + "; a hollow dot sits past the axis. The engine clips z at " + String.fromCharCode(177) + "5" + SIGMA + ", shown as " + GE + " +5" + SIGMA + ".</p>" +
      '<div class="ld-legend"><span><i class="ld-lg-wh"></i>5th to 95th percentile</span><span><i class="ld-lg-box"></i>25th to 75th</span><span><i class="ld-lg-dot"></i>' + esc(S.ticker[i]) + "</span>" + (noSector ? "" : "<span><i class=\"ld-lg-tick\"></i>" + (useSector ? "universe median" : "sector median") + "</span>") + "</div>" +
      '<div class="ld-wrow hd"><div class="lab ld-kicker">Metric</div><div class="strip-cell">' + axis + '</div><div class="rv ld-kicker">Value</div><div class="zv ld-kicker">z</div></div>' + rows;
    host.querySelectorAll("[data-cs]").forEach(function (b) {
      b.addEventListener("click", function () {
        var s = b.getAttribute("data-cs");
        if (s === cst.scope) return;
        cst.scope = s; drawWhere(i);
        var f = document.getElementById(b.id); if (f) f.focus({ preventScroll: true });
      });
    });
  }

  function businessBlock(tk, det) {
    if (!det) return "<div></div>";
    var peer = det.peer_share, biz = det.business || {}, segs = det.segments;
    var text = biz.excerpt ? dashFree(biz.excerpt) : "";
    return '<section class="ld-sec" aria-labelledby="ld-bus-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bus-h">What it does</h2>' +
      (biz.filed ? '<span class="ld-kicker">10-K filed ' + dateMid(biz.filed) + "</span>" : "") + "</div>" +
      (text ? '<p class="ld-bus">' + esc(text.slice(0, 900)) + (text.length > 900 ? "…" : "") + "</p>" : '<p class="ld-note">No business description held.</p>') +
      (segs && segs.names && segs.names.length ? '<p style="font-size:14px;color:var(--ink2);margin:8px 0 0"><span class="ld-kicker">Segments</span> ' + esc(segs.names.map(dashFree).join(", ")) + (segs.as_of ? " (" + esc(segs.form || "filing") + " of " + dateMid(segs.as_of) + ")" : "") + "</p>" : "") +
      (peer && peer.share != null ? '<div class="ld-kicker" style="margin-top:16px">Share of sub-industry revenue</div><div class="ld-share"><i style="width:' + (peer.share * 100).toFixed(1) + '%"></i></div>' +
        '<p style="font-size:13.5px;color:var(--ink2);margin:0">' + num(peer.share * 100, 1) + "% of trailing revenue across " + peer.n + " names in " + esc(peer.group) + ", ranked " + peer.rank + " of " + peer.n + ". Revenue share is not market share: the peer group is a GICS label, not a market.</p>" : "") +
      "</section>";
  }

  function companyDetail(tk, det, news) {
    var rep = det && det.reported || {};
    var an = (rep.annual || []).filter(function (r) { return r.period === "FY" || !r.period || String(r.period).indexOf("FY") === 0; });
    var hist = "";
    if (an.length >= 2) {
      var series = [
        ["Revenue", "revenue", function (v) { return ctx.APTZ.fmt("market_cap", v); }],
        ["Diluted EPS", "eps_diluted", function (v) { return (v < 0 ? MINUS : "") + "$" + num(Math.abs(v), 2); }],
        ["Operating margin", "operating_margin", function (v) { return (v < 0 ? MINUS : "") + num(Math.abs(v) * 100, 1) + "%"; }],
        ["Free cash flow", "fcf", function (v) { return (v < 0 ? MINUS : "") + ctx.APTZ.fmt("market_cap", Math.abs(v)); }],
      ];
      var y0 = String(an[0].period_end).slice(0, 4), y1 = String(an[an.length - 1].period_end).slice(0, 4);
      hist = '<section class="ld-sec" aria-labelledby="ld-rh-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-rh-h">Reported history</h2><span class="ld-kicker">As reported, annual, ' + an.length + " years, " + y0 + " to " + y1 + "</span></div>" +
        '<div class="ld-hist4">' + series.map(function (s) {
          var vals = an.map(function (r) { return r[s[1]]; });
          var fi = vals.findIndex(function (v) { return v != null && isFinite(v); });
          var li = vals.length - 1; while (li >= 0 && (vals[li] == null || !isFinite(vals[li]))) li--;
          if (fi < 0 || li <= fi) return '<div class="ld-sm"><div class="ld-kicker">' + s[0] + '</div><p class="ld-muted" style="font-size:13px">Not reported.</p></div>';
          return '<div class="ld-sm"><div class="ld-kicker">' + s[0] + '</div><div class="v">' + s[2](vals[li]) + "</div>" + sparkSvg(vals) +
            '<div class="f"><span>' + String(an[fi].period_end).slice(0, 4) + " " + s[2](vals[fi]) + "</span><span>" + String(an[li].period_end).slice(0, 4) + "</span></div></div>";
        }).join("") + "</div>" +
        '<p class="ld-muted" style="font-size:13px;margin-top:14px">From XBRL company facts, consolidated figures only. A gap in a line is a year the figure was not tagged, not a year without it. Free cash flow is operating cash flow less capital expenditure.</p></section>';
    }
    var fl = det && det.filings || [];
    var filings = fl.length ? '<section class="ld-sec" aria-labelledby="ld-fi-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-fi-h">Filings held</h2><span class="ld-kicker">' + fl.length + " documents</span></div>" +
      '<div class="ld-tbl-wrap"><table class="ld-mini"><thead><tr><th>Filed</th><th>Form</th><th>Section</th><th class="r">Characters</th></tr></thead><tbody>' +
      fl.map(function (f) {
        return '<tr style="cursor:default"><td class="ld-num">' + esc(f.filed) + '</td><td class="ld-num">' + esc(f.form) + "</td><td>" + esc(String(f.doc_kind || "").replace(/_/g, " ")) + '</td><td class="r ld-num">' + (f.text_chars ? int(f.text_chars) : "n/a") + "</td></tr>";
      }).join("") + "</tbody></table></div></section>"
      : '<section class="ld-sec"><div class="ld-sec-h"><h2 class="ld-h2">Filings held</h2></div><p class="ld-note">' +
        (det ? "No filing text collected yet." : "Filing text and reported history are collected for " + int(CFG.have && CFG.have.company ? CFG.have.company.length : 0) + " companies so far: the research queue and the names the screen has surfaced. " + esc(tk) + " is not one of them yet.") + "</p></section>";
    var items = (Array.isArray(news) ? news : news && (news.items || news.news) || []).slice(0, 10);
    var newsHTML = items.length ? '<section class="ld-sec" aria-labelledby="ld-nw-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-nw-h">Recent headlines</h2><span class="ld-kicker">Tone: VADER, ' + MINUS + "1 to +1</span></div>" +
      '<ul class="ld-news">' + items.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).map(function (n) {
        var v = n.vader, tone = v == null ? "no score" : v > 0.05 ? "positive" : v < -0.05 ? "negative" : "neutral";
        var bar = v == null ? "" : '<span class="ld-tone" aria-hidden="true"><i class="' + (v >= 0 ? "p" : "n") + '" style="' + (v >= 0 ? "left:50%;" : "right:50%;") + "width:" + (Math.min(1, Math.abs(v)) * 50).toFixed(1) + '%"></i></span>';
        var d = n.ts ? new Date(n.ts * 1000).toISOString().slice(0, 10) : "";
        return '<li><a class="h" href="' + esc(n.link) + '" target="_blank" rel="noopener">' + esc(dashFree(cleanHead(n.title || n.h || "", n.source))) + '</a><div class="m"><span class="ld-src">' + esc(srcTxt(n.source)) + (d ? " " + MID + " " + dateShort(d) : "") + "</span>" + bar +
          '<span class="ld-src" style="letter-spacing:.04em;text-transform:none">' + tone + (v == null ? "" : " " + signed(v, 2)) + "</span></div></li>";
      }).join("") + "</ul></section>" : '<section class="ld-sec"><div class="ld-sec-h"><h2 class="ld-h2">Recent headlines</h2></div><p class="ld-note">No company headlines collected in the last run.</p></section>';
    return hist + '<div class="ld-two">' + filings + newsHTML + "</div>";
  }
  /* ---------------------------------------------------------------- RESEARCH */

  function progressLine(host, r) {
    function draw() {
      var n = callNums(r), v = viewOf(r);
      var W = Math.max(280, host.clientWidth || 560), pl = 10, pr = 10, y = 40;
      var hasT = isFinite(n.t), hasW = v.open && isFinite(n.w);
      var pts = [n.e, n.l];
      if (hasT) pts.push(n.t);
      if (hasW) pts.push(n.w);
      var mn = Math.min.apply(null, pts), mx = Math.max.apply(null, pts), pad = (mx - mn) * 0.12 || mn * 0.05;
      mn -= pad; mx += pad;
      function X(p) { return pl + (W - pl - pr) * (p - mn) / (mx - mn); }
      var below = [{ x: X(n.e), t: "written " + money(n.e) }];
      if (hasT) below.push({ x: X(n.t), t: (v.open ? "target " : "ref. target ") + money(n.t) });
      if (hasW) below.push({ x: X(n.w), t: "if wrong " + money(n.w) });
      below.sort(function (a, b) { return a.x - b.x; });
      // Lay labels out by their real extent (mono face, about 0.62em per character at 10.5px), clamped
      // inside the chart, and drop a label to the next row whenever it would touch one already placed.
      var CH = 10.5 * 0.62, rows = [], lab = "";
      below.forEach(function (b) {
        var w = b.t.length * CH, cx = Math.max(w / 2 + 1, Math.min(W - w / 2 - 1, b.x)), x0 = cx - w / 2, x1 = cx + w / 2;
        var row = 0;
        while (row < rows.length && rows[row] > x0 - 10) row++;
        rows[row] = x1;
        lab += '<text x="' + cx.toFixed(1) + '" y="' + (y + 22 + row * 14) + '" text-anchor="middle">' + b.t + "</text>";
      });
      var H = y + 22 + Math.max(1, rows.length) * 14;
      var lx = X(n.l), top1 = "LAST CLOSE " + dateShort(r.last_close.date).toUpperCase(), tw = Math.max(top1.length * 9.5 * 0.62, money(n.l).length * CH);
      var lcx = Math.max(tw / 2 + 1, Math.min(W - tw / 2 - 1, lx));
      var svg = '<svg viewBox="0 0 ' + W + " " + H + '" height="' + H + '" role="img" aria-label="' + esc(r.ticker + ": " + callLine(r)) + '">' +
        '<line class="ax" x1="' + pl + '" x2="' + (W - pr) + '" y1="' + y + '" y2="' + y + '"/>' +
        '<line class="mv" x1="' + X(n.e).toFixed(1) + '" x2="' + lx.toFixed(1) + '" y1="' + y + '" y2="' + y + '"/>' +
        (hasT ? '<line class="' + (v.open ? "tk" : "tkr") + '" x1="' + X(n.t).toFixed(1) + '" x2="' + X(n.t).toFixed(1) + '" y1="' + (y - 9) + '" y2="' + (y + 9) + '"/>' : "") +
        (hasW ? '<line class="tkw" x1="' + X(n.w).toFixed(1) + '" x2="' + X(n.w).toFixed(1) + '" y1="' + (y - 9) + '" y2="' + (y + 9) + '"/>' : "") +
        '<circle class="en" cx="' + X(n.e).toFixed(1) + '" cy="' + y + '" r="5"/>' +
        '<circle class="lc" cx="' + lx.toFixed(1) + '" cy="' + y + '" r="5.5"/>' +
        '<text class="k" x="' + lcx.toFixed(1) + '" y="' + (y - 22) + '" text-anchor="middle">' + top1 + "</text>" +
        '<text x="' + lcx.toFixed(1) + '" y="' + (y - 10) + '" text-anchor="middle">' + money(n.l) + "</text>" + lab + "</svg>";
      host.innerHTML = svg;
    }
    var n0 = callNums(r);
    if (!r.last_close || !isFinite(n0.l) || !isFinite(n0.e)) { host.innerHTML = '<p class="ld-note">No close held since the note was written, so there is no move to draw.</p>'; return; }
    draw();
    redrawers.push(draw);
  }

  function cleanHtml(h) {
    return dashFree(String(h || "")).replace(/<script[\s\S]*?<\/script>/gi, "").replace(/\son\w+="[^"]*"/gi, "");
  }
  function noteBody(note) {
    return (note.sections || []).map(function (s) { return "<h5>" + esc(dashFree(s.title)) + "</h5>" + cleanHtml(s.html); }).join("");
  }
  function todayISO() { return ASOF || new Date().toISOString().slice(0, 10); }

  /* The full thesis, placed on the company page. r is docs/thesis/TICKER.json: the current view's scalars at
     the top, every note newest first in notes[], and last_close. Earlier notes stay readable below. */
  function thesisSection(tk, r) {
    if (!r) return '<p class="ld-nothesis" id="ld-thesis">No thesis written for ' + esc(tk) + " yet.</p>";
    var v = viewOf(r), n = callNums(r), note = r.notes && r.notes[0] || {};
    var conv = parseInt(r.conviction, 10) || 0;
    var days = r.review_by ? daysBetween(todayISO(), r.review_by) : NaN;
    var lc = r.last_close;
    var facts = '<ul class="ld-facts">' +
      '<li><span class="k">Price when written</span><span class="v">' + money(n.e) + (r.written_on ? " on " + dateMid(r.written_on) : "") + "</span></li>" +
      (lc && isFinite(n.l) ? '<li><span class="k">Last close</span><span class="v">' + money(n.l) + " on " + dateMid(lc.date) + (isFinite(n.move) ? ' <span class="' + chgCls(n.move * 100) + '">(' + signed(n.move * 100, 1) + "%)</span>" : "") + "</span></li>" : '<li><span class="k">Last close</span><span class="v">none held</span></li>') +
      (v.open && isFinite(n.t) ? '<li><span class="k">Target</span><span class="v">' + money(n.t) + (isFinite(n.need) ? " (" + signed(n.need * 100, 1) + "% from written" + (isFinite(n.l) ? ", " + signed((n.t / n.l - 1) * 100, 1) + "% from last close" : "") + ")" : "") + "</span></li>"
        : isFinite(n.t) ? '<li><span class="k">Reference target</span><span class="v">' + money(n.t) + " (not in force while the view is " + esc(v.label.toLowerCase()) + ")</span></li>" : '<li><span class="k">Target</span><span class="v">none</span></li>') +
      (v.open && isFinite(n.w) ? '<li><span class="k">If the view is wrong</span><span class="v">about ' + money(n.w) + "</span></li>" : "") +
      (r.review_by ? '<li><span class="k">Look again by</span><span class="v">' + dateMid(r.review_by) + " (" + (days < 0 ? "due, " + -days + " days ago" : days + " days from " + dateShort(todayISO())) + ")</span></li>" : "") +
      (r.horizon_days ? '<li><span class="k">Horizon</span><span class="v">' + esc(r.horizon_days) + " trading days</span></li>" : "") + "</ul>";
    var conds = (note.conditions || []).map(function (c) { return "<li>" + esc(dashFree(c.text || c)) + "</li>"; }).join("");
    var hist = (r.history || []).map(function (h) {
      return '<li><div class="d">' + (h.date ? dateMid(h.date) : "") + " " + MID + " " + esc(h.kind) + "</div>" + esc(VIEW[h.direction] || h.direction) + ", conviction " + esc(h.conviction) + " of 5" + (h.target_price ? ", target " + money(parseFloat(h.target_price)) : "") +
        '<div class="ld-muted" style="font-size:13px">' + esc(dashFree(h.trigger || "")) + "</div></li>";
    }).join("");
    var secs = noteBody(note);
    var older = (r.notes || []).slice(1).map(function (o) {
      var body = noteBody(o);
      return '<details class="ld-det"><summary>The ' + esc(o.kind || "note") + " of " + (o.date ? dateMid(o.date) : "an earlier date") + ": " + esc(VIEW[o.direction] || o.direction || "") + (o.conviction != null ? ", conviction " + esc(o.conviction) + " of 5" : "") + "</summary>" +
        '<div class="ld-notebody">' + (o.key_claim ? '<p class="ld-claim" style="font-size:18px">' + esc(dashFree(o.key_claim)) + "</p>" : "") + (body || '<p class="ld-muted">The body of this note was not published.</p>') + "</div></details>";
    }).join("");
    var src = r.note_path && CFG.repoUrl ? ' <a class="ld-inl" href="' + esc(CFG.repoUrl + r.note_path) + '" target="_blank" rel="noopener">source note</a>' : "";
    return '<section class="ld-sec ld-thesis" id="ld-thesis" aria-labelledby="ld-th-h">' +
      '<div class="ld-sec-h"><h2 class="ld-h2" id="ld-th-h">Investment thesis</h2><span class="ld-kicker">' + esc(r.kind || "note") + (r.written_on ? " " + MID + " written " + dateMid(r.written_on) : "") + " " + MID + " " + v.status + (days < 0 && v.open ? " " + MID + " review due" : "") + "</span></div>" +
      '<div class="ld-call-view' + (v.open ? " ld-view open" : "") + '">' + esc(v.label) + "</div>" +
      '<div><span class="ld-conv" aria-hidden="true">' + [1, 2, 3, 4, 5].map(function (k) { return '<i class="' + (k <= conv ? "on" : "") + '"></i>'; }).join("") + '</span><span class="ld-kicker">Conviction ' + conv + " of 5</span></div>" +
      (r.key_claim ? '<p class="ld-claim">' + esc(dashFree(r.key_claim)) + "</p>" : "") +
      '<div class="ld-call-grid"><div>' +
      '<div class="ld-kicker" style="margin-top:4px">' + (v.open ? "From the written price toward the target" : "Price since the note was written, against the reference target") + "</div>" +
      '<div class="ld-prog" data-prog="' + esc(r.ticker || tk) + '"></div>' +
      '<p style="font-size:14px;color:var(--ink2);margin:6px 0 0">' + esc(cap1(callLine(r))) + ".</p>" +
      '<div class="ld-rblock">' + (r.falsifier ? "<h4>What would prove it wrong</h4><p>" + esc(dashFree(r.falsifier)) + "</p>" : "") +
      (conds ? "<h4>What has to stay true</h4><ul>" + conds + "</ul>" : "") +
      (note.add_if ? "<h4>What would make the view stronger</h4><p>" + esc(dashFree(note.add_if)) + "</p>" : "") +
      (note.since_last_note ? "<h4>Since the last note</h4><p>" + esc(cap1(dashFree(note.since_last_note))) + "</p>" : "") +
      "</div></div>" +
      "<div>" + facts + (hist ? '<div class="ld-rblock"><h4>History of the call</h4><ul class="ld-hist-l">' + hist + "</ul></div>" : "") + "</div></div>" +
      ((r.data_caveats || []).length ? '<details class="ld-det"><summary>What the data cannot say (' + r.data_caveats.length + " caveats)</summary><div class=\"ld-rblock\"><ul>" + r.data_caveats.map(function (c) { return "<li>" + esc(dashFree(c)) + "</li>"; }).join("") + "</ul></div></details>" : "") +
      (secs ? '<details class="ld-det"><summary>Read the full note (' + (note.sections || []).length + " sections)</summary><div class=\"ld-notebody\">" + secs + "</div></details>" : "") +
      older +
      (src ? '<p class="ld-muted" style="font-size:13px;margin:12px 0 0">' + (r.note_count > 1 ? r.note_count + " notes on " + esc(tk) + ". " : "") + "The" + src + " is kept in the repository.</p>" : "") +
      "</section>";
  }

  /* Research: an index of theses. Each row leads to the thesis on its company page. CFG.research comes from
     _research_views, the same source as the thesis files; CFG.record is the track record. */
  var STATUS_WORD = CFG.statusWords || { open: "Open call", watching: "Watching", graded: "Checked", due: "Review due" };
  function renderResearch(main) {
    var calls = (CFG.research || []).slice().sort(function (a, b) {
      var va = viewOf(a).open ? 0 : 1, vb = viewOf(b).open ? 0 : 1;
      return va - vb || ((b.written_on || "") > (a.written_on || "") ? 1 : (b.written_on || "") < (a.written_on || "") ? -1 : 0);
    });
    var closeDates = calls.filter(function (r) { return r.last_close; }).map(function (r) { return r.last_close.date; }).filter(function (d, k, a) { return a.indexOf(d) === k; });
    var rows = calls.map(function (r) {
      var v = viewOf(r), n = callNums(r), conv = parseInt(r.conviction, 10) || 0;
      var toT = isFinite(n.t) && isFinite(n.l) ? (n.t / n.l - 1) * 100 : NaN;
      return '<tr data-t="' + esc(r.ticker) + '"><td><a class="ld-tk" href="' + ctx.href("company", r.ticker) + '/thesis">' + esc(r.ticker) + "</a></td>" +
        "<td>" + esc(r.name || "") + "</td>" +
        '<td><span class="ld-view' + (v.open ? " open" : "") + '">' + esc(v.label) + '</span><div class="ref">' + esc(STATUS_WORD[r.status] || v.status) + "</div></td>" +
        '<td><span class="ld-conv" aria-hidden="true">' + [1, 2, 3, 4, 5].map(function (k) { return '<i class="' + (k <= conv ? "on" : "") + '"></i>'; }).join("") + '</span><span class="ld-num">' + conv + " of 5</span></td>" +
        '<td class="r ld-num">' + money(n.e) + "</td>" +
        '<td class="r ld-num">' + money(n.l) + (r.last_close ? ' <span class="ref">' + esc(dateShort(r.last_close.date)) + "</span>" : "") + "</td>" +
        '<td class="r ld-num">' + (isFinite(n.t) ? money(n.t) + (v.open ? "" : ' <span class="ref" title="Reference level only: the view is ' + esc(v.label.toLowerCase()) + '">ref.</span>') : "none") + "</td>" +
        '<td class="r ld-num">' + (isFinite(toT) ? signed(toT, 1) + "%" : "n/a") + "</td>" +
        '<td class="ld-num">' + (r.review_by ? dateMid(r.review_by) : "none") + "</td>" +
        '<td class="ld-num">' + (r.written_on ? dateMid(r.written_on) : "") + "</td></tr>";
    }).join("");
    main.innerHTML = '<div class="ld-wrap">' +
      '<div class="ld-head"><div><div class="ld-kicker"><b>Research</b> ' + MID + " " + calls.length + " " + (calls.length === 1 ? "thesis" : "theses") + (closeDates.length ? " " + MID + " last close " + esc(closeDates.map(dateShort).join(", ")) : "") + "</div>" +
      '<h1 class="ld-h1">Research</h1><p class="ld-deck">A written view on one company at a time, with a target, a date to look again and what would prove it wrong. Each full thesis lives on its company page, beside the price and the metrics it argues from.</p></div></div>' +
      '<section class="ld-sec" aria-labelledby="ld-ri-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-ri-h">Every thesis</h2><span class="ld-kicker">Open calls first</span></div>' +
      (calls.length ? '<div class="ld-tbl-wrap"><table class="ld-rtab"><thead><tr><th>Ticker</th><th>Company</th><th>View</th><th>Conviction</th><th class="r">Entry</th><th class="r">Last close</th><th class="r">Target</th><th class="r">To target</th><th>Review by</th><th>Written</th></tr></thead><tbody>' +
      rows + "</tbody></table></div>" : '<p class="ld-empty">No thesis has been written yet.</p>') +
      '<p class="ld-muted" style="font-size:13.5px;margin:12px 0 0;max-width:90ch">To target is the move still needed from the last close to the target. For a view that holds no position (keep watching, no view) the target is a reference level only. A company shows as watching when the note decided not to take a side; those notes make no call, so they are never checked.</p></section>' +
      recordHTML(CFG.record) + "</div>";
    var tb = main.querySelector(".ld-rtab tbody");
    if (tb) tb.addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      var tr = e.target.closest("tr[data-t]");
      if (tr) location.href = ctx.href("company", tr.getAttribute("data-t")) + "/thesis";
    });
  }
  /* The record: what has been said, and how much of it has been checked. Ported from the previous research
     page, which is where the owner reads it. */
  function recordHTML(rec) {
    if (!rec) return "";
    var tiles = [["Companies covered", rec.tickers], ["Open calls", rec.open], ["Watching, no call made", rec.watching], ["Calls checked", rec.graded]];
    var tiers = (rec.tiers || []).map(function (t) {
      return "<tr><td class=\"ld-num\">" + esc(t.conviction) + '</td><td class="r ld-num">' + (t.n || 0) + '</td><td class="r ld-num">' + (t.open || 0) + '</td><td class="r ld-num">' + (t.watching || 0) + '</td><td class="r ld-num">' + (t.graded || 0) + '</td><td class="r ld-muted">not yet</td><td class="r ld-muted">not yet</td></tr>';
    }).join("") || '<tr><td colspan="7" class="ld-muted">No notes yet.</td></tr>';
    var verdict = rec.scored ? "" : '<p class="ld-note">No call has reached its target date yet, so the last two columns are empty and nobody knows how good these calls are.' +
      (rec.earliest_maturity ? " The first one comes due on " + dateMid(rec.earliest_maturity) + "." : "") + " Until then this page keeps what was said and when it was said.</p>";
    return '<section class="ld-sec" aria-labelledby="ld-rec-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-rec-h">The record</h2><span class="ld-kicker">' + int(rec.notes || 0) + " notes</span></div>" +
      '<div class="ld-stats" style="grid-template-columns:repeat(4,minmax(0,1fr))">' + tiles.map(function (t) {
        return '<div class="ld-stat" style="display:block"><div class="ld-kicker">' + esc(t[0]) + '</div><div class="v">' + int(t[1] || 0) + "</div></div>";
      }).join("") + "</div>" +
      '<div class="ld-tbl-wrap"><table class="ld-rtab" style="margin-top:18px"><thead><tr><th>Confidence</th><th class="r">Companies</th><th class="r">Open</th><th class="r">Watching</th><th class="r">Checked</th><th class="r">Right</th><th class="r">Against similar companies</th></tr></thead><tbody>' + tiers + "</tbody></table></div>" +
      verdict +
      '<p class="ld-muted" style="font-size:13.5px;margin:12px 0 0;max-width:90ch">Each call is checked on its target date: was it right, and did the shares do better than similar companies over the same months. The table splits the calls by confidence, from 0 to 5, because the score is only useful if the confident calls turn out better than the cautious ones.</p></section>';
  }
  function scrollToThesis() {
    var t = document.getElementById("ld-thesis");
    if (!t || !document.getElementById("ld-th-h")) return;
    t.scrollIntoView({ block: "start" });
    var h = document.getElementById("ld-th-h");
    h.tabIndex = -1; h.focus({ preventScroll: true });
  }

  /* ---------------------------------------------------------------- boot */

  function listen(target, evt, fn) { target.addEventListener(evt, fn); }
  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { redrawers.forEach(function (f) { try { f(); } catch (e) { console.error(e); } }); }, 120);
  }
  function showError(main, e) {
    main.innerHTML = '<div class="ld-wrap"><div class="ld-head"><div><div class="ld-kicker"><b>Could not load the data</b></div>' +
      '<p class="ld-deck">' + esc(e && e.message || e) + ". Reload the page; if it persists, the last run may not have published stocks-data.json.</p></div></div></div>";
  }

  function boot() {
    ctx = makeCtx();
    rootEl = document.querySelector(".ld-root") || document.body;
    var main = document.getElementById("ld-main");
    var page = document.body.getAttribute("data-page") || "home";
    cur = { page: page, arg: null };
    var tb = document.querySelector(".ld-theme");
    if (tb) tb.addEventListener("click", toggleTheme);
    paintThemeBtn();
    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      if (mq.addEventListener) mq.addEventListener("change", paintThemeBtn);
    }
    listen(window, "resize", onResize);
    listen(document, "keydown", onKey);
    // Formatting needs the metric list even where no rows are loaded (the home page's screen of the day).
    if (window.APTZ && CFG.metrics) window.APTZ.init({ stocks: { ticker: [], sector: [], kind: [] }, vals: {}, metrics: CFG.metrics, nonop: CFG.nonop, minCohort: CFG.minCohort });
    if (!main) return;
    if (page === "stocks" || page === "company") {
      loadUniverse().then(function () {
        if (page === "stocks") renderStocks(main);
        else {
          renderCompany(main);
          listen(window, "hashchange", function () { redrawers = []; renderCompany(main); window.scrollTo(0, 0); });
        }
      }).catch(function (e) { console.error(e); showError(main, e); });
      return;
    }
    if (page === "today") renderToday(main);
    else if (page === "stories") renderStories(main);
    else if (page === "research") renderResearch(main);
    else renderHome(main);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
