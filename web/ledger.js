/* The Ledger site: every page of the published site, drawn in the browser from data the pipeline writes.
 *
 * One palette and one set of faces everywhere (web/ledger.css). Each page is a static shell from
 * lambda_function.py (masthead, footer, and a JSON blob in window.APT_PAGE with what that page needs);
 * this file draws the body into #ld-main. body[data-page] picks the page:
 *   home      the brief's top stories, the screen of the day (computed by the pipeline), the research calls
 *   today     the latest brief by section, repeats folded together
 *   stories   the story library, searchable
 *   stocks    every listing and every tracked metric in one grid, tinted by universe z, filtered by a
 *             small query language in the command bar (stocks.html#q=gm>1 pe<-0.5); its Map view places the
 *             same screen in 3D, on the four factor scores or any three metrics (stocks.html#view=map)
 *   company   company.html#TICKER: price, sector-relative scores, the full thesis when one exists, where
 *             each metric sits in the universe, closest profiles, business, history, filings, headlines
 *   research  an index of theses, each row leading to its company page, and the record
 *   portfolios one card per model portfolio, how companies are sorted into the six style boxes, and the rules
 *   book      book.html#ID: one model portfolio, its holdings, the rules book beside it, decisions and trades
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
    if (!d) return "Latest close";
    var x = dt(d);
    return "As of the close on " + DAYS[x.getUTCDay()] + " " + x.getUTCDate() + " " + MONTHS[x.getUTCMonth()] +
      (ASOF && d !== ASOF ? " (still the latest on " + dt(ASOF).getUTCDate() + " " + MONTHS[dt(ASOF).getUTCMonth()] + ")" : "");
  }

  var ctx = null, A = null, S = null, N = 0, rootEl = null;
  var cur = { page: null, arg: null };
  var redrawers = [];
  var themeHooks = [];   // run after the theme changes, for what CSS cannot repaint (canvas)
  var resizeTimer = 0, storyTimer = 0;

  /* ---------------------------------------------------------------- site context
   * What the prototype's shell provided: links between pages, memoized z, the row index, and the
   * security_type vocabulary. Links are real URLs, so every page works from a bookmark. */

  var PAGE_FILE = { home: "index.html", today: "today.html", stories: "stories.html", stocks: "stocks.html",
                    research: "research.html", company: "company.html", portfolios: "portfolios.html",
                    book: "book.html" };
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
      KIND_LABEL: kindLabels(),
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
                 price_date: [], chg_gap: [], earn: [], score: [], g: [], v: [], q: [], mom: [], ndim: [], status: [], mcap_raw: [] };
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
      cols.chg.push(r.change_gap ? null : num0(r.change_pct));
      cols.chg_gap.push(r.change_gap ? 1 : 0);
      cols.price_date.push(r.price_date || "");
      cols.earn.push(r.earnings_date || "");
      var dims = [r.g, r.v, r.q, r.m].filter(function (d) { return typeof d === "number" && isFinite(d); });
      // The composite: the mean of the dimension z-scores, when the pipeline calls the row scorable.
      cols.score.push(r.scorable && dims.length ? dims.reduce(function (a, b) { return a + b; }, 0) / dims.length : null);
      cols.g.push(num0(r.g)); cols.v.push(num0(r.v)); cols.q.push(num0(r.q)); cols.mom.push(num0(r.m));
      cols.ndim.push(dims.length);
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
  /* The site's one name for each listing type: KIND_WHY in prose ("a debt listing"), KIND_PLURAL in counts,
     and the same words capitalised as the label (kindLabels). */
  var KIND_WHY = { debt: "debt listing", structured: "trust certificate", equity_units: "unit listing", spac: "SPAC shell",
                   cef: "closed-end fund", bdc: "business development company", lp: "partnership", royalty_trust: "royalty trust" };
  var KIND_PLURAL = { operating: "companies", lp: "partnerships", bdc: "business development companies", royalty_trust: "royalty trusts",
                      spac: "SPAC shells", debt: "debt listings", structured: "trust certificates", equity_units: "unit listings",
                      cef: "closed-end funds" };
  function kindLabels() {
    var out = { operating: "Company" };
    Object.keys(KIND_WHY).forEach(function (k) { out[k] = cap1(KIND_WHY[k]); });
    return out;
  }
  /* Why a field has no figure, or why its figure is older than today: short forms of FIELD_STATUS, used as a
     table cell and as a sentence (with a full stop added) in the Stocks readout. */
  var STATUS_WHY = { awaiting_filing: "Updates at the next filing", no_coverage: "Not in source data",
                     insufficient_history: "Too little history", cohort_too_small: "Too few sector peers",
                     deferred_budget: "Due at the next update", not_meaningful: "Not meaningful",
                     source_error: "Source error", not_applicable: "Does not apply",
                     vendor_value: "From the data vendor" };
  /* A field withheld for a listing's type, as the end of a sentence: "P/E does not apply to a debt listing". */
  function naWhy(kind) { return "does not apply to a " + (KIND_WHY[kind] || kind); }
  /* A count and its noun: "1 day", "3 days", "1,521 stories". */
  function plural(c, one, many) { return int(c) + " " + (c === 1 ? one : many); }
  /* A list in prose: "a", "a and b", "a, b and c". */
  function andList(xs) { return xs.length < 2 ? xs.join("") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1]; }
  /* A metric label inside a sentence: "revenue growth", but "P/E", "ROE" and "EPS growth" keep their capitals. */
  function midLabel(label) { return /^[A-Z][a-z]/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label; }

  var GROUPS = [];
  function metricsByGroup() {
    var out = {};
    if (!GROUPS.length) A.metrics.forEach(function (m) { if (GROUPS.indexOf(m.group) < 0) GROUPS.push(m.group); });
    GROUPS.forEach(function (g) { out[g] = []; });
    A.metrics.forEach(function (m) { out[m.group].push(m); });
    return out;
  }
  function metric(k) { return ctx.APTZ.metric(k); }
  /* Which direction is better for a metric, as a sentence without its full stop. */
  function betterTxt(m) { return m.better > 0 ? "Higher is better" : m.better < 0 ? "Lower is better" : "Neither higher nor lower\u00a0is\u00a0better"; }
  /* The pipeline's own description of a field (FIELD_METHODS), for a title attribute: the note, then the formula. */
  function methodTxt(key) {
    var fm = (CFG.fieldMethods || {})[key];
    return fm ? (fm.note || "") + (fm.formula ? " Formula: " + fm.formula + "." : "") : "";
  }

  /* Value cell for a row and metric on the company page, honest about withheld or missing fields. */
  function valueOf(i, key) {
    var kind = S.kind[i];
    if (na(i, key)) return { txt: "n/a", why: "Does not apply", whyLong: naWhy(kind), blank: true };
    var v = A.vals[key][i];
    if (v == null || !isFinite(v)) {
      var st = S.status[i] && S.status[i][key];
      return { txt: "n/a", why: st ? STATUS_WHY[st] || "Not reported" : "Not reported", blank: true };
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
    themeChanged();
  }
  function themeChanged() { themeHooks.forEach(function (f) { try { f(); } catch (e) { console.error(e); } }); }
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
      sodHTML = '<section aria-labelledby="ld-sod-h"><div class="ld-panel"><div class="ld-kicker"><b>Screen of the day</b></div>' +
        '<h2 class="ld-h2" id="ld-sod-h" style="margin-top:8px">' + esc(P.name) + "</h2>" +
        '<p style="margin:6px 0 0;color:var(--ink2);font-size:15px">' + esc(P.blurb) + "</p>" +
        '<div class="ld-bands">' + keys.map(function (k) {
          var raw = bandRawTxt(k, bands[k], P.stats[k]);
          return '<span class="ld-band"><b>' + esc(metric(k).label) + "</b> " + bandTxt(bands[k]) + (raw ? ' <span class="ld-muted">(' + raw + ")</span>" : "") + "</span>";
        }).join("") + "</div>" +
        '<p style="margin:0 0 10px;font:400 20px/1.3 var(--serif)"><b style="color:var(--accent-ink);font-weight:500">' + int(P.match) + "</b> of " + int(P.of) + " operating companies " + (P.match === 1 ? "matches" : "match") + "</p>" +
        (P.hits.length ? '<div class="ld-tbl-wrap"><table class="ld-mini ld-sodt" id="ld-sod"><thead><tr><th>Company</th>' +
        keys.map(function (k) { return '<th class="r" title="' + esc(metric(k).label) + ', z-score against all companies">' + esc(SHORT_LABEL[k] || metric(k).label) + ' <span class="lc">' + SIGMA + "</span></th>"; }).join("") +
        '<th class="r sc" title="Overall score, compared with the company’s own sector">Sector score</th></tr></thead><tbody>' + screenRows + "</tbody></table></div>" : "") +
        '<p class="ld-muted" style="font-size:13px;margin:10px 0 14px">' + (!P.hits.length ? "" : P.match <= P.hits.length ? (P.match === 1 ? "The one match. " : "Every match, highest overall score first. ") : "The " + (P.hits.length === 8 ? "eight" : P.hits.length) + " with the highest overall score. ") +
        "The metric columns are z-scores: how far each figure sits from the median of all " + int(P.of) + " operating companies (not counting funds, shells and bond listings), in units of the usual spread. The score compares each company with its own sector. Data as of " + dateMid(ASOF) + ".</p>" +
        '<a class="ld-link" href="' + ctx.href("stocks", P.q) + '">Open this screen ' + ARROW + "</a></div></section>";
    }

    // Research calls
    var calls = research.map(function (r) {
      var v = viewOf(r);
      return '<li><a class="ld-tk" href="' + ctx.href("company", r.ticker) + '#thesis">' + esc(r.ticker) + "</a>" +
        '<span class="ld-muted" style="font-size:14.5px">' + esc(r.name || r.ticker) + "</span>" +
        '<span class="ld-view ' + (v.open ? "open" : "") + '">' + v.label + "</span>" +
        '<span class="meta">' + esc(callSentence(r)) + "</span></li>";
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
      '<p class="ld-deck" style="margin:0;max-width:44ch">One news brief a day in ' + plural(withStories.length, "section", "sections") + ', a library of ' + plural(T.total || 0, "story", "stories") +
      ", and a stock screener of " + int(CFG.nListings || 0) + " US listings that compares each figure with every other company’s.</p></div>" +
      (quotes ? '<div class="ld-quotes" aria-label="' + esc("Market prices from the brief" + (b.date ? " of " + dateMid(b.date) : "")) + '" style="border-top:1px solid var(--rule)">' + quotes + "</div>" : "") +
      '<div class="ld-front">' +
      '<section aria-labelledby="ld-top-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-top-h">Top of the brief</h2><a class="ld-link" href="' + ctx.href("today") + '">All ' + plural(unique, "story", "stories") + " " + ARROW + "</a></div>" +
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
      (win.last ? '<p style="margin:10px 0 0;font-size:14px;color:var(--ink2)">The words that appear most often in headlines from ' + dateShort(win.curStart) + " to " + dateShort(win.last) + ", counted once per story. In brackets, the count for the 7 days before (" + dateShort(win.prevStart) + " to " + dateShort(win.prevEnd) + ").</p>" : "") +
      '<ul class="ld-terms">' + terms.map(function (t) {
        return "<li><span>" + esc(t.w) + '</span><span class="bar"><i style="width:' + (100 * t.n / tmax).toFixed(1) + '%"></i></span><span class="n">' + t.n + " (was " + t.p + ")</span></li>";
      }).join("") + "</ul>" +
      (days.length ? '<div class="ld-kicker" style="margin-top:22px">Stories per day</div>' +
      '<div class="ld-cols" role="img" aria-label="Stories per day on the last ' + plural(days.length, "day", "days") + " with a brief, " + esc(dateMid(days[0])) + " to " + esc(dateMid(days[days.length - 1])) + '">' +
      days.map(function (d, j) {
        return '<div class="' + (j === days.length - 1 ? "last" : "") + '" style="height:' + (100 * dayCounts[d] / dmax).toFixed(1) + '%" title="' + esc(dateMid(d)) + ": " + plural(dayCounts[d], "story", "stories") + '"></div>';
      }).join("") + '</div><div class="ld-cols-x">' + days.map(function (d) { return "<span>" + dt(d).getUTCDate() + "</span>"; }).join("") + "</div>" : "") +
      '<div class="ld-stats"><div class="ld-stat"><div class="ld-kicker">Stories</div><div class="v">' + int(T.total || 0) + '</div><div class="d">on ' + plural(allDays.length, "day", "days") + " with a brief</div></div>" +
      '<div class="ld-stat"><div class="ld-kicker">Sources</div><div class="v">' + int(T.sources || 0) + '</div><div class="d">publications</div></div>' +
      '<div class="ld-stat"><div class="ld-kicker">Cadence</div><div class="v">Daily</div><div class="d">' + cadenceTxt(T.cadence) + "</div></div></div>" +
      "</section></div>" +
      '<section class="ld-sec" aria-labelledby="ld-pages-h" style="padding-top:48px"><div class="ld-sec-h" style="border-bottom:0"><h2 class="ld-h2" id="ld-pages-h">Pages on this site</h2></div>' +
      '<div class="ld-pages">' +
      pageCard("today", "Today", (b.date ? "The " + dateMid(b.date) + " brief" : "The latest brief") + ", section by section: " + plural(unique, "story", "stories") + ", each shown once.") +
      pageCard("stories", "Stories", "Search and filter all " + plural(T.total || 0, "story", "stories") + ", grouped by the day they ran.") +
      pageCard("stocks", "Stocks", int(CFG.nListings || 0) + " US listings: the S&P 500, 400 and 600, and every other stock on Nasdaq, NYSE and NYSE American. Filter on any figure.") +
      pageCard("company", "A company", "One page per company: price history, how each figure compares with all companies, filings and headlines.", CFG.sampleTicker || "AAPL") +
      pageCard("research", "Research", plural(research.length, "written thesis", "written theses") + ", each with a target price, a date to review it, and what would prove it wrong.") +
      "</div></section></div>";

    var sod = main.querySelector("#ld-sod tbody");
    if (sod) sod.addEventListener("click", rowClick);
  }
  /* Honest cadence line: when the one-a-day edition started, and what ran before it. */
  function cadenceTxt(c) {
    if (!c || !c.dailyN) return "Varies from day to day";
    var t = "One a day since " + dateShort(c.dailyFirst) + " (" + plural(c.dailyN, "brief", "briefs") + ").";
    if (c.olderN) t += " Morning, midday and evening editions from " + dateShort(c.olderFirst) + " to " + dateShort(c.olderLast) + " (" + plural(c.olderN, "story", "stories") + ").";
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
  /* A call in whole sentences, for the Home page's list and the thesis on the company page. */
  function callSentence(r) {
    var v = viewOf(r), n = callNums(r);
    var out = "Written " + (r.written_on ? "on " + dateShort(r.written_on) : "on an unrecorded date") + (isFinite(n.e) ? " at " + money(n.e) : "");
    if (v.open) out += (isFinite(n.t) ? ", with a target of " + money(n.t) + (r.review_by ? " by " + dateShort(r.review_by) : "") : ", with no target" + (r.review_by ? ", to be reviewed by " + dateShort(r.review_by) : "")) + ".";
    else out += ".";
    if (r.last_close && isFinite(n.l)) {
      out += " The last close was " + money(n.l) + " on " + dateShort(r.last_close.date);
      if (isFinite(n.move)) {
        out += Math.abs(n.move) < 0.0005 ? ", unchanged since the note" : ", " + (n.move > 0 ? "up " : "down ") + num(Math.abs(100 * n.move), 1) + "% since the note";
        var toward = n.need !== 0 ? n.move / n.need : 0;
        if (v.open && isFinite(n.t) && Math.abs(n.move) >= 0.0005) out += toward >= 0 ? ", toward the target" : ", away from the target";
      }
      out += ".";
    } else out += " There has been no close since.";
    if (!v.open) out += isFinite(n.t) ? " The " + money(n.t) + " target is for reference only and is not active while the view is “" + v.label + "”." : " There is no target.";
    return out;
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
      (secs.length ? '<p class="ld-deck">' + plural(unique, "story", "stories") + " in " + plural(secs.length, "section", "sections") + "." + (filed > unique ? " " + plural(filed - unique, "story was a repeat", "stories were repeats") + " from another section. Each story appears once, under the first section it ran in, with a note of where else it ran." : "") + "</p>"
        : '<p class="ld-deck">No brief has been published yet. This page fills in after the next scheduled run.</p>') +
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
              '<div class="m"><span class="ld-src">' + esc(x.src) + "</span>" + (x.also.length ? ' <span class="also">' + MID + " also in " + esc(andList(x.also)) + "</span>" : "") + "</div></div>";
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
      '<h1 class="ld-h1">Story library</h1><p class="ld-deck">Every story the brief has carried, ' + int(stories.length) + " in all, newest first and grouped by the day it ran. Search looks at the headline, the summary and the source.</p></div></div>" +
      '<div class="ld-filters">' +
      '<div class="row"><label for="ld-sq" class="ld-sr">Search stories</label><input class="ld-input" id="ld-sq" type="search" placeholder="Search headlines, summaries and sources" style="flex:1 1 260px;max-width:520px" value="' + esc(sst.q) + '">' +
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
    el.innerHTML = '<p class="ld-more" style="margin:14px 0 0">' + (list.length ? (list.length === 1 ? "Showing the one matching story" : shown.length === list.length ? "Showing all " + plural(list.length, "matching story", "matching stories") : "Showing " + int(shown.length) + " of " + plural(list.length, "matching story", "matching stories")) : "") + "</p>" +
      (list.length ? groups.map(function (g) {
        var tot = dayTotals[g.d], part = g.items.length < tot;
        return '<section class="ld-day"><div class="dh"><div class="d">' + dateLong(g.d) + '</div><div class="ld-kicker" style="margin-top:4px">' +
          (part ? "Showing " + g.items.length + " of " + tot + ", more below" : plural(tot, "story", "stories")) + "</div></div><div>" +
          g.items.map(function (s) {
            var meta = [srcTxt(s.src), s.sec, s.ed === "daily" ? "" : cap1(s.ed) + " edition"].filter(Boolean).map(esc).join(" " + MID + " ");
            return '<div class="ld-story"><a class="h" href="' + esc(s.link) + '" target="_blank" rel="noopener">' + esc(cleanHead(s.h, s.src)) + '</a><div class="m ld-src">' + meta + "</div></div>";
          }).join("") + "</div></section>";
      }).join("") : '<p class="ld-empty">No story matches. Try fewer words, or set the section and edition back to All.</p>') +
      (list.length > shown.length ? '<div class="ld-more"><button type="button" class="ld-btn" id="ld-smore">Show ' + Math.min(150, list.length - shown.length) + " more</button><span>" + int(list.length - shown.length) + " more to show</span></div>" : "");
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
  /* A number with a unit is a raw value, not a z: % is a percent (roe>=15% is 0.15), x a multiple, k m b t
     scale it (mcap>=10000m is $10B) and r takes the stored value as it is (beta<=1.2r). */
  var UNITRE = "(%|x|k|m|b|t|r)";
  var UNIT_MUL = { "%": 0.01, x: 1, k: 1e3, m: 1e6, b: 1e9, t: 1e12, r: 1 };
  var RAW_CMP = new RegExp("^([a-z0-9_]+)(>=|<=|>|<)" + NUMRE + UNITRE + "$");
  var RAW_RANGE = new RegExp("^([a-z0-9_]+)[:=]" + NUMRE + UNITRE + "?\\.\\." + NUMRE + UNITRE + "$");
  var DP_DIMS = ["Growth", "Value", "Momentum", "Quality"];
  var DP_WORD = { growth: "Growth", value: "Value", momentum: "Momentum", mom: "Momentum", quality: "Quality" };
  var RESEARCH_ORDER = ["thesis", "watchlist", "long", "short", "avoid", "watch"];
  var RESEARCH_WORD = { thesis: "Has a thesis", watchlist: "Watchlist", long: VIEW.long, short: VIEW.short, avoid: VIEW.avoid, watch: VIEW.watch };
  var RESEARCH = CFG.research || { thesis: {}, watchlist: [] };
  var WATCH = {};
  (RESEARCH.watchlist || []).forEach(function (t) { WATCH[t] = 1; });

  function normQuery(q) {
    return (q || "").replace(/−/g, "-").replace(/σ/g, "").replace(/≥/g, ">=").replace(/≤/g, "<=")
      .replace(/\s*(>=|<=|>|<)\s*/g, "$1").replace(/\s*\.\.\s*/g, "..");
  }
  function tokenize(q) { var t = normQuery(q).trim(); return t ? t.split(/\s+/) : []; }

  function parseQuery(q) {
    var toks = tokenize(q);
    var p = { bands: {}, raw: {}, sectors: [], idx: [], include: {}, hideKinds: {}, text: [], scope: "universe", chips: [],
              research: "", dp: {}, needCap: false };
    var sectors = Object.keys(SECTOR_SHORT);
    toks.forEach(function (raw, ti) {
      var t = raw.toLowerCase(), m, key;
      if ((m = t.match(RAW_RANGE))) {
        key = A2K[m[1]];
        if (!key) return p.chips.push({ ti: ti, err: 1, html: "no metric called <b>" + esc(m[1]) + "</b>; press ? for the list" });
        var r0 = +m[2] * UNIT_MUL[m[3] || m[5]], r1 = +m[4] * UNIT_MUL[m[5]];
        addRaw(p, key, Math.min(r0, r1), Math.max(r0, r1), ti);
        return;
      }
      if ((m = t.match(RAW_CMP))) {
        key = A2K[m[1]];
        if (!key) return p.chips.push({ ti: ti, err: 1, html: "no metric called <b>" + esc(m[1]) + "</b>; press ? for the list" });
        var rv = +m[3] * UNIT_MUL[m[4]];
        if (m[2].charAt(0) === ">") addRaw(p, key, rv, Infinity, ti); else addRaw(p, key, -Infinity, rv, ti);
        return;
      }
      if ((m = t.match(RANGE))) {
        key = A2K[m[1]];
        if (!key) return p.chips.push({ ti: ti, err: 1, html: "no metric called <b>" + esc(m[1]) + "</b>; press ? for the list" });
        addBand(p, key, Math.min(+m[2], +m[3]), Math.max(+m[2], +m[3]), ti);
        return;
      }
      if ((m = t.match(CMP))) {
        key = A2K[m[1]];
        if (!key) return p.chips.push({ ti: ti, err: 1, html: "no metric called <b>" + esc(m[1]) + "</b>; press ? for the list" });
        var v = +m[3];
        if (m[2] === ">" || m[2] === ">=") addBand(p, key, v, Infinity, ti);
        else if (m[2] === "<" || m[2] === "<=") addBand(p, key, -Infinity, v, ti);
        else addBand(p, key, v - 0.25, v + 0.25, ti);
        return;
      }
      if (t.charAt(0) === "+" || t.charAt(0) === "-") {
        var k = t.slice(1), kinds = KIND_SWITCH[k];
        if (!kinds) return p.chips.push({ ti: ti, err: 1, html: "no listing type called <b>" + esc(raw) + "</b>" });
        kinds.forEach(function (kd) { if (t.charAt(0) === "+") p.include[kd] = 1; else p.hideKinds[kd] = 1; });
        p.chips.push({ ti: ti, inc: 1, html: '<span class="cl">' + (t.charAt(0) === "+" ? "show" : "hide") + "</span> <b>" +
          andList(kinds.map(function (kd) { return KIND_PLURAL[kd]; })) + "</b>" });
        return;
      }
      if ((m = t.match(/^(sector|sec|s):(.+)$/))) {
        var val = m[2].replace(/[^a-z0-9]/g, ""), hits = [];
        if (val === "none" || val === "unclassified") hits = [""];
        else if (SECTOR_WORDS[val]) hits = [SECTOR_WORDS[val]];
        else sectors.forEach(function (s) { if (s && s.toLowerCase().replace(/[^a-z0-9]/g, "").indexOf(val) >= 0) hits.push(s); });
        if (!hits.length) return p.chips.push({ ti: ti, err: 1, html: "no sector name contains <b>" + esc(m[2]) + "</b>" });
        hits.forEach(function (h) { if (p.sectors.indexOf(h) < 0) p.sectors.push(h); });
        p.chips.push({ ti: ti, html: '<span class="cl">sector</span> <b>' + esc(hits.map(function (h) { return SECTOR_SHORT[h]; }).join(" or ")) + "</b>" });
        return;
      }
      if ((m = t.match(/^(idx|index):(.+)$/))) {
        var iv = m[2].replace(/[^a-z0-9]/g, "");
        var list = iv === "sp1500" ? ["S&P 500", "S&P 400", "S&P 600"] : IDX_TOK[iv] ? [IDX_TOK[iv]] : null;
        if (!list) return p.chips.push({ ti: ti, err: 1, html: "no index or exchange called <b>" + esc(m[2]) + "</b>" });
        list.forEach(function (x) { if (p.idx.indexOf(x) < 0) p.idx.push(x); });
        p.chips.push({ ti: ti, html: '<span class="cl">index</span> <b>' + esc(list.join(" or ")) + "</b>" });
        return;
      }
      if ((m = t.match(/^scope:(sector|universe|sec|uni|u|s)$/))) {
        p.scope = m[1].charAt(0) === "s" ? "sector" : "universe";
        p.chips.push({ ti: ti, html: '<span class="cl">scope</span> <b>' + (p.scope === "sector" ? "within each sector" : "against all companies") + "</b>" });
        return;
      }
      if ((m = t.match(/^research:([a-z]+)$/))) {
        var rw = m[1] === "any" ? "thesis" : m[1] === "wl" ? "watchlist" : m[1];
        if (!RESEARCH_WORD[rw]) return p.chips.push({ ti: ti, err: 1, html: "no research filter called <b>" + esc(m[1]) + "</b>" });
        p.research = rw;
        p.chips.push({ ti: ti, html: '<span class="cl">research</span> <b>' + esc(RESEARCH_WORD[rw]) + "</b>" });
        return;
      }
      if ((m = t.match(/^dp:([a-z]+)(>=|>|=)?(\d+)$/))) {
        var dim = DP_WORD[m[1]];
        if (!dim) return p.chips.push({ ti: ti, err: 1, html: "no factor called <b>" + esc(m[1]) + "</b>" });
        var need = +m[3] + (m[2] === ">" ? 1 : 0);
        p.dp[dim] = Math.max(p.dp[dim] || 0, need);
        p.chips.push({ ti: ti, html: '<span class="cl">figures</span> <b>' + dim + "</b> " + GE + " " + need + " of " + (CFG.dims && CFG.dims[dim] ? CFG.dims[dim].length : 5) });
        return;
      }
      if (t === "has:cap") {
        p.needCap = true;
        p.chips.push({ ti: ti, html: '<span class="cl">require</span> <b>market cap</b>' });
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
  function addRaw(p, key, lo, hi, ti) {
    var b = p.raw[key];
    if (b) { b[0] = Math.max(b[0], lo); b[1] = Math.min(b[1], hi); } else p.raw[key] = [lo, hi];
    function f(v) { return ctx.APTZ.fmt(key, v); }
    var txt = lo === -Infinity ? LE + " " + f(hi) : hi === Infinity ? GE + " " + f(lo) : f(lo) + " to " + f(hi);
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
    var secCount = {}, idxCount = {}, resCount = {}, th = RESEARCH.thesis || {}, dpn = dpCounts();
    var dpKeys = Object.keys(p.dp).filter(function (d) { return p.dp[d] > 0; });
    if (p.sectors.length) { secSet = {}; p.sectors.forEach(function (s) { secSet[s] = 1; }); }
    if (p.idx.length) { idxSet = {}; p.idx.forEach(function (s) { idxSet[s] = 1; }); }
    for (var i = 0; i < N; i++) {
      var k = S.kind[i];
      if (hidden[k]) { hiddenCount[k] = (hiddenCount[k] || 0) + 1; continue; }
      universeN++;
      if (nonopKind(k)) incCount++;
      // What the rail counts beside each option: the listings shown, before any other filter.
      secCount[S.sector[i]] = (secCount[S.sector[i]] || 0) + 1;
      idxCount[S.index[i]] = (idxCount[S.index[i]] || 0) + 1;
      var tki = S.ticker[i];
      if (Object.prototype.hasOwnProperty.call(th, tki)) {
        resCount.thesis = (resCount.thesis || 0) + 1;
        resCount[th[tki]] = (resCount[th[tki]] || 0) + 1;
      }
      if (WATCH[tki]) resCount.watchlist = (resCount.watchlist || 0) + 1;
      if (secSet && !secSet[S.sector[i]]) continue;
      if (idxSet && !idxSet[S.index[i]]) continue;
      if (p.needCap && S.mcap_raw[i] == null) continue;
      if (p.research && !researchHas(p.research, tki)) continue;
      if (dpKeys.length) {
        var enough = true;
        for (var d = 0; d < dpKeys.length; d++) if (dpn[dpKeys[d]][i] < p.dp[dpKeys[d]]) { enough = false; break; }
        if (!enough) continue;
      }
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
    /* Rows a band cannot judge because the banded field does not apply to their type (withheld by the
       pipeline, status not_applicable). Counted so the page can say what it left out. */
    var rawKeys = Object.keys(p.raw);
    var keys = Object.keys(p.bands).concat(rawKeys.filter(function (rk) { return !p.bands[rk]; }));
    var drop = [], dropKinds = {}, dropKeys = {};
    if (keys.length) for (j = 0; j < base.length; j++) {
      var ii = base[j], kd = S.kind[ii], hit = false;
      for (var q = 0; q < keys.length; q++) if (na(ii, keys[q])) { dropKeys[keys[q]] = 1; hit = true; }
      if (hit) { drop.push(ii); dropKinds[kd] = (dropKinds[kd] || 0) + 1; }
    }
    // Raw bands compare the stored value; a row with none does not pass, as with a z band.
    if (rawKeys.length) base = base.filter(function (ri) {
      for (var rk = 0; rk < rawKeys.length; rk++) {
        var v = A.vals[rawKeys[rk]][ri], b = p.raw[rawKeys[rk]];
        if (v == null || v < b[0] || v > b[1]) return false;
      }
      return true;
    });
    var baseIdx = Int32Array.from(base);
    var match = ctx.APTZ.filter(res, p.bands, { idx: baseIdx });
    var order = Array.from(match);
    gridSort(order, res);
    screen = { p: p, res: res, match: match, order: order, universeN: universeN, hidden: hidden, hiddenCount: hiddenCount,
               incCount: incCount, dropN: drop.length, dropKinds: dropKinds, dropKeys: Object.keys(dropKeys),
               secCount: secCount, idxCount: idxCount, resCount: resCount };
    return screen;
  }

  function researchHas(r, tk) {
    var th = RESEARCH.thesis || {};
    if (r === "watchlist") return !!WATCH[tk];
    if (!Object.prototype.hasOwnProperty.call(th, tk)) return false;
    return r === "thesis" || th[tk] === r;
  }
  /* Data points per dimension: how many of the dimension's scored fields (SCORE_GROUPS_PY, CFG.dims) a row
     has a value for. A field withheld for the listing's type counts as missing. Once per page. */
  var DPN = null;
  function dpCounts() {
    if (DPN) return DPN;
    DPN = {};
    DP_DIMS.forEach(function (d) {
      var c = new Uint8Array(N);
      ((CFG.dims || {})[d] || []).forEach(function (f) {
        var v = A.vals[f];
        if (v) for (var i = 0; i < N; i++) if (v[i] != null) c[i]++;
      });
      DPN[d] = c;
    });
    return DPN;
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
      if (cur.page === "stocks" && railOpen()) { e.preventDefault(); openRail(false); return; }
      if (typing && cur.page === "stocks") { t.blur(); return; }
    }
    if (typing || cur.page !== "stocks") return;
    if (e.key === "?") { e.preventDefault(); toggleHelp(document.activeElement); return; }
    if (e.key === "/") {
      e.preventDefault();
      var q = document.getElementById("ld-q");
      if (railIsDrawer()) openRail(true);
      if (q) { q.focus(); q.select(); }
      return;
    }
    if (pageKeys && pageKeys(e)) e.preventDefault();
  }

  function helpHTML() {
    var groups = A.groups.map(function (g) {
      return "<h3>" + esc(g) + '</h3><div class="cols">' + M.filter(function (m) { return m.group === g; }).map(function (m) {
        return '<div title="' + esc(m.label + ". " + betterTxt(m) + ". " + methodTxt(m.key)) + '"><code>' + esc(ALIAS[m.key]) + "</code> " + esc(m.label) + "</div>";
      }).join("") + "</div>";
    }).join("");
    return '<div class="ld-help" id="ld-help" role="dialog" aria-modal="false" aria-labelledby="ld-help-h">' +
      '<button type="button" class="ld-btn x" data-close aria-label="Close help">Close</button>' +
      '<h2 id="ld-help-h">How to write filters</h2>' +
      "<p>Type filters into the box, separated by spaces. A company must pass all of them (a repeated sector or index filter allows any of those named), and a company with no figure for a filtered metric is left out. " +
      "A plain number is a z-score: how far a figure sits from the median of the " + int(cohortN()) + " operating companies (as of " + dateMid(ASOF) + "), in units of the usual spread, " + SIGMA + ". " +
      "The median and spread are measured so that a few extreme companies do not distort them, and z-scores are capped at " + String.fromCharCode(177) + "5.</p>" +
      "<dl><dt><code>gm&gt;1</code></dt><dd>gross margin z-score at or above +1" + SIGMA + " (<code>&gt;=</code> means the same)</dd>" +
      "<dt><code>pe&lt;-0.5</code></dt><dd>P/E z-score at or below " + MINUS + "0.5" + SIGMA + " (cheaper than the typical company)</dd>" +
      "<dt><code>roe:1..3</code></dt><dd>a range, both ends included</dd>" +
      "<dt><code>vol=0</code></dt><dd>within " + String.fromCharCode(177) + "0.25" + SIGMA + " of the number</dd>" +
      "<dt><code>pe&lt;=20x roe&gt;=15%</code></dt><dd>a number with a unit is the figure itself, not a z-score: % for a percent, x for a multiple, k, m, b or t for thousands, millions, billions or trillions (<code>mcap&gt;=10b</code> is $10 billion), r for the figure as stored</dd>" +
      "<dt><code>research:thesis</code></dt><dd>companies with a written thesis; also <code>watchlist</code>, or the current view: <code>long</code> (" + esc(VIEW.long) + "), <code>short</code> (" + esc(VIEW.short) + "), <code>avoid</code> (" + esc(VIEW.avoid) + "), <code>watch</code> (" + esc(VIEW.watch) + ")</dd>" +
      "<dt><code>dp:value&gt;=3</code></dt><dd>at least 3 of the 5 figures behind a factor score (growth, value, momentum or quality); <code>has:cap</code> requires a market cap</dd>" +
      "<dt><code>sector:energy</code></dt><dd>any part of a sector's name (tech, health, staples, realestate), or <code>none</code> for no sector; repeat it to allow several sectors</dd>" +
      "<dt><code>idx:sp500</code></dt><dd>an index or exchange: sp500, sp400, sp600, sp1500 (all three), nasdaq, nyse, amex (NYSE American); repeat it to allow several</dd>" +
      "<dt><code>+spac +notes +cef</code></dt><dd>show SPAC shells, bond listings (debt listings, trust certificates and unit listings) or closed-end funds, which are hidden unless you ask; <code>+nonop</code> shows all three</dd>" +
      "<dt><code>-lp -bdc -trust</code></dt><dd>hide partnerships, business development companies or royalty trusts, which are shown with a tag unless you hide them</dd>" +
      "<dt><code>scope:sector</code></dt><dd>z-scores against each company's own sector (sectors of 20 or more companies)</dd>" +
      "<dt><code>apple</code></dt><dd>any other word matches the start of a ticker or any part of a name</dd></dl>" +
      "<h3>Keys</h3><dl><dt><kbd>/</kbd></dt><dd>go to the filter box</dd><dt><kbd>j</kbd> <kbd>k</kbd></dt><dd>move the cursor down or up a row</dd>" +
      "<dt><kbd>Enter</kbd></dt><dd>open the company at the cursor</dd><dt><kbd>s</kbd></dt><dd>switch between comparing with all companies and with each sector</dd>" +
      "<dt><kbd>z</kbd></dt><dd>show figures or z-scores in the cells</dd><dt><kbd>m</kbd></dt><dd>show the next 150 rows</dd>" +
      "<dt><kbd>b</kbd></dt><dd>filter for companies within " + String.fromCharCode(177) + "0.5" + SIGMA + " of the cursor row on the sorted column (or shift-click a cell)</dd>" +
      "<dt><kbd>" + String.fromCharCode(8592, 8593, 8594, 8595) + "</kbd></dt><dd>turn the map, when it has focus; <kbd>Esc</kbd> there clears the selected companies</dd>" +
      "<dt><kbd>Esc</kbd></dt><dd>leave the filter box, or close this help</dd><dt><kbd>?</kbd></dt><dd>open or close this help</dd></dl>" +
      "<h3>Colour</h3><p>Cells are tinted by z-score: slate below the median, ochre above, plain near zero. The tint shows position, not quality: for P/E a high z-score means expensive.</p>" +
      '<h2 style="margin-top:18px">Metric names</h2><p>Hover over a name to see how it is worked out.</p>' + groups + "</div>";
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
     A bare preset id (stocks.html#qarp) opens that ready-made screen. The map adds its own parameters,
     joined with "&" (the query itself is encoded, so it never carries a bare "&"):
       view=map  the map instead of the grid     map=axes  three metrics instead of the four factors
       ax=gm,pe,r122  the three axes, by command-bar name */
  function hashDec(v) { try { return decodeURIComponent(v.replace(/\+/g, " ")); } catch (e) { return v; } }
  function hashState() {
    var h = (location.hash || "").replace(/^#/, ""), out = { q: "", view: "grid", mode: "tetra", ax: null };
    if (!h) return out;
    h.split("&").forEach(function (part, pi) {
      var eq = part.indexOf("=");
      if (eq < 0) { if (pi === 0) out.q = PRESET_Q[part] || ""; return; }
      var k = part.slice(0, eq), v = hashDec(part.slice(eq + 1));
      if (k === "q") out.q = v;
      else if (k === "view" && v === "map") out.view = "map";
      else if (k === "map" && v === "axes") out.mode = "axes";
      else if (k === "ax") {
        var ks = v.toLowerCase().split(",").map(function (a) { return A2K[a.trim()]; }).filter(Boolean);
        if (ks.length === 3) out.ax = ks;
      }
    });
    return out;
  }
  function queryFromHash() { return hashState().q; }
  function writeHash() {
    var parts = [];
    if (gst.query.trim()) parts.push("q=" + encodeURIComponent(gst.query.trim()));
    if (mst.view === "map") {
      parts.push("view=map");
      if (mst.mode === "axes") parts.push("map=axes");
      if (mst.mode === "axes" && mst.ax) parts.push("ax=" + mst.ax.map(function (k) { return ALIAS[k]; }).join(","));
    }
    var want = parts.length ? "#" + parts.join("&") : "";
    if ((location.hash || "") !== want) {
      try { history.replaceState(null, "", want || location.pathname + location.search); } catch (e) { /* file: or sandboxed */ }
    }
  }
  /* The page is an app on a desktop: a rail of filters on the left and the results filling the rest, each
     scrolling in its own box so the grid's scrollbars are always on screen. Under 900px the rail is a
     drawer opened from the header. */
  function renderStocks(main) {
    initAliases();
    var hs = hashState();
    gst.query = hs.q;
    mst.view = hs.view; mst.mode = hs.mode; mst.ax = hs.ax;
    try { var ru = localStorage.getItem(RAIL_UNITS_KEY); if (ru === "z" || ru === "raw") railUnits = ru; } catch (e) { /* storage unavailable */ }
    main.innerHTML = '<div class="ld-app" id="ld-app">' +
      '<aside class="ld-rail" id="ld-rail" aria-label="Filters">' + railHTML() + "</aside>" +
      '<div class="ld-scrim" id="ld-scrim" data-rail-close hidden></div>' +
      '<section class="ld-res" aria-label="Results">' +
      '<div class="ld-stat" id="ld-stat"></div>' +
      '<div class="ld-gwrap" id="ld-grid" role="region" aria-label="Results table, scrolls both ways" tabindex="0"></div>' +
      mapHTML() +
      '<div class="ld-read" id="ld-read" aria-live="off">' + (isTouch()
        ? "Tap a row to open the company. The small chart at the top of each column shows how that figure is spread across all operating companies."
        : "Hover over a cell to read it. Click a row to open the company, or shift-click a figure to filter for companies within " + String.fromCharCode(177) + "0.5" + SIGMA + " of it.") + "</div>" +
      "</section></div>";

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
    main.querySelector("#ld-qchips").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-ti]");
      if (!b) return;
      removeToken(+b.getAttribute("data-ti"));
      q.value = gst.query; gst.limit = 150; gst.cursor = 0;
      updateScreen(true);
      var next = main.querySelector("#ld-qchips button");
      (next || q).focus();
    });
    wireRail(main);
    main.querySelector("#ld-scrim").addEventListener("click", function () { openRail(false); });
    main.querySelector("#ld-stat").addEventListener("click", onStatClick);
    var grid = main.querySelector("#ld-grid");
    grid.addEventListener("click", onGridClick);
    grid.addEventListener("mouseover", onGridHover);
    pageKeys = stocksKeys;
    redrawers.push(function () { if (mst.view === "grid" && (window.innerWidth < 560) !== gridNarrow) renderGrid({ keepScroll: true }); });
    redrawers.push(function () { if (!railIsDrawer()) openRail(false); });
    wireMap(main);
    listen(window, "hashchange", function () {
      var st = hashState();
      var sameAx = String(st.ax) === String(mst.ax);
      if (st.q === gst.query.trim() && st.view === mst.view && st.mode === mst.mode && sameAx) return;
      mst.mode = st.mode; mst.ax = st.ax;
      gst.query = st.q; syncQuery(); updateScreen(true);
      setView(st.view);
    });
    updateScreen(false);
    setView(mst.view);
  }

  function onStatClick(e) {
    var b = e.target.closest("button");
    if (!b) return;
    if (b.hasAttribute("data-rail-open")) { openRail(true); return; }
    if (b.hasAttribute("data-view")) setView(b.getAttribute("data-view"));
    var a = b.getAttribute("data-act");
    if (a === "scope") toggleScope(b.getAttribute("data-v"));
    else if (a === "cells") { gst.cells = b.getAttribute("data-v"); renderGrid({ keepScroll: true }); renderStat(); }
    else if (a === "reset") resetScreen();
    var sel = a ? '#ld-stat [data-act="' + a + '"]' + (b.getAttribute("data-v") ? '[data-v="' + b.getAttribute("data-v") + '"]' : "")
      : b.hasAttribute("data-view") ? '#ld-stat [data-view="' + b.getAttribute("data-view") + '"]' : null;
    var again = sel && document.querySelector(sel);
    if (again) again.focus();
  }
  function resetScreen() { gst.query = ""; gst.sort = { k: "cap", dir: -1 }; syncQuery(); updateScreen(true); }
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
    syncRail();
    /* The hidden view waits: the grid redraws when it is shown again, the map on every change while shown. */
    if (mst.view === "map") { gridStale = true; mapRefresh(); return; }
    gridStale = false;
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

  /* Filters in force, for the drawer button: every token except the scope switch. */
  function filterCount() { return tokenize(gst.query).filter(function (t) { return !/^scope:/i.test(t); }).length; }

  function renderStat() {
    var el = document.getElementById("ld-stat");
    if (!el) return;
    var s = screen, p = s.p, hc = s.hiddenCount;
    var hiddenNon = 0, parts = [], groups = [];
    ctx.NON_OPERATING.forEach(function (k) { hiddenNon += hc[k] || 0; });
    var debtLike = (hc.debt || 0) + (hc.structured || 0) + (hc.equity_units || 0);
    // The hidden types in the words the style guide uses: funds, shells and bond listings.
    if (hc.cef) { groups.push("funds"); parts.push(plural(hc.cef, "closed-end fund", "closed-end funds")); }
    if (hc.spac) { groups.push("shells"); parts.push(plural(hc.spac, "SPAC shell", "SPAC shells")); }
    if (debtLike) { groups.push("bond listings"); parts.push(plural(debtLike, "bond listing", "bond listings") + " (debt listings, trust certificates and unit listings)"); }
    var what = s.incCount ? "listings" : "operating companies";
    var hid = hiddenNon ? int(hiddenNon) + " listings that are not operating companies are hidden: " + andList(parts) + ". Turn them on under Listing types." :
      int(s.incCount) + " funds, shells and bond listings are shown, each with a tag. Their z-scores still compare them with operating companies.";
    var extra = [];
    if (hc.lp) extra.push(plural(hc.lp, "partnership", "partnerships"));
    if (hc.bdc) extra.push(plural(hc.bdc, "business development company", "business development companies"));
    if (hc.royalty_trust) extra.push(plural(hc.royalty_trust, "royalty trust", "royalty trusts"));
    var drop = "";
    if (s.dropN) {
      var dk = Object.keys(s.dropKinds);
      drop = " " + plural(s.dropN, "listing", "listings") + " (" + andList(dk.map(function (k) { return plural(s.dropKinds[k], KIND_WHY[k] || k, KIND_PLURAL[k] || k); })) +
        (s.dropN === 1 ? ") is" : ") are") + " left out because " + s.dropKeys.map(function (k) { return BYKEY[k].label; }).join(" or ") +
        " does not apply to " + (s.dropN === 1 ? "it." : "them.");
    }
    var scope = p.scope, nf = filterCount(), rail = document.getElementById("ld-rail");
    var why = hid + (extra.length ? " Your filters also hide " + andList(extra) + "." : "") + " " +
      (scope === "sector" ? "Z-scores compare each company with its own sector." : "Z-scores compare each company with all " + int(cohortN()) + " operating companies.") +
      (Object.keys(p.bands).length || Object.keys(p.raw).length ? " A company with no figure for a filtered metric is left out." : "") + drop;
    el.innerHTML = '<p class="cnt" id="ld-count" data-n="' + s.match.length + '" data-m="' + s.universeN + '"><b>' + int(s.match.length) + "</b> of " + int(s.universeN) + " " + what + " " + (s.match.length === 1 ? "matches" : "match") + "</p>" +
      '<p class="hid" id="ld-hid" title="' + esc(why) + '">' + (hiddenNon ? int(hiddenNon) + " " + andList(groups) + " hidden" : int(s.incCount) + " funds, shells and bond listings shown") +
      (s.dropN ? " " + MID + " " + int(s.dropN) + " left out" : "") + "</p>" +
      '<div class="tools"><button type="button" class="ld-btn ld-fbtn" data-rail-open aria-controls="ld-rail" aria-expanded="' + !!(rail && rail.classList.contains("open")) + '">Filters (' + nf + ")</button>" +
      '<div class="ld-seg ld-vseg" role="group" aria-label="View"><button type="button" data-view="grid" aria-pressed="' + (mst.view === "grid") + '">Grid</button>' +
      '<button type="button" data-view="map" aria-pressed="' + (mst.view === "map") + '">Map</button></div>' +
      '<div class="ld-seg" role="group" aria-label="Compare with"><button type="button" data-act="scope" data-v="universe" aria-pressed="' + (scope === "universe") + '" title="Compare each company with all companies (keyboard: s)">All</button>' +
      '<button type="button" data-act="scope" data-v="sector" aria-pressed="' + (scope === "sector") + '" title="Compare each company with its own sector (keyboard: s)">Sector</button></div>' +
      (mst.view === "grid" ? '<div class="ld-seg" role="group" aria-label="Cells show"><button type="button" data-act="cells" data-v="raw" aria-pressed="' + (gst.cells === "raw") + '" title="Show each figure (keyboard: z)">Value</button>' +
      '<button type="button" data-act="cells" data-v="z" aria-pressed="' + (gst.cells === "z") + '" title="Show z-scores (keyboard: z)">z</button></div>' : "") +
      '<button type="button" class="ld-btn" data-act="reset">Reset</button>' +
      (mst.view === "map" ? "" : '<span class="ld-zleg" aria-label="Colour scale, from 3σ below the median to 3σ above"><span>' + MINUS + "3" + SIGMA + '</span><i class="ld-zn6"></i><i class="ld-zn4"></i><i class="ld-zn2"></i><i class="z0"></i><i class="ld-zp2"></i><i class="ld-zp4"></i><i class="ld-zp6"></i><span>+3' + SIGMA + "</span></span>") + "</div>";
  }

  /* ---------- the rail ----------
   * Every filter on the left writes a query token, and every change to the query redraws the rail, so the
   * screen bar, the rail and the URL (#q=) always say the same thing and any screen can be bookmarked.
   * Metric filters in raw units write a number with a unit (pe<=20x, roe>=15%, mcap>=10000m); in z they
   * write the bare number, as the query language always has. The Raw | z switch picks how a metric with no
   * filter yet is entered; a metric that already has one keeps the unit its token was written in. */
  var RAIL_UNITS_KEY = "apt-stocks-rail-units", VIEWS_KEY = "apt-stocks-views";
  var railUnits = "raw", railTimer = 0, viewsNote = "";
  var SECTOR_TOK = { "Information Technology": "it", "Health Care": "hc", "Financials": "fin", "Industrials": "ind",
    "Utilities": "util", "Materials": "mat", "Consumer Discretionary": "disc", "Consumer Staples": "staples",
    "Real Estate": "re", "Communication Services": "comm", "Energy": "energy", "": "none" };
  var IDX_ORDER = ["sp500", "sp400", "sp600", "nasdaq", "nyse", "amex"];
  /* Listing types: the first three are hidden unless asked for, the last three shown unless turned off. */
  var LISTING_SW = [["spac", "SPAC shells", ["spac"], 0], ["notes", "Bond listings", ["debt", "structured", "equity_units"], 0],
    ["cef", "Closed-end funds", ["cef"], 0], ["lp", "Partnerships", ["lp"], 1], ["bdc", "Business development companies", ["bdc"], 1], ["trust", "Royalty trusts", ["royalty_trust"], 1]];

  function railIsDrawer() { return !!(window.matchMedia && window.matchMedia("(max-width: 899px)").matches); }
  function openRail(on) {
    var rail = document.getElementById("ld-rail"), scrim = document.getElementById("ld-scrim");
    if (!rail) return;
    var was = rail.classList.contains("open");
    rail.classList.toggle("open", !!on);
    if (scrim) scrim.hidden = !on;
    document.querySelectorAll("[data-rail-open]").forEach(function (b) { b.setAttribute("aria-expanded", String(!!on)); });
    if (on && !was) { var c = rail.querySelector(".ld-rclose"); if (c) c.focus(); }
    if (!on && was && rail.contains(document.activeElement)) { var b = document.querySelector("[data-rail-open]"); if (b) b.focus(); }
  }
  function railOpen() { var r = document.getElementById("ld-rail"); return !!(r && r.classList.contains("open")); }

  /* A raw rail input's unit: the label beside it, the suffix its token carries, and what one typed unit is
     worth in the stored value. Market cap is entered in $M. */
  function rawUnit(m) {
    if (m.unit === "pct") return { lab: "%", suf: "%", mul: 0.01 };
    if (m.unit === "x") return { lab: "x", suf: "x", mul: 1 };
    if (m.unit === "usd") return { lab: "$M", suf: "m", mul: 1e6 };
    return { lab: "", suf: "r", mul: 1 };
  }
  function numStr(v) { return String(+(+v).toPrecision(8)); }
  /* What the reader typed, as a stored value (raw) or a z. Commas, a $ and a k, m, b or t suffix are read;
     blank or unreadable is NaN, an open end. */
  function readInput(s, kind, u) {
    s = String(s || "").trim().toLowerCase().replace(/[$,\s]/g, "").replace(/−/g, "-").replace(/σ/g, "");
    if (!s) return NaN;
    var last = s.charAt(s.length - 1), sc = { k: 1e3, m: 1e6, b: 1e9, t: 1e12 }[last];
    if (kind === "z") return isFinite(+s) ? +s : NaN;
    if (sc) { s = s.slice(0, -1); return s && isFinite(+s) ? +s * sc : NaN; }
    if (last === "%") { s = s.slice(0, -1); return s && isFinite(+s) ? +s / 100 : NaN; }
    return isFinite(+s) ? +s * u.mul : NaN;
  }
  function bandToken(key, lo, hi, kind) {
    var a = ALIAS[key], u = kind === "z" ? { suf: "", mul: 1 } : rawUnit(BYKEY[key]);
    function n(v) { return numStr(v / u.mul) + u.suf; }
    var hasLo = !isNaN(lo), hasHi = !isNaN(hi);
    if (hasLo && hasHi) return a + ":" + n(Math.min(lo, hi)) + ".." + n(Math.max(lo, hi));
    if (hasLo) return a + ">=" + n(lo);
    if (hasHi) return a + "<=" + n(hi);
    return "";
  }
  function isKeyTok(key) { return function (t) { var m = t.match(/^([a-z0-9_]+)[<>=:]/); return !!m && A2K[m[1]] === key; }; }
  /* Replace every token the test matches with toks (empty strings dropped). */
  function setTokens(test, toks) {
    var keep = tokenize(gst.query).filter(function (t) { return !test(t.toLowerCase()); });
    gst.query = keep.concat(toks.filter(Boolean)).join(" ");
  }
  function applyQuery() { syncQuery(); updateScreen(true); }
  /* The unit a metric row is showing: its filter's, else the rail's switch. */
  function rowKind(key) {
    var p = screen ? screen.p : null;
    if (p && p.raw[key]) return "raw";
    if (p && p.bands[key]) return "z";
    return railUnits;
  }
  function railSec(id, title, body, open) {
    return '<details class="ld-rg" data-rg="' + id + '"' + (open ? " open" : "") + '><summary><span class="t">' + title + '</span><span class="n" data-rgn="' + id + '"></span></summary>' +
      '<div class="ld-rgb">' + body + "</div></details>";
  }
  function railHTML() {
    var narrow = window.innerWidth < 560;
    var secs = {};
    for (var i = 0; i < N; i++) secs[S.sector[i]] = 1;
    var secList = Object.keys(secs).sort(function (a, b) { return !a ? 1 : !b ? -1 : a.localeCompare(b); });
    var th = RESEARCH.thesis || {};
    var h = '<div class="ld-rhead"><span class="ld-rtitle">Filters</span>' +
      '<button type="button" class="ld-btn" data-act="reset">Reset</button>' +
      '<button type="button" class="ld-btn ld-rclose" data-rail-close aria-label="Close filters">Close</button></div>' +
      '<div class="ld-rq"><div class="ld-cmdrow"><label for="ld-q" class="ld-sr">Filters, typed</label>' +
      '<input id="ld-q" name="ld-q" autocomplete="off" spellcheck="false" autocapitalize="off" value="' + esc(gst.query) + '" placeholder="' +
      (narrow ? "gm>1 pe<-0.5" : "gm>1 pe<-0.5 vol<0") + '" aria-describedby="ld-qchips">' +
      '<button type="button" class="ld-btn" data-help-local aria-label="How to write filters, and keyboard keys">? Help</button></div>' +
      '<div class="ld-qchips" id="ld-qchips" aria-live="polite"></div></div>';
    h += railSec("pre", "Ready-made screens", '<ul class="ld-rpre">' + PRESETS.map(function (p) {
      return '<li><button type="button" data-preset="' + esc(p.q) + '" title="' + esc((p.blurb ? p.blurb + " " : "") + p.q) + '">' + esc(p.name) + "</button></li>";
    }).join("") + "</ul>", true);
    h += railSec("idx", "Index or exchange", '<div class="ld-rchips"><button type="button" class="ld-chip" data-idx="" aria-pressed="true">All</button>' + IDX_ORDER.map(function (k) {
      return '<button type="button" class="ld-chip" data-idx="' + k + '" aria-pressed="false">' + esc(IDX_TOK[k]) + ' <span class="c" data-idxn="' + k + '"></span></button>';
    }).join("") + "</div>", true);
    h += railSec("sec", "Sector", '<div class="ld-rlist">' + secList.map(function (sc) {
      return '<label><input type="checkbox" data-sec="' + esc(sc) + '"><span>' + esc(sectorName(sc)) + '</span><span class="c" data-secn="' + esc(sc) + '"></span></label>';
    }).join("") + "</div>", true);
    h += railSec("res", "Research", '<div class="ld-rchips"><button type="button" class="ld-chip" data-res="" aria-pressed="true">All</button>' + RESEARCH_ORDER.map(function (k) {
      var n = k === "thesis" ? Object.keys(th).length : k === "watchlist" ? (RESEARCH.watchlist || []).length : Object.keys(th).filter(function (t) { return th[t] === k; }).length;
      if (!n && k !== "thesis" && k !== "watchlist") return "";
      return '<button type="button" class="ld-chip" data-res="' + k + '" aria-pressed="false">' + esc(RESEARCH_WORD[k]) + ' <span class="c" data-resn="' + k + '"></span></button>';
    }).join("") + "</div>", true);
    h += railSec("cap", "Market cap ($M)", '<div class="ld-mm"><input class="ld-rin" data-cap="lo" inputmode="decimal" placeholder="min" aria-label="Minimum market cap, in $ millions">' +
      '<span>to</span><input class="ld-rin" data-cap="hi" inputmode="decimal" placeholder="max" aria-label="Maximum market cap, in $ millions"></div>', true);
    h += '<div class="ld-rmh"><span class="ld-rtitle">Metrics</span><div class="ld-seg" role="group" aria-label="Enter metric filters as">' +
      '<button type="button" data-units="raw" aria-pressed="' + (railUnits === "raw") + '">Value</button><button type="button" data-units="z" aria-pressed="' + (railUnits === "z") + '">z-score</button></div></div>';
    A.groups.forEach(function (g) {
      h += railSec("g-" + g, esc(g), M.filter(function (m) { return m.group === g; }).map(function (m) {
        return '<div class="ld-mrow" data-k="' + m.key + '"><span class="lab" title="' + esc(m.label + ". " + betterTxt(m) + ".") + '"><code>' + esc(ALIAS[m.key]) + "</code> " + esc(m.label) + "</span>" +
          '<span class="hs" aria-hidden="true"></span>' +
          '<span class="mm"><input class="ld-rin" data-lo="' + m.key + '" inputmode="decimal" aria-label="' + esc(m.label) + ', minimum">' +
          '<span>to</span><input class="ld-rin" data-hi="' + m.key + '" inputmode="decimal" aria-label="' + esc(m.label) + ', maximum"><span class="u"></span></span></div>';
      }).join(""), false);
    });
    h += railSec("hyg", "Data coverage", '<p class="ld-rnote">Require at least this many of the 5 figures behind each factor score.</p><div class="ld-dp">' + DP_DIMS.map(function (d) {
      return '<label><span>' + d + '</span><input class="ld-rin" type="number" min="0" max="5" step="1" data-dp="' + d + '" value="0"></label>';
    }).join("") + '</div><label class="ld-rcheck"><input type="checkbox" data-needcap><span>Require a market cap</span></label>', false);
    h += railSec("lst", "Listing types", '<p class="ld-rnote">Operating companies are always shown. Bond listings are debt listings, trust certificates and unit listings.</p><div class="ld-rlist">' + LISTING_SW.map(function (w) {
      return '<label><input type="checkbox" data-sw="' + w[0] + '"><span>' + esc(w[1]) + '</span><span class="c" data-swn="' + w[0] + '"></span></label>';
    }).join("") + "</div>", false);
    h += railSec("views", "Saved views", '<ul class="ld-views" id="ld-views"></ul>' +
      '<div class="ld-vsave"><input class="ld-rin" id="ld-vname" maxlength="60" placeholder="Name this view" aria-label="Name for the saved view">' +
      '<button type="button" class="ld-btn" data-vsave>Save</button></div><p class="ld-rnote" id="ld-vnote" aria-live="polite"></p>', true);
    h += '<div class="ld-rfoot"><p class="ld-kicker">' + int(N) + " listings " + MID + " " + A.metrics.length + " metrics " + MID + " prices of " + dateShort(PRICE_DATE) + "</p>" +
      '<p class="ld-gfoot" id="ld-gfoot">Figures as of ' + dateMid(ASOF) + "; the price and 1D (one-day change) columns are from the close on " + dateLong(PRICE_DATE) + ". " +
      "A z-score is how far a figure sits from the median, in units of the usual spread; market cap and volume are put on a log scale first. " +
      "A dot means there is no figure; n/a means the figure does not apply to that type of listing.</p></div>";
    return h;
  }

  /* z of a stored value against the universe, to mark a raw band on the z histogram. */
  function zOfRaw(key, v) {
    if (!isFinite(v)) return v;
    var st = mz("universe").stats[key];
    if (!st || !(st.scale > 0)) return NaN;
    var t = BYKEY[key].transform === "log10" ? (v > 0 ? Math.log10(v) : -Infinity) : v;
    return (t - st.center) / st.scale;
  }
  function rawShown(key, v) { return isFinite(v) ? numStr(v / rawUnit(BYKEY[key]).mul) : ""; }

  function syncRail() {
    var rail = document.getElementById("ld-rail");
    if (!rail || !screen) return;
    var p = screen.p, s = screen, act = document.activeElement;
    function setVal(inp, v) { if (inp !== act) inp.value = v; }
    function cnt(n) { return n ? int(n) : "0"; }
    rail.querySelectorAll("[data-idx]").forEach(function (b) {
      var k = b.getAttribute("data-idx");
      b.setAttribute("aria-pressed", String(k ? p.idx.indexOf(IDX_TOK[k]) >= 0 : !p.idx.length));
    });
    rail.querySelectorAll("[data-idxn]").forEach(function (c) { c.textContent = cnt(s.idxCount[IDX_TOK[c.getAttribute("data-idxn")]]); });
    rail.querySelectorAll("[data-sec]").forEach(function (c) { c.checked = p.sectors.indexOf(c.getAttribute("data-sec")) >= 0; });
    rail.querySelectorAll("[data-secn]").forEach(function (c) { c.textContent = cnt(s.secCount[c.getAttribute("data-secn")]); });
    rail.querySelectorAll("[data-res]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-res") === p.research)); });
    rail.querySelectorAll("[data-resn]").forEach(function (c) { c.textContent = cnt(s.resCount[c.getAttribute("data-resn")]); });
    var capB = p.raw.market_cap;
    rail.querySelectorAll("[data-cap]").forEach(function (inp) { setVal(inp, capB ? rawShown("market_cap", capB[inp.getAttribute("data-cap") === "lo" ? 0 : 1]) : ""); });
    rail.querySelectorAll("[data-units]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-units") === railUnits)); });
    var groupN = {};
    rail.querySelectorAll(".ld-mrow").forEach(function (row) {
      var key = row.getAttribute("data-k"), m = BYKEY[key], kind = rowKind(key), b = kind === "z" ? p.bands[key] : p.raw[key];
      if (b) groupN[m.group] = (groupN[m.group] || 0) + 1;
      row.setAttribute("data-kind", kind);
      row.classList.toggle("on", !!b);
      var lo = row.querySelector("[data-lo]"), hi = row.querySelector("[data-hi]");
      var show = kind === "z" ? function (v) { return isFinite(v) ? numStr(v) : ""; } : function (v) { return rawShown(key, v); };
      setVal(lo, b ? show(b[0]) : ""); setVal(hi, b ? show(b[1]) : "");
      lo.placeholder = kind === "z" ? "min z" : "min"; hi.placeholder = kind === "z" ? "max z" : "max";
      row.querySelector(".u").textContent = kind === "z" ? SIGMA : rawUnit(m).lab;
      var zb = p.bands[key] || null;
      if (!zb && p.raw[key] && p.scope === "universe") zb = [zOfRaw(key, p.raw[key][0]), zOfRaw(key, p.raw[key][1])];
      row.querySelector(".hs").innerHTML = histSVG(key, zb, p.scope, 96, 18);
    });
    A.groups.forEach(function (g) {
      var n = rail.querySelector('[data-rgn="g-' + g + '"]'), d = rail.querySelector('[data-rg="g-' + g + '"]');
      if (n) n.textContent = groupN[g] ? String(groupN[g]) : "";
      // A group with a filter in it opens once, so a screen read from the URL shows its terms.
      if (d && groupN[g] && !d.hasAttribute("data-seen")) { d.open = true; d.setAttribute("data-seen", ""); }
    });
    rail.querySelectorAll("[data-dp]").forEach(function (inp) { setVal(inp, String(p.dp[inp.getAttribute("data-dp")] || 0)); });
    rail.querySelector("[data-needcap]").checked = p.needCap;
    var sw = listingState(p);
    rail.querySelectorAll("[data-sw]").forEach(function (c) { c.checked = sw[c.getAttribute("data-sw")]; });
    LISTING_SW.forEach(function (w) {
      var n = 0, c = rail.querySelector('[data-swn="' + w[0] + '"]');
      w[2].forEach(function (k) { n += A.kinds[k] || 0; });
      if (c) c.textContent = cnt(n);
    });
    var badges = { idx: p.idx.length, sec: p.sectors.length, res: p.research ? 1 : 0, cap: capB ? 1 : 0,
                   hyg: Object.keys(p.dp).filter(function (d) { return p.dp[d] > 0; }).length + (p.needCap ? 1 : 0) };
    Object.keys(badges).forEach(function (k) { var n = rail.querySelector('[data-rgn="' + k + '"]'); if (n) n.textContent = badges[k] ? String(badges[k]) : ""; });
    renderViews();
  }

  /* Which listing types are shown, as the query has them. */
  function listingState(p) {
    var out = {};
    LISTING_SW.forEach(function (w) {
      var k = w[2][0];
      out[w[0]] = w[3] ? !p.hideKinds[k] : !!p.include[k];
    });
    return out;
  }
  function writeListings(state) {
    setTokens(function (t) { return /^[+-]/.test(t) && !!KIND_SWITCH[t.slice(1)]; }, LISTING_SW.map(function (w) {
      var on = state[w[0]];
      return w[3] ? (on ? "" : "-" + w[0]) : (on ? "+" + w[0] : "");
    }));
    applyQuery();
  }

  function writeRow(key, loS, hiS, kind) {
    var u = rawUnit(BYKEY[key]);
    setTokens(isKeyTok(key), [bandToken(key, readInput(loS, kind, u), readInput(hiS, kind, u), kind)]);
    applyQuery();
  }

  function wireRail(main) {
    var rail = main.querySelector("#ld-rail");
    rail.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      var p = screen.p, v;
      if (b.hasAttribute("data-rail-close")) { openRail(false); return; }
      if (b.getAttribute("data-act") === "reset") { resetScreen(); return; }
      if (b.hasAttribute("data-preset")) { gst.query = b.getAttribute("data-preset"); applyQuery(); return; }
      if (b.hasAttribute("data-idx")) {
        v = b.getAttribute("data-idx");
        var on = {};
        IDX_ORDER.forEach(function (k) { if (p.idx.indexOf(IDX_TOK[k]) >= 0) on[k] = 1; });
        if (!v) on = {}; else if (on[v]) delete on[v]; else on[v] = 1;
        setTokens(function (t) { return /^(idx|index):/.test(t); }, IDX_ORDER.filter(function (k) { return on[k]; }).map(function (k) { return "idx:" + k; }));
        applyQuery(); focusSame(b, "data-idx"); return;
      }
      if (b.hasAttribute("data-res")) {
        v = b.getAttribute("data-res");
        setTokens(function (t) { return /^research:/.test(t); }, [v && v !== p.research ? "research:" + v : ""]);
        applyQuery(); focusSame(b, "data-res"); return;
      }
      if (b.hasAttribute("data-units")) {
        railUnits = b.getAttribute("data-units") === "z" ? "z" : "raw";
        try { localStorage.setItem(RAIL_UNITS_KEY, railUnits); } catch (err) { /* storage unavailable */ }
        syncRail(); return;
      }
      if (b.hasAttribute("data-vsave")) { saveView(); return; }
      if (b.hasAttribute("data-vload")) {
        var vw = loadViews()[+b.getAttribute("data-vload")];
        if (vw) { gst.query = vw.q; viewsNote = "Loaded " + vw.name + "."; applyQuery(); }
        return;
      }
      if (b.hasAttribute("data-vdel")) {
        var all = loadViews(), gone = all.splice(+b.getAttribute("data-vdel"), 1)[0];
        viewsNote = storeViews(all) ? (gone ? "Deleted " + gone.name + "." : "") : "Could not delete: this browser is not storing data for this site.";
        renderViews();
        var nm = document.getElementById("ld-vname"); if (nm) nm.focus();
      }
    });
    rail.addEventListener("change", function (e) {
      var t = e.target, p = screen.p;
      if (t.hasAttribute("data-sec")) {
        var on = p.sectors.slice(), sc = t.getAttribute("data-sec"), at = on.indexOf(sc);
        if (t.checked && at < 0) on.push(sc); else if (!t.checked && at >= 0) on.splice(at, 1);
        setTokens(function (x) { return /^(sector|sec|s):/.test(x); }, on.map(function (x) {
          return "sector:" + (SECTOR_TOK[x] !== undefined ? SECTOR_TOK[x] : x.toLowerCase().replace(/[^a-z0-9]/g, ""));
        }));
        applyQuery(); return;
      }
      if (t.hasAttribute("data-needcap")) { setTokens(function (x) { return x === "has:cap"; }, [t.checked ? "has:cap" : ""]); applyQuery(); return; }
      if (t.hasAttribute("data-sw")) { var st = listingState(p); st[t.getAttribute("data-sw")] = t.checked; writeListings(st); return; }
      railInput(t, true);
    });
    rail.addEventListener("input", function (e) { railInput(e.target, false); });
    rail.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      if (e.target.id === "ld-vname") { e.preventDefault(); saveView(); return; }
      if (e.target.classList.contains("ld-rin")) { e.preventDefault(); railInput(e.target, true); }
    });
  }
  function focusSame(b, attr) {
    var again = document.querySelector("#ld-rail [" + attr + '="' + b.getAttribute(attr) + '"]');
    if (again) again.focus();
  }
  /* Typing in a rail number waits a moment before it rewrites the query; leaving the field or Enter applies it now. */
  function railInput(t, now) {
    var fn = null;
    if (t.hasAttribute("data-cap")) {
      var box = t.closest(".ld-mm");
      fn = function () { writeRow("market_cap", box.querySelector('[data-cap="lo"]').value, box.querySelector('[data-cap="hi"]').value, "raw"); };
    } else if (t.hasAttribute("data-lo") || t.hasAttribute("data-hi")) {
      var row = t.closest(".ld-mrow"), key = row.getAttribute("data-k");
      fn = function () { writeRow(key, row.querySelector("[data-lo]").value, row.querySelector("[data-hi]").value, rowKind(key)); };
    } else if (t.hasAttribute("data-dp")) {
      var d = t.getAttribute("data-dp");
      fn = function () {
        var n = Math.max(0, Math.min(5, Math.round(+t.value) || 0));
        setTokens(function (x) { var m = x.match(/^dp:([a-z]+)/); return !!m && DP_WORD[m[1]] === d; }, [n ? "dp:" + d.toLowerCase() + ">=" + n : ""]);
        applyQuery();
      };
    }
    if (!fn) return;
    if (railTimer) { clearTimeout(railTimer); railTimer = 0; }
    if (now) fn(); else railTimer = setTimeout(function () { railTimer = 0; fn(); }, 350);
  }

  /* Saved views: a name and a query, kept in this browser only. */
  function loadViews() {
    try {
      var v = JSON.parse(localStorage.getItem(VIEWS_KEY) || "[]");
      return Array.isArray(v) ? v.filter(function (x) { return x && typeof x.name === "string" && typeof x.q === "string"; }) : [];
    } catch (e) { return []; }
  }
  function storeViews(v) { try { localStorage.setItem(VIEWS_KEY, JSON.stringify(v)); return true; } catch (e) { return false; } }
  function saveView() {
    var nm = document.getElementById("ld-vname"), name = (nm && nm.value || "").trim().slice(0, 60);
    if (!name) { viewsNote = "Give the view a name first."; renderViews(); if (nm) nm.focus(); return; }
    var all = loadViews().filter(function (x) { return x.name !== name; });
    all.push({ name: name, q: gst.query.trim() });
    viewsNote = storeViews(all) ? "Saved " + name + "." : "Could not save: this browser is not storing data for this site.";
    if (nm) nm.value = "";
    renderViews();
  }
  function renderViews() {
    var ul = document.getElementById("ld-views"), note = document.getElementById("ld-vnote");
    if (!ul) return;
    var all = loadViews(), q = gst.query.trim();
    ul.innerHTML = all.length ? all.map(function (v, i) {
      return '<li><button type="button" class="ld-vload" data-vload="' + i + '" aria-pressed="' + (v.q === q) + '" title="' + esc(v.q || "No filters") + '">' + esc(v.name) + "</button>" +
        '<button type="button" class="x" data-vdel="' + i + '" aria-label="Delete ' + esc(v.name) + '">' + TIMES + "</button></li>";
    }).join("") : '<li class="none">No saved views yet.</li>';
    if (note) note.textContent = viewsNote;
  }

  function cohortHist(scope, key) {
    var ck = scope + "|" + key;
    if (!gHistCache[ck]) gHistCache[ck] = ctx.APTZ.hist(mz(scope), key, 24, -4, 4);
    return gHistCache[ck];
  }
  function histSVG(key, band, scope, W, H) {
    W = W || 60; H = H || 16;
    var h = cohortHist(scope, key), n = h.counts.length, bw = W / n, mx = Math.max.apply(null, h.counts) || 1;
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
    var fixed = [["tk", "Ticker"], ["name", "Name"], ["sector", "Sector"], ["price", "Price"], ["cap", "Mkt cap"], ["chg", "1D"]];
    gridNarrow = window.innerWidth < 560;
    var W = { tk: 92, name: gridNarrow ? 124 : 200, sector: 104, price: 84, cap: 70, chg: 72 }, MW = 76;
    var tw = W.tk + W.name + W.sector + W.price + W.cap + W.chg + MW * cols.length;
    var h = '<table class="ld-g" style="width:' + tw + 'px"><caption class="ld-sr">Matching listings and ' + cols.length + " metrics, " + (gst.cells === "z" ? "showing z-scores" : "showing figures") + ", tinted by z-score</caption><colgroup>" +
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
        '><button type="button" class="colbtn" data-sort="' + m.key + '" title="' + esc(m.label + ". " + betterTxt(m) + ". Click to sort by z-score.") + '">' +
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
    if (!s.order.length) h += '<div class="ld-gmore">Nothing matches. Remove a filter, widen a range, or show more under Listing types.</div>';
    else if (lim < s.order.length) {
      var nx = Math.min(150, s.order.length - lim);
      h += '<div class="ld-gmore"><span>Showing ' + int(lim) + " of " + int(s.order.length) + '.</span><button type="button" class="ld-btn" data-more>Show the next ' + nx + "</button></div>";
    } else h += '<div class="ld-gmore"><span>' + (s.order.length === 1 ? "Showing the one match." : "Showing all " + int(s.order.length) + ".") + "</span></div>";
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
    var capBlank = na(i, "market_cap"), chgBlank = !!S.chg_gap[i];
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
      if (td.hasAttribute("data-na")) { setReadout("<b>" + esc(S.ticker[i]) + "</b> " + esc(midLabel(cols[+td.getAttribute("data-na")].label)) + " " + naWhy(S.kind[i]) + ", so there is nothing to filter around."); return; }
      addBandAround(cols[+td.getAttribute("data-c")].key, i);
      return;
    }
    ctx.go("company", S.ticker[i]);
  }
  function isTouch() { return !!(window.matchMedia && window.matchMedia("(hover: none)").matches); }

  function addBandAround(key, i) {
    if (na(i, key)) { setReadout("<b>" + esc(S.ticker[i]) + "</b> " + esc(midLabel(BYKEY[key].label)) + " " + naWhy(S.kind[i]) + ", so no filter was added."); return; }
    var z = screen.res.z[key][i];
    if (isNaN(z)) { setReadout("<b>" + esc(S.ticker[i]) + "</b> has no " + esc(midLabel(BYKEY[key].label)) + " z-score, so no filter was added."); return; }
    var lo = Math.round((z - 0.5) * 100) / 100, hi = Math.round((z + 0.5) * 100) / 100, a = ALIAS[key];
    setToken(function (t) { var m = t.match(/^([a-z0-9_]+)(?:[<>=:]|$)/); return m && A2K[m[1]] === key; }, a + ":" + lo + ".." + hi);
    syncQuery();
    updateScreen(true);
    setReadout("Added a filter around " + esc(S.ticker[i]) + ": <b>" + esc(a) + "</b> from " + zsig(lo) + " to " + zsig(hi) + ".");
  }
  function setReadout(html) { var r = document.getElementById("ld-read"); if (r) r.innerHTML = html; }

  function onGridHover(e) {
    if (isTouch()) return;
    var tr = e.target.closest("tr[data-i]"), td = e.target.closest("td[data-c],td[data-na]");
    if (!td || !tr) return;
    var c = td.hasAttribute("data-c") ? td.getAttribute("data-c") : td.getAttribute("data-na");
    cellReadout(e.currentTarget.__cols[+c], +tr.getAttribute("data-i"));
  }
  /* The hover line, as a finding: "NVDA revenue growth 83.4%: higher than 93% of companies. Higher is better.
     Updates at the next filing." Size metrics say larger and smaller; a neutral metric states no direction. */
  function cellReadout(m, i) {
    var kind = S.kind[i], sec = S.sector[i], tk = "<b>" + esc(S.ticker[i]) + "</b> " + esc(midLabel(m.label));
    if (na(i, m.key)) { setReadout(tk + " " + naWhy(kind) + "."); return; }
    var v = A.vals[m.key][i], zU = mz("universe").z[m.key][i], zS = screen.p.scope === "sector" ? screen.res.z[m.key][i] : NaN;
    var stt = S.status[i] && S.status[i][m.key], note = stt ? " " + (STATUS_WHY[stt] || stt) + "." : "";
    if (v == null) { setReadout(tk + ": no figure." + note); return; }
    var pc = pctl(m.key, i), size = m.unit === "usd" || m.unit === "shares";
    var up = size ? "larger" : "higher", dn = size ? "smaller" : "lower", where = "";
    if (!isNaN(pc)) {
      var p = Math.round(pc);
      where = pc >= 99.5 ? up + " than almost every company" : pc < 0.5 ? dn + " than almost every company"
        : p >= 50 ? up + " than " + p + "% of companies" : dn + " than " + (100 - p) + "% of companies";
    }
    var zs = gst.cells === "z" && !isNaN(zU) ? " (z-score " + zsig(zU) + ")" : "";
    setReadout(tk + " <b>" + esc(ctx.APTZ.fmt(m.key, v)) + "</b>" + zs + (where ? ": " + where : "") + "." +
      (screen.p.scope === "sector" ? (sec ? " Within " + esc(sec) + ", its z-score is " + zsig(zS) + "." : " It has no sector to compare with.") : "") +
      (m.better ? " " + betterTxt(m) + "." : "") + note);
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
    if (mst.view === "map" && e.key !== "s") return false;   // the grid's keys; the map's canvas takes its own
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
      if (!key) { setReadout("Sort by a metric column first: <b>b</b> then filters for companies near the cursor row on that metric."); return true; }
      addBandAround(key, screen.order[gst.cursor]); return true;
    }
    return false;
  }
  function cursorReadout() {
    if (!screen || !screen.order.length) return;
    var i = screen.order[gst.cursor];
    setReadout("Row " + int(gst.cursor + 1) + " of " + int(screen.order.length) + ": <b>" + esc(S.ticker[i]) + "</b> " + esc(S.name[i]) +
      ". Enter opens it. The red tick in each column's small chart marks where it sits.");
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
  /* ---------------------------------------------------------------- STOCKS: the factor map
   * The grid's other view (stocks.html#view=map). The screen's matches are ink dots and the rest of the
   * universe a faint cloud behind them, so the map shows where a screen sits. Two projections:
   *   factors  the four dimension scores (Growth, Value, Momentum, Quality; each a z against the company's
   *            own sector, as the pipeline scores them) on the corners of a regular tetrahedron. The four
   *            corner vectors sum to zero, so a company that scores evenly sits in the middle and a
   *            lopsided one is pulled toward the corners it earns. A company needs 3 of the 4 scores to be
   *            placed; a missing fourth counts as zero, the sector median.
   *   axes     any three of the metrics as x, y and z, in universe robust z clipped at +/-4.
   * Restored from the old Radar view's factor map (tetraPos, drawTetra, the p97 scale) and drawn in the
   * Ledger palette. Hand-rolled canvas: yaw about the vertical, then pitch, then a mild perspective
   * divide. Drag or the arrow keys turn it; a click focuses a company, shift-click adds up to five. */

  var TETRA_V = { g: [1, 1, 1], v: [1, -1, -1], m: [-1, 1, -1], q: [-1, -1, 1] };
  var TETRA_ORDER = ["g", "v", "m", "q"];
  var TETRA_COL = { g: "g", v: "v", m: "mom", q: "q" };           // the S column holding each score
  var TETRA_LABEL = { g: "Growth", v: "Value", m: "Momentum", q: "Quality" };
  var TETRA_EDGES = [["g", "v"], ["g", "m"], ["g", "q"], ["v", "m"], ["v", "q"], ["m", "q"]];
  var RT3 = Math.sqrt(3), AX_CLIP = 4, FOCUS_MAX = 5;
  var AX_DEFAULT = ["roe_ttm", "pe", "return_12_2"];
  var VIEW_ANGLE = { tetra: [0.62, -0.32], axes: [-0.62, -0.34] };
  // Honour the system setting rather than offering motion this reader has said they do not want.
  var REDUCED_MOTION = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var mst = { view: "grid", mode: "tetra", ax: null, yaw: 0.62, pitch: -0.32, spin: false, add: false,
              focus: [], lists: null, drawn: null };
  var TP = null, AXC = null, mapColors = null, gridStale = false, spinId = 0, drawId = 0;
  var PX2 = null;   // projected x, y, depth and radius per row, from the last frame drawn

  function mapHTML() {
    var opts = A.groups.map(function (g) {
      return '<optgroup label="' + esc(g) + '">' + M.filter(function (m) { return m.group === g; }).map(function (m) {
        return '<option value="' + m.key + '">' + esc(ALIAS[m.key] + "  " + m.label) + "</option>";
      }).join("") + "</optgroup>";
    }).join("");
    var tap = isTouch();
    return '<section class="ld-map" id="ld-map" aria-label="Factor map" hidden>' +
      '<div class="ld-mapbar">' +
      '<div class="ld-seg" role="group" aria-label="The map shows"><button type="button" data-mode="tetra" aria-pressed="true">Factor scores</button>' +
      '<button type="button" data-mode="axes" aria-pressed="false">Three metrics</button></div>' +
      '<div class="ld-mapaxes" id="ld-mapaxes" hidden>' + ["x", "y", "z"].map(function (a, j) {
        return '<label><span>' + a + '</span><select class="ld-select" data-ax="' + j + '" aria-label="Metric on the ' + a + ' axis">' + opts + "</select></label>";
      }).join("") + "</div>" +
      '<span class="sp"></span>' +
      '<button type="button" class="ld-btn" data-spin aria-pressed="false"' + (REDUCED_MOTION ? ' disabled title="Off, because your system asks for reduced motion"' : ' title="Turn the map slowly"') + ">Spin</button>" +
      '<button type="button" class="ld-btn" data-add aria-pressed="false" title="Each ' + (tap ? "tap" : "click") + ' adds a company to the selection instead of replacing it' + (tap ? "" : " (shift-click does the same)") + '">Compare</button>' +
      '<button type="button" class="ld-btn" data-mapreset title="Back to the starting angle">Reset view</button></div>' +
      '<div class="ld-mapgrid"><div><div class="ld-mapbox">' +
      '<canvas id="ld-mapc" tabindex="0" role="img" aria-describedby="ld-mapcount" aria-label="Factor map"></canvas></div>' +
      '<p class="ld-mapcount" id="ld-mapcount" aria-live="polite"></p>' +
      '<p class="ld-mapkey" aria-hidden="true"><span><i class="m"></i>matches your filters</span><span><i class="c"></i>all other companies</span><span><i class="f"></i>selected</span></p>' +
      '<p class="ld-mapread" id="ld-mapread"></p></div>' +
      '<aside class="ld-mapfocus" id="ld-mapfocus" aria-label="Selected companies" aria-live="polite"></aside></div>' +
      '<p class="ld-gfoot" id="ld-mapfoot"></p></section>';
  }

  function mapHint() {
    return (isTouch() ? "Drag to turn the map. Tap a dot to select it; turn on Compare to select up to five."
      : "Drag or use the arrow keys to turn the map. Click a dot to select it; shift-click to add up to five.");
  }
  function setMapRead(html) { var r = document.getElementById("ld-mapread"); if (r) r.innerHTML = html; }

  function setView(v) {
    mst.view = v === "map" ? "map" : "grid";
    var map = document.getElementById("ld-map");
    if (!map) return;
    var isMap = mst.view === "map";
    map.hidden = !isMap;
    ["ld-grid", "ld-read"].forEach(function (id) { var el = document.getElementById(id); if (el) el.hidden = isMap; });
    document.querySelectorAll("[data-view]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-view") === mst.view)); });
    writeHash();
    renderStat();
    if (isMap) { mapRefresh(); startSpin(); return; }
    stopSpin();
    if (gridStale) { gridStale = false; renderGrid(); }
  }

  /* The three axes in use: the viewer's pick, else the metrics the query bands (in query order), filled
     from AX_DEFAULT, so switching to axes shows the screen's own terms. */
  function axesNow() {
    if (mst.ax) return mst.ax;
    var out = [];
    Object.keys(screen ? screen.p.bands : {}).concat(AX_DEFAULT).forEach(function (k) { if (out.length < 3 && out.indexOf(k) < 0) out.push(k); });
    return out;
  }

  /* Factor positions, once per page: normalised by the 97th percentile distance of the operating cohort
     (at least the corner distance), so one scale holds for the whole cloud and the shape you are turning
     stays the same shape whatever the screen. */
  function tetraCoords() {
    if (TP) return TP;
    var x = new Float64Array(N), y = new Float64Array(N), z = new Float64Array(N), ds = [];
    var mk = ctx.z("universe").cohortMask;
    for (var i = 0; i < N; i++) {
      if (S.ndim[i] < 3) { x[i] = y[i] = z[i] = NaN; continue; }
      var px = 0, py = 0, pz = 0;
      for (var j = 0; j < 4; j++) {
        var k = TETRA_ORDER[j], w = S[TETRA_COL[k]][i] || 0, V = TETRA_V[k];
        px += w * V[0]; py += w * V[1]; pz += w * V[2];
      }
      x[i] = px / RT3; y[i] = py / RT3; z[i] = pz / RT3;
      if (mk[i]) ds.push(Math.sqrt(x[i] * x[i] + y[i] * y[i] + z[i] * z[i]));
    }
    ds.sort(function (a, b) { return a - b; });
    var p97 = ds.length ? ds[Math.min(ds.length - 1, Math.floor(ds.length * 0.97))] : 1;
    var ref = Math.max(p97, RT3);
    for (i = 0; i < N; i++) { x[i] /= ref; y[i] /= ref; z[i] /= ref; }
    TP = { x: x, y: y, z: z, corner: RT3 / ref };
    return TP;
  }
  /* Axis positions: universe z clipped at +/-4, scaled so the cube's corners sit on the unit sphere. */
  function axisCoords(keys) {
    var id = keys.join(",");
    if (AXC && AXC.id === id) return AXC;
    var U = ctx.z("universe"), c = [new Float64Array(N), new Float64Array(N), new Float64Array(N)], s = 1 / (AX_CLIP * RT3);
    keys.forEach(function (k, a) {
      var zz = U.z[k], out = c[a];
      for (var i = 0; i < N; i++) { var v = zz[i]; out[i] = isNaN(v) ? NaN : Math.max(-AX_CLIP, Math.min(AX_CLIP, v)) * s; }
    });
    AXC = { id: id, keys: keys, x: c[0], y: c[1], z: c[2] };
    return AXC;
  }
  function mapCoords() { return mst.mode === "axes" ? axisCoords(axesNow()) : tetraCoords(); }

  /* Which rows are drawn, recomputed when the screen or the projection changes (not per frame). */
  function mapLists() {
    var P = mapCoords(), inM = new Uint8Array(N), match = [], cloud = [], hid = screen.hidden;
    function placed(i) { return !isNaN(P.x[i]) && !isNaN(P.y[i]) && !isNaN(P.z[i]); }
    for (var j = 0; j < screen.match.length; j++) { var i = screen.match[j]; inM[i] = 1; if (placed(i)) match.push(i); }
    for (i = 0; i < N; i++) if (!inM[i] && !hid[S.kind[i]] && placed(i)) cloud.push(i);
    var capMax = 0;
    for (i = 0; i < N; i++) if (S.mcap_raw[i] > capMax) capMax = S.mcap_raw[i];
    mst.lists = { P: P, inM: inM, match: match, cloud: cloud, m: screen.match.length, n: match.length,
                  k: screen.match.length - match.length, capMax: capMax || 1 };
  }

  function mapRefresh() {
    if (!screen || mst.view !== "map") return;
    var bar = document.getElementById("ld-map");
    if (!bar) return;
    bar.querySelectorAll("[data-mode]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-mode") === mst.mode)); });
    var axEl = document.getElementById("ld-mapaxes"), keys = axesNow();
    axEl.hidden = mst.mode !== "axes";
    axEl.querySelectorAll("select").forEach(function (sel, j) { sel.value = keys[j]; });
    mapLists();
    mapCount();
    renderFocus();
    drawMap();
  }

  function mapCount() {
    var L = mst.lists, el = document.getElementById("ld-mapcount"), foot = document.getElementById("ld-mapfoot");
    var one = screen.incCount ? "listing" : "company", what = screen.incCount ? "listings" : "companies", axes = mst.mode === "axes", keys = axesNow();
    var why = axes ? "no figure on one of the three axes" : "fewer than 3 of the 4 factor scores";
    el.setAttribute("data-n", L.n); el.setAttribute("data-m", L.m); el.setAttribute("data-k", L.k);
    // How many matches are on the map, how many are not and why, and what the faint dots are.
    var on = !L.m ? "No " + one + " matches your filters. "
      : L.n === L.m ? (L.m === 1 ? "The <b>1</b> matching " + one + " is on the map. " : "All <b>" + int(L.m) + "</b> matching " + what + " are on the map. ")
      : "<b>" + int(L.n) + "</b> of the " + int(L.m) + " matching " + what + " " + (L.n === 1 ? "is" : "are") + " on the map. " +
        int(L.k) + " " + (L.k === 1 ? "is not, because it has " : "are not, because they have ") + why + ". ";
    el.innerHTML = on + (L.cloud.length ? "The " + (L.cloud.length === 1 ? "1 other " + one + " is" : int(L.cloud.length) + " other " + what + " are") + " drawn faint. " : "") + "Dot size shows market cap.";
    var cv = document.getElementById("ld-mapc");
    if (cv) cv.setAttribute("aria-label", (axes ? "3D chart of " + andList(keys.map(function (k) { return BYKEY[k].label; })) : "Factor map of the Growth, Value, Momentum and Quality scores") +
      ": " + plural(L.n, "matching " + one, "matching " + what) + " shown. The arrow keys turn it.");
    foot.innerHTML = axes
      ? "Each axis is a z-score against all " + int(cohortN()) + " operating companies, whatever the Sector setting: how far a company sits from the median, in units of the usual spread. " +
        "A company past " + String.fromCharCode(177) + "4" + SIGMA + " is drawn at " + String.fromCharCode(177) + "4" + SIGMA + ", on a face of the cube. Market cap and volume are put on a log scale first."
      : "Each corner is one of the four factor scores, which compare a company with its own sector. A company is pulled toward the corners it scores well on, and sits near the middle when its scores are even. " +
        "It needs 3 of the 4 scores to appear; a missing fourth counts as zero, the sector median. The scale is set by the 97th percentile of operating companies' distance from the middle, so a few far-out companies sit beyond the corners.";
    if (!document.getElementById("ld-mapread").innerHTML) setMapRead(mapHint());
  }

  function mapColorsNow() {
    if (mapColors) return mapColors;
    var cs = getComputedStyle(document.documentElement);
    function v(n, d) { return (cs.getPropertyValue(n) || "").trim() || d; }
    mapColors = { ink: v("--ink", "#1c1a16"), ink2: v("--ink2", "#474238"), muted: v("--muted", "#655d50"), faint: v("--faint", "#a79e8d"),
                  hair: v("--hair", "#d3cab8"), accent: v("--accent", "#b8361c"), accentInk: v("--accent-ink", "#9c2c15"),
                  bg: v("--raised", "#f6f2e9"), mono: v("--mono", "monospace") };
    return mapColors;
  }

  function requestDraw() { if (!drawId) drawId = requestAnimationFrame(function () { drawId = 0; drawMap(); }); }

  function drawMap() {
    var cv = document.getElementById("ld-mapc");
    if (!cv || mst.view !== "map" || !mst.lists) return;
    var w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    var g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    var C = mapColorsNow(), L = mst.lists, P = L.P, axes = mst.mode === "axes";
    var narrow = w < 520, scale = (Math.min(w, h) / 2 - (narrow ? 26 : 38)) / 1.12;
    var cyw = Math.cos(mst.yaw), syw = Math.sin(mst.yaw), cp = Math.cos(mst.pitch), sp = Math.sin(mst.pitch);
    // Mild perspective: enough that the near side reads as nearer, not so much it bends the cloud.
    function proj(x, y, z) {
      var x1 = x * cyw + z * syw, z1 = -x * syw + z * cyw, y2 = y * cp - z1 * sp, z2 = y * sp + z1 * cp, k = 4 / (4 - z2);
      return { x: w / 2 + x1 * scale * k, y: h / 2 - y2 * scale * k, z: z2, k: k };
    }
    if (!PX2) PX2 = { x: new Float64Array(N), y: new Float64Array(N), z: new Float64Array(N), r: new Float64Array(N) };
    var sizeK = narrow ? 0.7 : 1;
    function place(i) {
      var q = proj(P.x[i], P.y[i], P.z[i]);
      PX2.x[i] = q.x; PX2.y[i] = q.y; PX2.z[i] = q.z;
      var cap = S.mcap_raw[i];
      PX2.r[i] = (cap > 0 ? 1.6 + Math.sqrt(cap / L.capMax) * 9 * sizeK : 1.6) * q.k;
    }
    L.match.forEach(place); L.cloud.forEach(place);
    var fnt = function (px, wt) { return (wt || 500) + " " + px + "px " + C.mono; };

    // The frame first, so the dots read as sitting inside it. Edges behind the centre are dashed.
    g.lineWidth = 1;
    var labels = [];
    if (!axes) {
      var vp = {}, cr = tetraCoords().corner;
      TETRA_ORDER.forEach(function (k) { vp[k] = proj(TETRA_V[k][0] / RT3 * cr, TETRA_V[k][1] / RT3 * cr, TETRA_V[k][2] / RT3 * cr); });
      TETRA_EDGES.forEach(function (e) {
        var a = vp[e[0]], b = vp[e[1]], behind = (a.z + b.z) / 2 < 0;
        g.setLineDash(behind ? [3, 4] : []); g.strokeStyle = behind ? C.faint : C.ink2; g.globalAlpha = behind ? 0.8 : 0.7;
        g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); g.stroke();
      });
      TETRA_ORDER.forEach(function (k) { labels.push({ p: vp[k], t: TETRA_LABEL[k].toUpperCase(), corner: true }); });
    } else {
      var a = 1 / RT3, keys = axesNow(), corners = [];
      for (var c = 0; c < 8; c++) corners.push(proj(c & 1 ? a : -a, c & 2 ? a : -a, c & 4 ? a : -a));
      for (c = 0; c < 8; c++) for (var bit = 1; bit < 8; bit <<= 1) {
        if (c & bit) continue;
        var p0 = corners[c], p1 = corners[c | bit], behind2 = (p0.z + p1.z) / 2 < 0;
        g.setLineDash(behind2 ? [3, 4] : []); g.strokeStyle = C.hair; g.globalAlpha = 1;
        g.beginPath(); g.moveTo(p0.x, p0.y); g.lineTo(p1.x, p1.y); g.stroke();
      }
      g.setLineDash([]);
      [[1, 0, 0], [0, 1, 0], [0, 0, 1]].forEach(function (u, j) {
        var lo = proj(-a * u[0], -a * u[1], -a * u[2]), hi = proj(a * u[0], a * u[1], a * u[2]);
        g.strokeStyle = C.ink2; g.globalAlpha = 0.75;
        g.beginPath(); g.moveTo(lo.x, lo.y); g.lineTo(hi.x, hi.y); g.stroke();
        for (var t = -AX_CLIP; t <= AX_CLIP; t += 2) {
          var tp = proj(t / AX_CLIP * a * u[0], t / AX_CLIP * a * u[1], t / AX_CLIP * a * u[2]);
          g.beginPath(); g.arc(tp.x, tp.y, t === 0 ? 1.2 : 1.8, 0, Math.PI * 2); g.fillStyle = C.ink2; g.fill();
        }
        labels.push({ p: hi, t: ["x", "y", "z"][j] + " " + (narrow ? ALIAS[keys[j]] : BYKEY[keys[j]].label) + " +4" + SIGMA, corner: false });
        labels.push({ p: lo, t: MINUS + "4" + SIGMA, corner: false, small: true });
      });
    }
    g.setLineDash([]); g.globalAlpha = 1;
    // The origin: where a company that is typical on every axis sits.
    var o = proj(0, 0, 0);
    g.strokeStyle = C.ink2; g.globalAlpha = 0.6;
    g.beginPath(); g.moveTo(o.x - 4, o.y); g.lineTo(o.x + 4, o.y); g.moveTo(o.x, o.y - 4); g.lineTo(o.x, o.y + 4); g.stroke();

    // The rest of the universe: one faint layer, drawn as a single path.
    g.globalAlpha = L.n < 800 ? 0.4 : 0.55; g.fillStyle = C.faint; g.beginPath();
    L.cloud.forEach(function (i) { g.moveTo(PX2.x[i] + 1.1, PX2.y[i]); g.arc(PX2.x[i], PX2.y[i], 1.1, 0, Math.PI * 2); });
    g.fill();

    // The matches, far first so near ones overlap them. Nearer dots are more opaque; depth is the only
    // thing separating an overlapping pair. Few matches get more ink, so a narrow screen still reads.
    var order = L.match.slice().sort(function (p, q) { return PX2.z[p] - PX2.z[q]; });
    var focusOn = mst.focus.some(function (i) { return !isNaN(P.x[i]) && !isNaN(P.y[i]) && !isNaN(P.z[i]); });
    var few = L.n < 800, dimF = focusOn ? 0.7 : 1, rMin = few ? 2.4 : 1;
    g.fillStyle = C.ink;
    order.forEach(function (i) {
      var t = Math.max(0, Math.min(1, (PX2.z[i] + 1) / 2));
      g.globalAlpha = (few ? 0.5 + t * 0.4 : 0.16 + t * 0.34) * dimF;
      g.beginPath(); g.arc(PX2.x[i], PX2.y[i], Math.max(rMin, PX2.r[i]), 0, Math.PI * 2); g.fill();
    });
    g.globalAlpha = 1;
    mst.drawn = { w: w, h: h, order: order, cloud: L.cloud };

    // Labels, pushed out along the ray from the centre so they clear the cloud at any angle.
    g.textBaseline = "middle";
    labels.forEach(function (lb) {
      var ox = lb.p.x - w / 2, oy = lb.p.y - h / 2, len = Math.sqrt(ox * ox + oy * oy) || 1, push = lb.small ? 10 : 16;
      g.font = fnt(lb.small ? 9.5 : 10.5);
      var tw = g.measureText(lb.t).width;
      var x = lb.p.x + ox / len * push, y = lb.p.y + oy / len * push;
      g.textAlign = "center";
      x = Math.max(tw / 2 + 4, Math.min(w - tw / 2 - 4, x)); y = Math.max(9, Math.min(h - 9, y));
      g.globalAlpha = 1; g.lineWidth = 3; g.strokeStyle = C.bg; g.strokeText(lb.t, x, y);
      g.fillStyle = lb.p.z < 0 || lb.small ? C.muted : C.ink; g.fillText(lb.t, x, y);
      if (lb.corner) { g.beginPath(); g.arc(lb.p.x, lb.p.y, 2.5, 0, Math.PI * 2); g.fill(); }
    });

    // Focus last, in the accent, so a chosen company is never buried.
    g.font = fnt(11.5);
    g.textAlign = "left";
    mst.focus.forEach(function (i) {
      if (isNaN(P.x[i]) || isNaN(P.y[i]) || isNaN(P.z[i])) return;
      place(i);
      var x = PX2.x[i], y = PX2.y[i], r = Math.max(4, PX2.r[i]);
      g.globalAlpha = 1; g.fillStyle = C.accent; g.strokeStyle = C.accent; g.lineWidth = 1.5;
      g.beginPath(); g.arc(x, y, Math.min(r, 6), 0, Math.PI * 2); g.fill();
      g.beginPath(); g.arc(x, y, r + 4, 0, Math.PI * 2); g.stroke();
      var tx = x + r + 7, lab = S.ticker[i], tw = g.measureText(lab).width;
      if (tx + tw > w - 4) tx = x - r - 7 - tw;
      g.lineWidth = 3; g.strokeStyle = C.bg; g.strokeText(lab, tx, y);
      g.fillStyle = C.accentInk; g.fillText(lab, tx, y);
    });

    if (!L.n) {
      g.font = fnt(11.5, 400); g.textAlign = "left"; g.fillStyle = C.muted; g.globalAlpha = 1;
      var msg = L.m ? "No matching company has enough figures to appear on the map." : "Nothing matches your filters.";
      g.lineWidth = 3; g.strokeStyle = C.bg; g.strokeText(msg, 14, 18); g.fillText(msg, 14, 18);
    }
    g.textAlign = "start"; g.textBaseline = "alphabetic"; g.globalAlpha = 1;
  }

  /* The dot under a point: matches first (they are what the screen asks about), then the faint cloud.
     A frame drawn at another size is not trusted; the map redraws and the click is dropped, since picking
     the wrong company is worse than picking none. */
  function mapHit(cv, cx, cy, slop) {
    var d = mst.drawn;
    if (!d || d.w !== cv.clientWidth || d.h !== cv.clientHeight) { drawMap(); return -2; }
    var b = cv.getBoundingClientRect(), mx = cx - b.left, my = cy - b.top, best = -1, bd = Infinity;
    for (var j = d.order.length - 1; j >= 0; j--) {
      var i = d.order[j], dx = PX2.x[i] - mx, dy = PX2.y[i] - my, dd = dx * dx + dy * dy, rr = Math.max(4, PX2.r[i]) + slop;
      if (dd < rr * rr && dd < bd) { bd = dd; best = i; }
    }
    if (best >= 0) return best;
    for (j = 0; j < d.cloud.length; j++) {
      i = d.cloud[j]; dx = PX2.x[i] - mx; dy = PX2.y[i] - my; dd = dx * dx + dy * dy;
      if (dd < (3 + slop) * (3 + slop) && dd < bd) { bd = dd; best = i; }
    }
    return best;
  }

  function scoreLine(i) {
    if (mst.mode === "axes") return axesNow().map(function (k, j) {
      var v = A.vals[k][i], z = ctx.z("universe").z[k][i];
      return ["x", "y", "z"][j] + " " + esc(ALIAS[k]) + " " + (v == null ? "n/a" : esc(ctx.APTZ.fmt(k, v)) + " (" + zsig(z) + ")");
    }).join(", ");
    return TETRA_ORDER.map(function (k) { var v = S[TETRA_COL[k]][i]; return TETRA_LABEL[k].charAt(0) + " " + (v == null ? "n/a" : fsgn(v, 2)); }).join("  ");
  }
  function hoverRead(i) {
    if (i < 0) { setMapRead(mapHint()); return; }
    setMapRead("<b>" + esc(S.ticker[i]) + "</b> " + esc(S.name[i]) + (mst.lists.inM[i] ? "" : ", outside your filters") + ". " + scoreLine(i));
  }

  function toggleFocus(i, add) {
    var at = mst.focus.indexOf(i);
    if (!add) mst.focus = at >= 0 && mst.focus.length === 1 ? [] : [i];
    else if (at >= 0) mst.focus.splice(at, 1);
    else if (mst.focus.length >= FOCUS_MAX) { setMapRead("The map holds up to five selected companies. Remove one first."); return; }
    else mst.focus.push(i);
    renderFocus();
    drawMap();
  }

  function renderFocus() {
    var el = document.getElementById("ld-mapfocus");
    if (!el || !mst.lists) return;
    var head = '<div class="ld-mfh"><span class="ld-kicker">Selected' + (mst.focus.length ? " " + mst.focus.length + " of " + FOCUS_MAX : "") + "</span>" +
      (mst.focus.length ? '<button type="button" class="ld-btn" data-unfocus="all">Clear</button>' : "") + "</div>";
    if (!mst.focus.length) {
      el.innerHTML = head + '<p class="none">' + (isTouch() ? "Tap a dot to see the company here. Turn on Compare to see up to five side by side."
        : "Click a dot to see the company here. Shift-click, or turn on Compare, to see up to five side by side.") + "</p>";
      return;
    }
    var P = mst.lists.P, U = ctx.z("universe");
    el.innerHTML = head + mst.focus.map(function (i) {
      var tk = S.ticker[i], placedHere = !isNaN(P.x[i]) && !isNaN(P.y[i]) && !isNaN(P.z[i]);
      var rows = TETRA_ORDER.map(function (k) {
        var v = S[TETRA_COL[k]][i];
        return "<dt>" + TETRA_LABEL[k] + '</dt><dd class="' + zClass(v) + '">' + (v == null ? "n/a" : zsig(v)) + "</dd>";
      }).join("");
      var axRows = mst.mode === "axes" ? axesNow().map(function (k, j) {
        var v = A.vals[k][i], z = U.z[k][i];
        return "<dt>" + ["x", "y", "z"][j] + " " + esc(BYKEY[k].label) + '</dt><dd class="' + zClass(z) + '">' + (na(i, k) ? "n/a" : v == null ? "No figure" : esc(ctx.APTZ.fmt(k, v)) + " " + zsig(z)) + "</dd>";
      }).join("") : "";
      var notes = [];
      if (!mst.lists.inM[i]) notes.push("Outside your filters.");
      if (!placedHere) notes.push(mst.mode === "axes" ? "Not on the map: it has no figure on one of the axes." : "Not on the map: it has only " + S.ndim[i] + " of the 4 factor scores.");
      return '<div class="ld-mf"><div class="hd"><a class="ld-gtk" href="' + ctx.href("company", tk) + '">' + esc(tk) + "</a>" + kindTag(S.kind[i]) +
        '<button type="button" class="x" data-unfocus="' + i + '" aria-label="Remove ' + esc(tk) + ' from the selection">' + TIMES + "</button></div>" +
        '<div class="nm" title="' + esc(S.name[i]) + '">' + esc(S.name[i]) + "</div>" +
        '<div class="sub">' + esc(sectorName(S.sector[i])) + (S.mcap_raw[i] ? " " + MID + " market cap $" + fcap(S.mcap_raw[i]) : "") + "</div>" +
        '<dl><dt class="h">Scores within sector</dt><dd class="h"></dd>' + rows + (axRows ? '<dt class="h">Axes, against all companies</dt><dd class="h"></dd>' + axRows : "") + "</dl>" +
        (notes.length ? '<p class="off">' + notes.join(" ") + "</p>" : "") +
        '<a class="ld-link" href="' + ctx.href("company", tk) + '">Company page ' + ARROW + "</a></div>";
    }).join("");
  }

  function setSpin(on) {
    mst.spin = !!on && !REDUCED_MOTION;
    var b = document.querySelector("[data-spin]");
    if (b) b.setAttribute("aria-pressed", String(mst.spin));
    if (mst.spin) startSpin(); else stopSpin();
  }
  function startSpin() {
    if (spinId || !mst.spin || mst.view !== "map") return;
    var last = 0;
    spinId = requestAnimationFrame(function step(t) {
      spinId = 0;
      if (!mst.spin || mst.view !== "map") return;
      mst.yaw += 0.00019 * (last ? Math.min(64, t - last) : 16);
      last = t;
      drawMap();
      spinId = requestAnimationFrame(step);
    });
  }
  function stopSpin() { if (spinId) { cancelAnimationFrame(spinId); spinId = 0; } }
  function resetAngle() { var va = VIEW_ANGLE[mst.mode]; mst.yaw = va[0]; mst.pitch = va[1]; }

  function wireMap(main) {
    var cv = main.querySelector("#ld-mapc"), map = main.querySelector("#ld-map");
    if (!cv) return;
    resetAngle();
    map.querySelectorAll("[data-mode]").forEach(function (b) {
      b.addEventListener("click", function () {
        var m = b.getAttribute("data-mode");
        if (m === mst.mode) return;
        mst.mode = m; resetAngle(); writeHash(); mapRefresh();
      });
    });
    map.querySelectorAll("select[data-ax]").forEach(function (sel) {
      sel.addEventListener("change", function () {
        var keys = axesNow().slice();
        keys[+sel.getAttribute("data-ax")] = sel.value;
        mst.ax = keys; writeHash(); mapRefresh();
      });
    });
    map.querySelector("[data-spin]").addEventListener("click", function () { setSpin(!mst.spin); });
    map.querySelector("[data-add]").addEventListener("click", function (e) {
      mst.add = !mst.add; e.currentTarget.setAttribute("aria-pressed", String(mst.add));
    });
    map.querySelector("[data-mapreset]").addEventListener("click", function () { setSpin(false); resetAngle(); drawMap(); });
    map.querySelector("#ld-mapfocus").addEventListener("click", function (e) {
      var b = e.target.closest("[data-unfocus]");
      if (!b) return;
      var v = b.getAttribute("data-unfocus");
      mst.focus = v === "all" ? [] : mst.focus.filter(function (i) { return i !== +v; });
      renderFocus(); drawMap();
      var next = document.querySelector("#ld-mapfocus [data-unfocus]");
      (next || cv).focus();
    });

    var drag = null, suppress = false;
    cv.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      // Any press starts clean, so a stale arm from an abandoned gesture cannot swallow this selection.
      suppress = false;
      drag = { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY, moved: 0, spun: mst.spin, touch: e.pointerType !== "mouse" };
      stopSpin();
      try { cv.setPointerCapture(e.pointerId); } catch (err) { /* already released */ }
      cv.classList.add("drag");
    });
    cv.addEventListener("pointermove", function (e) {
      if (!drag) {
        if (e.pointerType !== "mouse") return;
        var i = mapHit(cv, e.clientX, e.clientY, 5);
        cv.style.cursor = i >= 0 ? "pointer" : "";
        hoverRead(i);
        return;
      }
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      // Furthest the pointer got from where it was pressed, not the path length, so a tremor that ends
      // where it began is still a click.
      drag.moved = Math.max(drag.moved, Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy));
      mst.yaw += dx * 0.008;
      // Clamped short of vertical, where the labels would flip and the shape stops reading.
      mst.pitch = Math.max(-1.35, Math.min(1.35, mst.pitch + dy * 0.008));
      requestDraw();
    });
    function release(e, cancelled) {
      if (!drag) return;
      var d = drag;
      drag = null;
      cv.classList.remove("drag");
      try { cv.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      // A deliberate turn parks the shape where the reader put it; spin stays off until asked for again.
      if (!cancelled && d.moved >= (d.touch ? 10 : 4)) { suppress = true; setSpin(false); }
      else if (d.spun) startSpin();
    }
    cv.addEventListener("pointerup", function (e) { release(e, false); });
    // A cancelled pointer never produces a click, so arming the suppression there would swallow the next.
    cv.addEventListener("pointercancel", function (e) { release(e, true); });
    cv.addEventListener("pointerleave", function (e) { if (!drag && e.pointerType === "mouse") { cv.style.cursor = ""; hoverRead(-1); } });
    cv.addEventListener("click", function (e) {
      if (suppress) { suppress = false; return; }
      var i = mapHit(cv, e.clientX, e.clientY, isTouch() ? 12 : 6);
      if (i < 0) return;
      toggleFocus(i, mst.add || e.shiftKey || e.ctrlKey || e.metaKey);
      hoverRead(i);
    });
    cv.addEventListener("keydown", function (e) {
      var step = e.shiftKey ? 0.3 : 0.1;
      if (e.key === "ArrowLeft") mst.yaw -= step;
      else if (e.key === "ArrowRight") mst.yaw += step;
      else if (e.key === "ArrowUp") mst.pitch = Math.max(-1.35, mst.pitch - step);
      else if (e.key === "ArrowDown") mst.pitch = Math.min(1.35, mst.pitch + step);
      else if (e.key === "Escape" && mst.focus.length) { mst.focus = []; renderFocus(); drawMap(); e.preventDefault(); e.stopPropagation(); return; }
      else return;
      e.preventDefault();
      setSpin(false);
      drawMap();
    });
    redrawers.push(drawMap);
    themeHooks.push(function () { mapColors = null; drawMap(); });
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
   *   prices/T.json   daily closes for the chart; news/T.json the latest headlines
   *   history/T.json  every panel day's metrics, with history/_universe.json for each day's z (write_history_views) */

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
      main.innerHTML = '<div class="ld-wrap"><div class="ld-head"><div><div class="ld-kicker"><b>Company</b></div><h1 class="ld-h1">No listing with the ticker ' + esc(tk) + '</h1><p class="ld-deck">None of the listings we track' + esc(ASOF ? " as of " + dateMid(ASOF) : "") + ' has that ticker. <a class="ld-inl" href="' + ctx.href("stocks") + '">Search the Stocks page</a> to find the one you want.</p></div></div></div>';
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

    var capTxt = na(i, "market_cap") ? "Market cap " + naWhy(kind) : capOf(i) === "n/a" ? "Market cap not reported" : "Market cap " + capOf(i);
    var head = '<div class="ld-co-head"><div><div class="ld-kicker"><a class="ld-inl" href="' + ctx.href("stocks") + '">Stocks</a> ' + MID + " " + esc(sectorName(S.sector[i])) + (S.sub[i] ? " " + MID + " " + esc(S.sub[i]) : "") + "</div>" +
      "<h1>" + esc(S.name[i]) + "</h1>" +
      '<div><span class="ld-co-tk">' + esc(tk) + "</span>" + (kind !== "operating" ? '<span class="ld-kt ld-kt-' + kind + (isNonOp(kind) ? " ld-kt-nonop" : "") + '">' + esc(ctx.KIND_LABEL[kind] || kind) + "</span>" : "") +
      ' <span class="ld-kicker" style="margin-left:8px">' + esc(S.index[i]) + "</span></div>" +
      (S.full_name[i] && S.full_name[i] !== S.name[i] ? '<div class="ld-muted" style="font-size:13px;margin-top:6px">Listed as ' + esc(S.full_name[i]) + "</div>" : "") +
      "</div>" +
      '<div class="ld-px"><div class="ld-kicker">' + (S.price[i] == null ? "No recent close" : closeLabel(pdate)) + '</div><div class="p" style="margin-top:6px">' + money(S.price[i]) + "</div>" +
      (S.chg_gap[i] ? '<div class="c">Day change not available: the previous session’s close is missing</div>' : '<div class="c ' + chgCls(chg) + '">' + chgTxt(chg) + " on the day</div>") +
      '<div class="cap">' + esc(capTxt) + "</div></div></div>";

    // chart: every stored daily close
    var pts = [], chartNote = "";
    var closes = files.prices && files.prices.closes || [];
    closes.forEach(function (c) { if (c && c.length >= 2 && c[1] != null && isFinite(c[1])) pts.push({ d: c[0], v: +c[1] }); });
    if (pts.length > 5) chartNote = "Daily closing price, " + dateMid(pts[0].d) + " to " + dateMid(pts[pts.length - 1].d);
    else pts = [];
    var perf = pts.length ? (pts[pts.length - 1].v / pts[0].v - 1) * 100 : null;

    var dims = [["Growth", S.g[i]], ["Value", S.v[i]], ["Quality", S.q[i]], ["Momentum", S.mom[i]]];
    var facts = '<ul class="ld-facts">' +
      '<li><span class="k">Overall score</span><span class="v">' + (S.score[i] == null ? "Not scored" : signed(S.score[i], 2)) + "</span></li>" +
      dims.map(function (d) {
        var v = d[1];
        var bar = v == null ? "" : '<span class="ld-dimbar" aria-hidden="true"><i style="' + (v >= 0 ? "left:50%;width:" : "right:50%;width:") + Math.max(2, Math.min(50, Math.abs(v) * 50)).toFixed(1) + '%"></i></span>';
        return '<li><span class="k">' + d[0] + '</span><span class="v">' + bar + (v == null ? "n/a" : signed(v, 2)) + "</span></li>";
      }).join("") +
      '<li><span class="k">Next results</span><span class="v">' + (S.earn[i] ? dateMid(S.earn[i]) : "Not yet announced") + "</span></li>" +
      '<li><span class="k">Listing type</span><span class="v">' + esc(ctx.KIND_LABEL[kind] || kind) + "</span></li>" +
      (perf != null ? '<li><span class="k">Price change on chart</span><span class="v ' + chgCls(perf) + '">' + signed(perf, 1) + "%</span></li>" : "") +
      "</ul>" +
      '<p class="ld-scale">' + (sec
        ? "<b>These scores compare " + esc(tk) + " with other " + esc(sec) + " companies.</b> Each is a z-score: how far " + esc(tk) + " sits from the middle of its sector, in units of the sector's usual spread, so +1 is one unit better than the sector's typical company. The chart further down compares it with all " + int(opN) + " operating companies we track, and the two can disagree: a P/E can look high next to all companies and ordinary within one sector. Each bar runs from 0 in the middle to " + String.fromCharCode(177) + "1\u00a0at\u00a0the\u00a0ends."
        : esc(tk) + " has no GICS sector (the standard industry classification), so it has no sector scores. The chart further down compares it with all " + int(opN) + " operating companies we track.") + "</p>";

    var nonop = isNonOp(kind);
    var near = nearestProfiles(i), nearest = near.list;

    main.innerHTML = '<div class="ld-wrap">' + head +
      (nonop ? '<p class="ld-note" style="margin-top:16px">' + esc(tk) + " is a " + esc(KIND_WHY[kind] || kind) + ", not an operating company, so it is left out of the medians that describe a typical company. Its z-scores still compare it with operating companies. Where a figure does not apply to a " + esc(KIND_WHY[kind] || kind) + ", the page says so.</p>" : "") +
      '<div class="ld-co-top"><section aria-labelledby="ld-ch-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-ch-h">Price history</h2><span class="ld-kicker">' + esc(chartNote || "None available") + "</span></div>" +
      (pts.length ? '<div class="ld-chart" id="ld-chart"></div>' : '<p class="ld-note">We have no price history to chart for ' + esc(tk) + "." + (S.price[i] != null ? " The only close we have is " + money(S.price[i]) + (pdate ? ", on " + dateMid(pdate) : "") + "." : "") + "</p>") +
      '</section><section aria-labelledby="ld-gl-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-gl-h">At a glance</h2><span class="ld-vs">' + (sec ? "Scores within " + esc(sec) : "No sector to compare with") + "</span></div>" + facts + "</section></div>" +
      thesisSection(tk, r, i) +
      '<div class="ld-co-pair"><section class="ld-sec" id="ld-where" aria-labelledby="ld-wh-h"></section>' +
      '<section class="ld-sec" id="ld-hist" aria-labelledby="ld-hi-h"></section>' +
      '<div class="ld-two"><section class="ld-sec" aria-labelledby="ld-np-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-np-h">Most similar companies</h2></div>' +
      '<p style="font-size:14px;color:var(--ink2);margin:10px 0 6px">Operating companies whose z-scores sit closest to ' + esc(tk) + "’s. Closeness is the typical gap between the two sets of z-scores (a root mean square, in " + SIGMA + "), " +
      profileScopeTxt(near.keys, kind) + " A smaller number means a closer match.</p>" +
      (nearest.length ? '<ul class="ld-peers">' + nearest.map(function (p) {
        return '<li><a class="ld-tk" href="' + ctx.href("company", S.ticker[p.i]) + '">' + esc(S.ticker[p.i]) + '</a><span class="n">' + esc(S.name[p.i]) + " " + MID + " " + esc(sectorName(S.sector[p.i])) + '</span><span class="d">' + num(p.d, 2) + SIGMA + "</span></li>";
      }).join("") + "</ul>" : '<p class="ld-note">' + esc(tk) + " has too few figures to compare with other companies.</p>") +
      "</section>" + businessBlock(tk, det) + "</div></div>" +
      companyDetail(tk, det, files.news) + "</div>";

    if (pts.length) priceChart(document.getElementById("ld-chart"), pts, chartNote);
    main.querySelectorAll("[data-prog]").forEach(function (h) { if (r) progressLine(h, r); });
    drawWhere(i);
    drawHistory(tk, i);
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
    if (keys.length <= left.length) return "using only the " + keys.length + " of " + A.metrics.length + " metrics that apply to a " + esc(why) + ": " + labs(keys.map(metric)) + ".";
    return "leaving out " + labs(left) + ", which " + (left.length === 1 ? "does" : "do") + " not apply to a " + esc(why) + ".";
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
          if (isFinite(tickZ)) strip += '<span class="sm" style="left:' + pctPos(tickZ).toFixed(2) + '%" title="' + (useSector ? "Median of all companies" : "Sector median") + '"></span>';
        }
        var z = v.blank ? NaN : R.z[k][i];
        if (isFinite(z)) strip += '<span class="dt' + (Math.abs(z) > WH ? " off" : "") + '" style="left:' + pctPos(z).toFixed(2) + '%"></span>';
        strip += "</span></div>";
        var noStats = useSector && !st0;
        return '<div class="ld-wrow"><div class="lab" title="' + esc(methodTxt(k)) + '">' + esc(m.label) + "<small>" + betterTxt(m) + "</small></div>" +
          '<div class="strip-cell" role="img" aria-label="' + esc(m.label + (v.blank ? ": " + (v.whyLong || v.why.toLowerCase()) : " " + v.txt + (isFinite(z) ? ", z-score " + zTxt(z) : ", no z-score")) + ". " + betterTxt(m) + ".") + '">' + strip + "</div>" +
          '<div class="rv' + (v.blank ? " na" : "") + '">' + esc(v.blank ? v.why : v.txt) + "</div>" +
          '<div class="zv">' + (noStats ? '<span class="ld-muted" style="font-size:11px">&lt; ' + minC + " peers</span>" : zTxt(z)) + "</div></div>";
      }).join("");
    }).join("");
    var U0 = U.stats.market_cap;
    var secN = 0;
    if (useSector) for (var j = 0; j < N; j++) if (U.cohortMask[j] && S.sector[j] === sector) secN++;
    host.innerHTML = '<div class="ld-sec-h ld-where-h"><div><h2 class="ld-h2" id="ld-wh-h">Compared with ' + (useSector ? "its sector" : "all companies") + "</h2>" +
      '<div class="ld-vs" style="margin-top:8px">' + (useSector ? "Against " + int(secN) + " operating companies in " + esc(sector) : "Against all " + int(cohortN()) + " operating companies we track") + "</div></div>" +
      '<div class="ld-seg-w"><div class="ld-seg" role="group" aria-label="Compare with"><button type="button" id="ld-cw-u" data-cs="universe" aria-pressed="' + !useSector + '">All companies</button><button type="button" id="ld-cw-s" data-cs="sector" aria-pressed="' + useSector + '"' + (noSector ? ' disabled aria-describedby="ld-cw-why" title="This listing has no GICS sector"' : "") + ">Sector</button></div>" +
      (noSector ? '<span class="ld-kicker" id="ld-cw-why">No sector to compare with</span>' : "") + "</div></div>" +
      '<p style="font-size:14.5px;color:var(--ink2);margin:12px 0 0;max-width:80ch">' + (useSector
        ? "The bars show how each figure is spread across the operating companies in " + esc(sectorName(sector)) + ", and the thin line at 0 is the sector median. The dot is " + esc(S.ticker[i]) + "’s z-score within its sector; the black tick is the median of all companies."
        : "The bars show how each figure is spread across the operating companies that report it (about " + int(Math.round((U0 ? U0.n : 0) / 100) * 100) + " for market cap), leaving out funds, shells and bond listings. The scale is the z-score: the thin line at 0 is the median, the company in the middle, and each step (" + SIGMA + ") is one unit of the usual spread, measured so that a few extreme companies do not distort it. The dot is " + esc(S.ticker[i]) + (sector ? "; the black tick is the median of its sector, " + esc(sector) + "." : ". It has no GICS sector, so there is no sector tick.")) +
      " An arrow at the end of a bar means the spread runs past " + String.fromCharCode(177) + "4" + SIGMA + ", and a hollow dot means " + esc(S.ticker[i]) + " itself is past the edge. Z-scores are capped at " + String.fromCharCode(177) + "5" + SIGMA + ", shown as " + GE + "\u00a0+5" + SIGMA + " or " + LE + "\u00a0" + MINUS + "5" + SIGMA + ".</p>" +
      '<div class="ld-legend"><span><i class="ld-lg-wh"></i>Middle 90% of companies</span><span><i class="ld-lg-box"></i>Middle 50%</span><span><i class="ld-lg-dot"></i>' + esc(S.ticker[i]) + "</span>" + (noSector ? "" : "<span><i class=\"ld-lg-tick\"></i>" + (useSector ? "Median of all companies" : "Sector median") + "</span>") + "</div>" +
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

  /* History: every panel day for one listing, from history/T.json (write_history_views), and the universe's
     centre and scale on each of those days from history/_universe.json, so any day's value can be read as a
     universe z the way zengine.js computes today's. A day with no value is drawn as a gap: the close was not
     that session's (price_stale), the field does not apply to this kind of listing, or nothing was recorded. */
  var HIST_DEFAULT = ["price", "pe", "revenue_growth_yoy", "operating_margin", "return_12_2", "volatility_1y"];
  var HIST_RANGES = [["1M", 21], ["3M", 63], ["6M", 126], ["1Y", 252]];
  var histU = null;
  function histUniverse() {
    if (!histU) histU = getJSON("history/_universe.json");
    return histU;
  }
  function histLabel(k) { return k === "price" ? "Price" : metric(k) ? metric(k).label : k; }
  function histFmt(k, v) {
    if (v == null || !isFinite(v)) return "n/a";
    if (k === "price") return money(v);
    return ctx.APTZ.fmt(k, v).replace(/^(\$?)-/, MINUS + "$1");
  }
  function histZ(k, v, U, ui) {
    var m = metric(k), st = U && U.m[k];
    if (!m || !st || v == null || !isFinite(v) || ui < 0) return NaN;
    var c = st.c[ui], s = st.s[ui];
    if (c == null || !(s > 0)) return NaN;
    var t = m.transform === "log10" ? (v > 0 ? Math.log10(v) : NaN) : v;
    if (isNaN(t)) return NaN;
    return Math.max(-5, Math.min(5, (t - c) / s));
  }

  /* One series on the panel's calendar: from the listing's first day to the panel's last, a date missing
     from the listing's file is a gap like any withheld value. why[j]: "" | "stale" | "na" | "none".
     For price only, an empty day with a close stored for that exact date (H.pf, write_history_views) is drawn
     with that close and marked fill[j] = 1. Nothing is interpolated: a day with no stored close stays a gap.
     chg[j] is the day's change on the price series, gap[j] = 1 where the panel flagged it change_gap. */
  function histSeries(H, U, k, mode) {
    var dates = U ? U.d : H.d, pos = {};
    H.d.forEach(function (d, j) { pos[d] = j; });
    var start = dates.indexOf(H.d[0]);
    if (start < 0) { dates = H.d; start = 0; }
    var raw = H.v[k], stale = {}, na = H.na && H.na[k];
    (H.stale || []).forEach(function (j) { stale[j] = 1; });
    var naSet = {};
    if (na === 1) H.d.forEach(function (d, j) { naSet[j] = 1; });
    else (na || []).forEach(function (j) { naSet[j] = 1; });
    var isPx = k === "price", pf = isPx && H.pf || {}, gapSet = {};
    (H.gap || []).forEach(function (j) { gapSet[j] = 1; });
    var out = { k: k, mode: mode, d: [], v: [], x: [], why: [], fill: [], chg: [], gap: [] };
    for (var ui = start; ui < dates.length; ui++) {
      var d = dates[ui], j = pos[d], v = j == null || !raw ? null : raw[j];
      var why = "", filled = 0;
      if (v == null || !isFinite(v)) {
        v = null;
        why = j == null ? "none" : naSet[j] ? "na" : stale[j] && (isPx || STALE_KEYS[k]) ? "stale" : "none";
        if (why !== "na" && pf[d] != null && isFinite(pf[d])) { v = +pf[d]; filled = 1; }
      }
      out.fill.push(filled);
      out.chg.push(isPx && j != null && H.c && H.c[j] != null ? H.c[j] : null);
      out.gap.push(isPx && j != null && gapSet[j] ? 1 : 0);
      var shown = v;
      if (mode === "z" && v != null) {
        shown = histZ(k, v, U, U ? ui : -1);
        if (isNaN(shown)) { shown = null; why = why || "noz"; }
      }
      out.d.push(d); out.x.push(v); out.v.push(shown); out.why.push(why);
    }
    return out;
  }
  // The fields a stale row loses (_PANEL_PRICE_FIELDS in the pipeline).
  var STALE_KEYS = {};
  ["change_pct", "volume", "volume_trend", "market_cap", "pe", "price_book", "fcf_yield", "return_1m", "return_12_2",
   "return_52w", "high52w_proximity", "rel_strength_sp500", "volatility_1y", "beta_1y", "sharpe_1y",
   "max_drawdown_1y"].forEach(function (k) { STALE_KEYS[k] = 1; });
  var HIST_WHY = { stale: "price out of date that day", na: "does not apply", none: "not recorded", noz: "no z-score that day" };
  var HIST_FILL = "close from the stored price history";

  function histPath(ser, X, Y, lo, n) {
    var d = "", run = 0, dots = [];
    for (var j = lo; j < lo + n; j++) {
      var v = ser.v[j];
      if (v == null) { if (run === 1) dots.push(j - 1); run = 0; continue; }
      d += (run ? "L" : "M") + X(j - lo).toFixed(1) + " " + Y(v).toFixed(1);
      run++;
    }
    if (run === 1) dots.push(lo + n - 1);
    return { d: d, dots: dots };
  }

  function histChart(host, ser, name) {
    var n = ser.v.length, lo = 0;
    if (cst.hrange && cst.hrange < n) { lo = n - cst.hrange; n = cst.hrange; }
    var W = Math.max(280, host.clientWidth || 640), H = W < 520 ? 210 : 270;
    var zmode = ser.mode === "z";
    var pl = 4, pr = zmode ? 44 : 70, pt = 12, pb = 26, iw = W - pl - pr, ih = H - pt - pb;
    var vs = ser.v.slice(lo, lo + n).filter(function (v) { return v != null; });
    var mn = Math.min.apply(null, vs), mx = Math.max.apply(null, vs);
    if (zmode) { mn = Math.min(mn, -2); mx = Math.max(mx, 2); }
    if (mn === mx) { var pad = Math.abs(mn) * 0.02 || 1; mn -= pad; mx += pad; }
    var step = zmode ? (mx - mn > 6 ? 2 : 1) : niceStep(mx - mn, 4);
    var y0 = Math.floor(mn / step) * step, y1 = Math.ceil(mx / step) * step;
    function X(j) { return pl + (n > 1 ? iw * j / (n - 1) : iw / 2); }
    function Y(v) { return pt + ih * (1 - (v - y0) / (y1 - y0)); }
    var g = [], sw = n > 1 ? iw / (n - 1) : iw;
    for (var j = 0; j < n; j++) {
      if (ser.v[lo + j] != null) continue;
      var gx = Math.max(pl, X(j) - sw / 2), gw = Math.min(pl + iw, X(j) + sw / 2) - gx;
      g.push('<rect class="gap" x="' + gx.toFixed(1) + '" y="' + pt + '" width="' + Math.max(1, gw).toFixed(1) + '" height="' + ih + '"/>');
    }
    for (var y = y0; y <= y1 + step / 2; y += step) {
      var yy = Y(y).toFixed(1), zero = zmode && Math.abs(y) < 1e-9;
      g.push('<line class="' + (zero ? "zl" : "gl") + '" x1="' + pl + '" x2="' + (pl + iw) + '" y1="' + yy + '" y2="' + yy + '"/>');
      var lab = zmode ? (Math.abs(y) < 1e-9 ? "0" : (y > 0 ? "+" : MINUS) + Math.abs(y)) + SIGMA : ser.k === "price" ? (y < 0 ? MINUS : "") + "$" + num(Math.abs(y), step < 1 ? 2 : 0) : histFmt(ser.k, y);
      g.push('<text x="' + (pl + iw + 8) + '" y="' + (Y(y) + 3.5).toFixed(1) + '">' + esc(lab) + "</text>");
    }
    var xl = [], lastX = -99, every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 70))));
    for (j = 0; j < n; j += every) {
      if (X(j) - lastX < 56 || X(j) > pl + iw - 24 && j) continue;
      xl.push('<text x="' + X(j).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="' + (j ? "middle" : "start") + '">' + esc(dateShort(ser.d[lo + j])) + "</text>");
      lastX = X(j);
    }
    var p = histPath(ser, X, Y, lo, n);
    var dots = p.dots.map(function (jj) { return '<circle class="pt" r="2.6" cx="' + X(jj - lo).toFixed(1) + '" cy="' + Y(ser.v[jj]).toFixed(1) + '"/>'; }).join("");
    // A day drawn from the stored close rather than the panel row: an open ring, so it reads as different.
    for (j = lo; j < lo + n; j++) {
      if (ser.fill[j] && ser.v[j] != null) dots += '<circle class="pf" r="3.2" cx="' + X(j - lo).toFixed(1) + '" cy="' + Y(ser.v[j]).toFixed(1) + '"/>';
    }
    var li = lo + n - 1; while (li >= lo && ser.v[li] == null) li--;
    var label = name + (zmode ? " z-score" : "") + ", " + dateMid(ser.d[lo]) + " to " + dateMid(ser.d[lo + n - 1]);
    host.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" height="' + H + '" role="img" aria-label="' + esc(label) + '">' +
      g.join("") + '<path class="ln" d="' + p.d + '"/>' + dots +
      '<line class="bl" x1="' + pl + '" x2="' + (pl + iw) + '" y1="' + (pt + ih) + '" y2="' + (pt + ih) + '"/>' + xl.join("") +
      (li >= lo ? '<circle class="dot" cx="' + X(li - lo).toFixed(1) + '" cy="' + Y(ser.v[li]).toFixed(1) + '" r="4"/>' : "") +
      '<line class="xh" x1="0" x2="0" y1="' + pt + '" y2="' + (pt + ih) + '" style="display:none"/>' +
      '<circle class="dot hx" r="4.5" cx="0" cy="0" style="display:none"/>' +
      '<rect x="' + pl + '" y="' + pt + '" width="' + iw + '" height="' + ih + '" fill="transparent" class="hit"/></svg>' +
      '<div class="ld-tip"></div>';
    var svg = host.querySelector("svg"), tip = host.querySelector(".ld-tip"), xh = host.querySelector(".xh"), xd = host.querySelector(".hx");
    function move(ev) {
      var r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * W / r.width;
      var jj = n > 1 ? Math.round((sx - pl) / iw * (n - 1)) : 0;
      jj = Math.max(0, Math.min(n - 1, jj));
      var x = X(jj), v = ser.v[lo + jj];
      xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.style.display = "";
      if (v != null) { xd.setAttribute("cx", x); xd.setAttribute("cy", Y(v)); xd.style.display = ""; } else xd.style.display = "none";
      var tt = histRead(ser, lo + jj);
      tip.textContent = tt;
      // A long reading (a filled day, a day change) wraps, and is kept inside the chart by its measured width.
      tip.classList.toggle("wide", tt.length > 34);
      var half = Math.min(r.width / 2, Math.max(90, tip.offsetWidth / 2));
      tip.style.left = Math.max(half, Math.min(r.width - half, x * r.width / W)) + "px";
      tip.style.top = ((v != null ? Y(v) : pt + ih / 2) * r.height / H) + "px";
      tip.style.opacity = "1";
      setHistRead(histRead(ser, lo + jj));
    }
    function leave() { xh.style.display = "none"; xd.style.display = "none"; tip.style.opacity = "0"; setHistRead(histRead(ser, li >= lo ? li : lo + n - 1)); }
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", leave);
    setHistRead(histRead(ser, li >= lo ? li : lo + n - 1));
  }
  function histRead(ser, j) {
    var head = dateShort3(ser.d[j]) + "  ";
    var raw = ser.x[j];
    if (ser.fill[j]) return head + histFmt(ser.k, ser.v[j]) + ", " + HIST_FILL + " for " + dateMid(ser.d[j]);
    if (raw == null) return head + HIST_WHY[ser.why[j]];
    var z = ser.mode === "z" ? ser.v[j] : NaN;
    var day = "";
    if (ser.k === "price") day = ser.gap[j] ? ", day change not available" : ser.chg[j] != null ? ", " + chgTxt(ser.chg[j]) + " on the day" : "";
    return head + histFmt(ser.k, raw) + day + (ser.mode === "z" ? "  z-score " + (z == null ? "n/a" : zTxt(z)) : "");
  }
  function setHistRead(t) { var el = document.getElementById("ld-hread"); if (el) el.textContent = t; }

  function histSpark(ser) {
    var W = 200, H = 54, vs = ser.v.filter(function (v) { return v != null; });
    if (!vs.length) return '<svg viewBox="0 0 200 54" aria-hidden="true"></svg>';
    var mn = Math.min.apply(null, vs), mx = Math.max.apply(null, vs);
    if (ser.mode === "z") { mn = Math.min(mn, -1); mx = Math.max(mx, 1); }
    if (mn === mx) { var pd = Math.abs(mn) * 0.02 || 1; mn -= pd; mx += pd; }
    var pad = (mx - mn) * 0.08; mn -= pad; mx += pad;
    var n = ser.v.length;
    function X(j) { return n > 1 ? W * j / (n - 1) : W / 2; }
    function Y(v) { return H * (1 - (v - mn) / (mx - mn)); }
    var p = histPath(ser, X, Y, 0, n);
    var zero = mn < 0 && mx > 0 ? '<line class="zero" x1="0" x2="' + W + '" y1="' + Y(0).toFixed(1) + '" y2="' + Y(0).toFixed(1) + '"/>' : "";
    // A lone day between gaps: a zero-length round-capped stroke, which stays a dot when the SVG is stretched.
    var dots = p.dots.map(function (j) { return "M" + X(j).toFixed(1) + " " + Y(ser.v[j]).toFixed(1) + "l0.01 0"; }).join("");
    return '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" aria-hidden="true">' + zero + '<path class="ln" d="' + p.d + '"/>' + (dots ? '<path class="pd" d="' + dots + '"/>' : "") + "</svg>";
  }

  function drawHistory(tk, i) {
    var host = document.getElementById("ld-hist");
    if (!host) return;
    if (noHistory(tk)) {
      host.innerHTML = '<div class="ld-sec-h"><h2 class="ld-h2" id="ld-hi-h">Daily history</h2></div><p class="ld-note">We have no daily history for ' + esc(tk) + ".</p>";
      return;
    }
    Promise.all([getJSON("history/" + tickerFile(tk)), histUniverse()]).then(function (got) {
      if (parseCompanyHash().tk && parseCompanyHash().tk !== tk) return;
      var H = got[0], U = got[1];
      if (!H || !H.d || !H.d.length) {
        host.innerHTML = '<div class="ld-sec-h"><h2 class="ld-h2" id="ld-hi-h">Daily history</h2></div><p class="ld-note">We have no daily history for ' + esc(tk) + ".</p>";
        return;
      }
      renderHistory(host, tk, i, H, U);
    });
  }

  function renderHistory(host, tk, i, H, U) {
    if (!cst.hkey) cst.hkey = "price";
    if (!cst.hmode) cst.hmode = "value";
    var first = U && U.d.length ? U.d[0] : H.d[0];
    var kind = S.kind[i];
    var groups = metricsByGroup();
    var opts = '<optgroup label="Price"><option value="price">Price</option></optgroup>' + GROUPS.map(function (g) {
      return '<optgroup label="' + esc(g) + '">' + groups[g].map(function (m) {
        return '<option value="' + m.key + '">' + esc(m.label) + "</option>";
      }).join("") + "</optgroup>";
    }).join("");
    var total = U ? U.d.length : H.d.length;
    host.innerHTML = '<div class="ld-sec-h"><h2 class="ld-h2" id="ld-hi-h">Daily history</h2><span class="ld-kicker">' + H.d.length + " " + (H.d.length === 1 ? "day" : "days") + " of data, " + dateMid(H.d[0]) + " to " + dateMid(H.d[H.d.length - 1]) + "</span></div>" +
      '<p class="ld-hnote">Our daily record began on ' + dateMid(first) + " and covers " + total + " " + (total === 1 ? "day" : "days") + " so far. " + esc(tk) + " appears on " +
      (H.d.length === total ? (total === 1 ? "that day" : "all of them") : H.d.length + " of them") + "." +
      (H.d.length < 30 ? " That is too few days to show a trend." : "") + " A gap in a line is a day with no figure: the price was out of date that day, the figure does not apply, or nothing was recorded. Each day's z-score compares " + esc(tk) + " with all operating companies on that day.</p>" +
      '<div class="ld-hctl"><label class="ld-kicker" for="ld-hk">Metric</label><select id="ld-hk" class="ld-select ld-hsel">' + opts + "</select>" +
      '<div class="ld-seg" role="group" aria-label="Show as"><button type="button" data-hm="value">Value</button><button type="button" data-hm="z">z-score</button></div>' +
      '<div class="ld-seg" role="group" aria-label="Time range" id="ld-hr"></div></div>' +
      '<div class="ld-hread ld-num" id="ld-hread" aria-live="polite"></div>' +
      '<div class="ld-chart ld-hchart" id="ld-hchart"></div>' +
      '<p class="ld-muted ld-hfoot" id="ld-hfoot"></p>' +
      '<div class="ld-hsm" id="ld-hsm"></div>';
    var sel = host.querySelector("#ld-hk");
    function paint() {
      var k = cst.hkey, isPrice = k === "price";
      var mode = isPrice ? "value" : cst.hmode;
      sel.value = k;
      host.querySelectorAll("[data-hm]").forEach(function (b) {
        var m = b.getAttribute("data-hm");
        b.setAttribute("aria-pressed", String(m === mode));
        if (m === "z") { b.disabled = isPrice; if (isPrice) b.title = "Price has no z-score"; else b.removeAttribute("title"); }
      });
      var ser = histSeries(H, U, k, mode);
      var rg = host.querySelector("#ld-hr"), avail = HIST_RANGES.filter(function (r) { return r[1] < ser.v.length; });
      if (cst.hrange && !avail.some(function (r) { return r[1] === cst.hrange; })) cst.hrange = 0;
      rg.hidden = !avail.length;
      rg.innerHTML = avail.length ? avail.map(function (r) { return '<button type="button" data-hr="' + r[1] + '" aria-pressed="' + (cst.hrange === r[1]) + '">' + r[0] + "</button>"; }).join("") +
        '<button type="button" data-hr="0" aria-pressed="' + !cst.hrange + '">All</button>' : "";
      var chart = host.querySelector("#ld-hchart"), foot = host.querySelector("#ld-hfoot");
      var have = ser.v.filter(function (v) { return v != null; }).length;
      var cnt = { stale: 0, na: 0, none: 0, noz: 0 }, nFill = 0;
      ser.why.forEach(function (w, j) { if (ser.fill[j]) nFill++; else if (w) cnt[w]++; });
      var nGap = ser.gap.filter(function (g) { return g; }).length;
      if (!have) {
        chart.innerHTML = '<p class="ld-note">' + esc(histLabel(k)) + (cnt.na === ser.v.length ? " " + esc(naWhy(kind)) + "." : " has no figure on any day.") + "</p>";
        setHistRead("");
      } else {
        histChart(chart, ser, histLabel(k));
      }
      var bits = [];
      function dayN(c) { return c + (c === 1 ? " day" : " days"); }
      if (cnt.stale) bits.push(dayN(cnt.stale) + " with an out-of-date price");
      if (cnt.na) bits.push(dayN(cnt.na) + " where it does not apply");
      if (cnt.none) bits.push(dayN(cnt.none) + " not recorded");
      if (cnt.noz) bits.push(dayN(cnt.noz) + " without a z-score");
      foot.textContent = have ? have + " of " + dayN(ser.v.length) + " " + (have === 1 ? "has" : "have") + " a figure" + (bits.length ? ". Missing: " + bits.join(", ") : "") + "." +
        (nFill ? " Open rings mark " + dayN(nFill) + " when our daily record had an out-of-date price; the close shown is the one stored for that date in the price history." : "") +
        (nGap ? " The change on the day is not available for " + dayN(nGap) + ", because the stored prices were missing the session before." : "") +
        (mode === "z" ? " Z-scores are measured from the median of each day's companies and capped at " + String.fromCharCode(177) + "5" + SIGMA + (metric(k) && metric(k).transform === "log10" ? "; this one is worked out on a log scale" : "") + "." : "") : "";
      var sm = host.querySelector("#ld-hsm");
      sm.innerHTML = HIST_DEFAULT.map(function (key) {
        var md = key === "price" ? "value" : cst.hmode, s = histSeries(H, U, key, md);
        var li = s.v.length - 1; while (li >= 0 && s.v[li] == null) li--;
        var big = li < 0 ? "n/a" : md === "z" ? zTxt(s.v[li]) : histFmt(key, s.x[li]);
        var allNa = s.why.every(function (w) { return w === "na"; });
        var pic = li < 0 ? '<span class="ld-hsmna">' + (allNa ? "Does not apply" : "No figures") + "</span>" : histSpark(s);
        return '<button type="button" class="ld-sm ld-hsmb" data-hk="' + key + '" aria-pressed="' + (key === k) + '"><span class="ld-kicker">' + esc(histLabel(key)) + (md === "z" ? " z-score" : "") + '</span><span class="v">' + esc(big) + "</span>" + pic +
          '<span class="f"><span>' + esc(dateShort(s.d[0])) + "</span><span>" + (li >= 0 ? esc(dateShort(s.d[li])) : "") + "</span></span></button>";
      }).join("");
    }
    sel.addEventListener("change", function () { cst.hkey = sel.value; paint(); });
    host.addEventListener("click", function (e) {
      var b = e.target.closest("[data-hm],[data-hr],[data-hk]");
      if (!b || b.disabled) return;
      if (b.hasAttribute("data-hm")) cst.hmode = b.getAttribute("data-hm");
      else if (b.hasAttribute("data-hr")) cst.hrange = +b.getAttribute("data-hr");
      else cst.hkey = b.getAttribute("data-hk");
      paint();
    });
    paint();
    redrawers.push(function () { if (host.isConnected) paint(); });
  }
  function noHistory(tk) {
    var h = CFG.have || {};
    if (!h.noHistory) return false;
    if (!NOHIST) { NOHIST = {}; h.noHistory.forEach(function (t) { NOHIST[t] = 1; }); }
    return !!NOHIST[tk];
  }
  var NOHIST = null;

  function businessBlock(tk, det) {
    if (!det) return "<div></div>";
    var peer = det.peer_share, biz = det.business || {}, segs = det.segments;
    var text = biz.excerpt ? dashFree(biz.excerpt) : "";
    return '<section class="ld-sec" aria-labelledby="ld-bus-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bus-h">What it does</h2>' +
      (biz.filed ? '<span class="ld-kicker">From the annual report (10-K) filed ' + dateMid(biz.filed) + "</span>" : "") + "</div>" +
      (text ? '<p class="ld-bus">' + esc(text.slice(0, 900)) + (text.length > 900 ? "…" : "") + "</p>" : '<p class="ld-note">No business description available.</p>') +
      (segs && segs.names && segs.names.length ? '<p style="font-size:14px;color:var(--ink2);margin:8px 0 0"><span class="ld-kicker">Business segments</span> ' + esc(segs.names.map(dashFree).join(", ")) + (segs.as_of ? " (from the " + esc(segs.form || "filing") + " filed " + dateMid(segs.as_of).replace(/ /g, "\u00a0") + ")" : "") + "</p>" : "") +
      (peer && peer.share != null ? '<div class="ld-kicker" style="margin-top:16px">Share of sub-industry revenue</div><div class="ld-share"><i style="width:' + (peer.share * 100).toFixed(1) + '%"></i></div>' +
        '<p style="font-size:13.5px;color:var(--ink2);margin:0">' + esc(tk) + "’s revenue over the past year was " + num(peer.share * 100, 1) + "% of the total for the " + peer.n + " " + (peer.n === 1 ? "company" : "companies") + " in " + esc(peer.group) + (peer.rank === 1 ? ", the largest share of any" : ", ranking " + peer.rank + " of " + peer.n) + ". This is not market share: the group is an industry label, not a market.</p>" : "") +
      "</section>";
  }

  /* The filings table's Section column, in words (the pipeline's _DOC_KIND_WORDS, shortened for a cell). */
  var DOC_KIND = { business: "business description", risk_factors: "risk factors", segment_note: "business segments",
                   earnings_release: "results announcement", mdna: "management discussion" };
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
      hist = '<section class="ld-sec" aria-labelledby="ld-rh-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-rh-h">Reported history</h2><span class="ld-kicker">' + an.length + " years as reported, " + y0 + " to " + y1 + "</span></div>" +
        '<div class="ld-hist4">' + series.map(function (s) {
          var vals = an.map(function (r) { return r[s[1]]; });
          var fi = vals.findIndex(function (v) { return v != null && isFinite(v); });
          var li = vals.length - 1; while (li >= 0 && (vals[li] == null || !isFinite(vals[li]))) li--;
          if (fi < 0 || li <= fi) return '<div class="ld-sm"><div class="ld-kicker">' + s[0] + '</div><p class="ld-muted" style="font-size:13px">Not reported.</p></div>';
          return '<div class="ld-sm"><div class="ld-kicker">' + s[0] + '</div><div class="v">' + s[2](vals[li]) + "</div>" + sparkSvg(vals) +
            '<div class="f"><span>' + String(an[fi].period_end).slice(0, 4) + " " + s[2](vals[fi]) + "</span><span>" + String(an[li].period_end).slice(0, 4) + "</span></div></div>";
        }).join("") + "</div>" +
        '<p class="ld-muted" style="font-size:13px;margin-top:14px">Figures for the whole company, from the machine-readable data (XBRL) in its SEC filings. A gap in a line is a year the figure was missing from that data, not a year the company had none. Diluted EPS is earnings per share, counting the extra shares that options and convertible securities could create. Free cash flow is cash from operations minus capital spending.</p></section>';
    }
    var fl = det && det.filings || [];
    var filings = fl.length ? '<section class="ld-sec" aria-labelledby="ld-fi-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-fi-h">Filings collected</h2><span class="ld-kicker">' + fl.length + " " + (fl.length === 1 ? "document" : "documents") + "</span></div>" +
      '<div class="ld-tbl-wrap"><table class="ld-mini"><thead><tr><th>Filed</th><th>Form</th><th>Section</th><th class="r">Characters</th></tr></thead><tbody>' +
      fl.map(function (f) {
        return '<tr style="cursor:default"><td class="ld-num">' + esc(f.filed) + '</td><td class="ld-num">' + esc(f.form) + "</td><td>" + esc(DOC_KIND[f.doc_kind] || String(f.doc_kind || "").replace(/_/g, " ")) + '</td><td class="r ld-num">' + (f.text_chars ? int(f.text_chars) : "n/a") + "</td></tr>";
      }).join("") + "</tbody></table></div></section>"
      : '<section class="ld-sec"><div class="ld-sec-h"><h2 class="ld-h2">Filings collected</h2></div><p class="ld-note">' +
        (det ? "No filings collected yet." : "We collect filings and reported figures for " + int(CFG.have && CFG.have.company ? CFG.have.company.length : 0) + " companies so far: those we are researching and those the daily screen has picked out. " + esc(tk) + " is not one of them yet.") + "</p></section>";
    var items = (Array.isArray(news) ? news : news && (news.items || news.news) || []).slice(0, 10);
    var newsHTML = items.length ? '<section class="ld-sec" aria-labelledby="ld-nw-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-nw-h">Recent headlines</h2><span class="ld-kicker">Tone from ' + MINUS + "1 (negative) to +1 (positive)</span></div>" +
      '<ul class="ld-news">' + items.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).map(function (n) {
        var v = n.vader, tone = v == null ? "no score" : v > 0.05 ? "positive" : v < -0.05 ? "negative" : "neutral";
        var bar = v == null ? "" : '<span class="ld-tone" aria-hidden="true"><i class="' + (v >= 0 ? "p" : "n") + '" style="' + (v >= 0 ? "left:50%;" : "right:50%;") + "width:" + (Math.min(1, Math.abs(v)) * 50).toFixed(1) + '%"></i></span>';
        var d = n.ts ? new Date(n.ts * 1000).toISOString().slice(0, 10) : "";
        return '<li><a class="h" href="' + esc(n.link) + '" target="_blank" rel="noopener">' + esc(dashFree(cleanHead(n.title || n.h || "", n.source))) + '</a><div class="m"><span class="ld-src">' + esc(srcTxt(n.source)) + (d ? " " + MID + " " + dateShort(d) : "") + "</span>" + bar +
          '<span class="ld-src" style="letter-spacing:.04em;text-transform:none">' + tone + (v == null ? "" : " " + signed(v, 2)) + "</span></div></li>";
      }).join("") + "</ul></section>" : '<section class="ld-sec"><div class="ld-sec-h"><h2 class="ld-h2">Recent headlines</h2></div><p class="ld-note">We found no recent headlines about ' + esc(tk) + ".</p></section>";
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
      if (hasT) below.push({ x: X(n.t), t: (v.open ? "target " : "reference target ") + money(n.t) });
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
      var svg = '<svg viewBox="0 0 ' + W + " " + H + '" height="' + H + '" role="img" aria-label="' + esc(r.ticker + ": " + callSentence(r)) + '">' +
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
    if (!r.last_close || !isFinite(n0.l) || !isFinite(n0.e)) { host.innerHTML = '<p class="ld-note">There has been no close since the note was written, so there is no price move to show.</p>'; return; }
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


  /* The figures a memo argues from, beside it. A memo is written as an argument and leaves the data to this
     page, so the latest values the site already holds for the company are listed under it, with a pointer to
     the reported history, the sector comparison and the most similar companies further down. */
  var REF_KEYS = ["market_cap", "pe", "ev_ebitda", "fcf_yield", "net_debt_ebitda", "revenue_growth_yoy",
                  "operating_margin", "gross_margin", "beta_1y", "volatility_1y"];
  function referenceData(tk, i) {
    if (i == null || i < 0) return "";
    var rows = REF_KEYS.filter(function (k) { return A.vals[k] && metric(k); }).map(function (k) {
      var v = valueOf(i, k);
      return '<li><span class="k">' + esc(metric(k).label) + '</span><span class="v">' + esc(v.blank ? v.why : v.txt) + "</span></li>";
    }).join("");
    if (!rows) return "";
    return '<details class="ld-det"><summary>Reference data</summary><div class="ld-rblock">' +
      '<ul class="ld-facts">' + rows + "</ul>" +
      '<p class="ld-muted" style="font-size:13px;margin:10px 0 0">The latest figures we hold for ' + esc(tk) + ", which may be newer than the memo. The reported history, how each figure compares with the sector, and the most similar companies are further down this page.</p>" +
      "</div></details>";
  }

  /* The full thesis, placed on the company page. r is docs/thesis/TICKER.json: the current view's scalars at
     the top, every note newest first in notes[], and last_close. Earlier notes stay readable below. */
  function thesisSection(tk, r, i) {
    if (!r) return '<p class="ld-nothesis" id="ld-thesis">There is no investment thesis for ' + esc(tk) + " yet.</p>";
    var v = viewOf(r), n = callNums(r), note = r.notes && r.notes[0] || {};
    var conv = parseInt(r.conviction, 10) || 0;
    var days = r.review_by ? daysBetween(todayISO(), r.review_by) : NaN;
    var lc = r.last_close;
    var facts = '<ul class="ld-facts">' +
      '<li><span class="k">Price when written</span><span class="v">' + money(n.e) + (r.written_on ? " on " + dateMid(r.written_on) : "") + "</span></li>" +
      (lc && isFinite(n.l) ? '<li><span class="k">Last close</span><span class="v">' + money(n.l) + " on " + dateMid(lc.date) + (isFinite(n.move) ? ' <span class="' + chgCls(n.move * 100) + '">(' + signed(n.move * 100, 1) + "%)</span>" : "") + "</span></li>" : '<li><span class="k">Last close</span><span class="v">None since</span></li>') +
      (v.open && isFinite(n.t) ? '<li><span class="k">Target</span><span class="v">' + money(n.t) + (isFinite(n.need) ? " (" + signed(n.need * 100, 1) + "% from written" + (isFinite(n.l) ? ", " + signed((n.t / n.l - 1) * 100, 1) + "% from last close" : "") + ")" : "") + "</span></li>"
        : isFinite(n.t) ? '<li><span class="k">Reference target</span><span class="v">' + money(n.t) + " (reference only)</span></li>" : '<li><span class="k">Target</span><span class="v">None</span></li>') +
      (v.open && isFinite(n.w) ? '<li><span class="k">View is wrong at</span><span class="v">about ' + money(n.w) + "</span></li>" : "") +
      (r.review_by ? '<li><span class="k">Review by</span><span class="v">' + dateMid(r.review_by) + " (" + (days < 0 ? "overdue by " + plural(-days, "day", "days") : days === 0 ? "today" : plural(days, "day", "days") + " from " + dateShort(todayISO())) + ")</span></li>" : "") +
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
        '<div class="ld-notebody">' + (o.key_claim ? '<p class="ld-claim" style="font-size:18px">' + esc(dashFree(o.key_claim)) + "</p>" : "") + (body || '<p class="ld-muted">The full text of this note was not published.</p>') + "</div></details>";
    }).join("");
    var src = r.note_path && CFG.repoUrl ? ' <a class="ld-inl" href="' + esc(CFG.repoUrl + r.note_path) + '" target="_blank" rel="noopener">source note</a>' : "";
    return '<section class="ld-sec ld-thesis" id="ld-thesis" aria-labelledby="ld-th-h">' +
      '<div class="ld-sec-h"><h2 class="ld-h2" id="ld-th-h">Investment thesis</h2><span class="ld-kicker">' + esc(r.kind || "note") + (r.written_on ? " " + MID + " written " + dateMid(r.written_on) : "") + " " + MID + " " + v.status + (days < 0 && v.open ? " " + MID + " review overdue" : "") + "</span></div>" +
      '<div class="ld-call-view' + (v.open ? " ld-view open" : "") + '">' + esc(v.label) + "</div>" +
      '<div><span class="ld-conv" aria-hidden="true">' + [1, 2, 3, 4, 5].map(function (k) { return '<i class="' + (k <= conv ? "on" : "") + '"></i>'; }).join("") + '</span><span class="ld-kicker">Conviction ' + conv + " of 5</span></div>" +
      (r.key_claim ? '<p class="ld-claim">' + esc(dashFree(r.key_claim)) + "</p>" : "") +
      '<div class="ld-call-grid"><div>' +
      '<div class="ld-kicker" style="margin-top:4px">' + (v.open ? "Price since the note, against the target" : "Price since the note, with the target for reference") + "</div>" +
      '<div class="ld-prog" data-prog="' + esc(r.ticker || tk) + '"></div>' +
      '<p style="font-size:14px;color:var(--ink2);margin:6px 0 0">' + esc(callSentence(r)) + "</p>" +
      '<div class="ld-rblock">' + (r.falsifier ? "<h4>What would prove it wrong</h4><p>" + esc(dashFree(r.falsifier)) + "</p>" : "") +
      (conds ? "<h4>What has to stay true</h4><ul>" + conds + "</ul>" : "") +
      (note.add_if ? "<h4>What would make the view stronger</h4><p>" + esc(dashFree(note.add_if)) + "</p>" : "") +
      (note.since_last_note ? "<h4>What changed since the last note</h4><p>" + esc(cap1(dashFree(note.since_last_note)).replace(/([^.!?])\s*$/, "$1.")) + "</p>" : "") +
      "</div></div>" +
      "<div>" + facts + (hist ? '<div class="ld-rblock"><h4>History of the call</h4><ul class="ld-hist-l">' + hist + "</ul></div>" : "") + "</div></div>" +
      ((r.data_caveats || []).length ? '<details class="ld-det"><summary>What the data cannot tell us (' + plural(r.data_caveats.length, "caveat", "caveats") + ")</summary><div class=\"ld-rblock\"><ul>" + r.data_caveats.map(function (c) { return "<li>" + esc(dashFree(c)) + "</li>"; }).join("") + "</ul></div></details>" : "") +
      (secs ? '<details class="ld-det"><summary>Read the full note (' + plural((note.sections || []).length, "section", "sections") + ")</summary><div class=\"ld-notebody\">" + secs + "</div></details>" : "") +
      referenceData(tk, i) +
      older +
      (src ? '<p class="ld-muted" style="font-size:13px;margin:12px 0 0">' + (r.note_count > 1 ? r.note_count + " notes on " + esc(tk) + " so far. " : "") + "Read the" + src + " in the project's repository.</p>" : "") +
      "</section>";
  }

  /* Research: an index of theses. Each row leads to the thesis on its company page. CFG.research comes from
     _research_views, the same source as the thesis files; CFG.record is the track record. */
  var STATUS_WORD = CFG.statusWords || { open: "Open call", watching: "Watching", graded: "Checked", due: "Review overdue" };
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
        '<td class="r ld-num">' + (isFinite(n.t) ? money(n.t) + (v.open ? "" : ' <span class="ref" title="' + esc("For reference only: the view is “" + v.label + "”") + '">ref.</span>') : "None") + "</td>" +
        '<td class="r ld-num">' + (isFinite(toT) ? signed(toT, 1) + "%" : "n/a") + "</td>" +
        '<td class="ld-num">' + (r.review_by ? dateMid(r.review_by) : "None") + "</td>" +
        '<td class="ld-num">' + (r.written_on ? dateMid(r.written_on) : "") + "</td></tr>";
    }).join("");
    main.innerHTML = '<div class="ld-wrap">' +
      '<div class="ld-head"><div><div class="ld-kicker"><b>Research</b> ' + MID + " " + plural(calls.length, "thesis", "theses") + (closeDates.length ? " " + MID + " prices of " + esc(andList(closeDates.map(dateShort))) : "") + "</div>" +
      '<h1 class="ld-h1">Research</h1><p class="ld-deck">Each thesis is a written view on one company, with a target price, a date to review it, and what would prove it wrong. The full thesis is on the company’s page, beside the price and the figures it draws on.</p></div></div>' +
      '<section class="ld-sec" aria-labelledby="ld-ri-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-ri-h">Every thesis</h2><span class="ld-kicker">Open calls first</span></div>' +
      (calls.length ? '<div class="ld-tbl-wrap"><table class="ld-rtab"><thead><tr><th>Ticker</th><th>Company</th><th>View</th><th>Conviction</th><th class="r">Written at</th><th class="r">Last close</th><th class="r">Target</th><th class="r">To target</th><th>Review by</th><th>Written on</th></tr></thead><tbody>' +
      rows + "</tbody></table></div>" : '<p class="ld-empty">No thesis has been written yet.</p>') +
      '<p class="ld-muted" style="font-size:13.5px;margin:12px 0 0;max-width:90ch">“To target” is how far the price still has to move, from the last close, to reach the target. When the view takes no side (“' + esc(VIEW.watch) + '” or “' + esc(VIEW["no view"]) + '”), the target is for reference only and the company shows as Watching. Those notes make no call, so they are never checked.</p></section>' +
      recordHTML(CFG.record) + analystInstructionsHTML(CFG.howAnalyst) + portfoliosLinkHTML() + "</div>";
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
    var tiles = [["Companies covered", rec.tickers], ["Open calls", rec.open], ["Watching, no call", rec.watching], ["Calls checked", rec.graded]];
    var tiers = (rec.tiers || []).map(function (t) {
      return "<tr><td class=\"ld-num\">" + esc(t.conviction) + '</td><td class="r ld-num">' + (t.n || 0) + '</td><td class="r ld-num">' + (t.open || 0) + '</td><td class="r ld-num">' + (t.watching || 0) + '</td><td class="r ld-num">' + (t.graded || 0) + '</td><td class="r ld-muted">Not yet</td><td class="r ld-muted">Not yet</td></tr>';
    }).join("") || '<tr><td colspan="7" class="ld-muted">No notes yet.</td></tr>';
    var verdict = rec.scored ? "" : '<p class="ld-note">No call has reached its target date yet, so the last two columns say “Not yet” and there is no telling yet how good these calls are.' +
      (rec.earliest_maturity ? " The first comes due on " + dateMid(rec.earliest_maturity) + "." : "") + " Until then, this page records what was said and when.</p>";
    return '<section class="ld-sec" aria-labelledby="ld-rec-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-rec-h">Track record</h2><span class="ld-kicker">' + plural(rec.notes || 0, "note", "notes") + "</span></div>" +
      '<div class="ld-stats" style="grid-template-columns:repeat(4,minmax(0,1fr))">' + tiles.map(function (t) {
        return '<div class="ld-stat" style="display:block"><div class="ld-kicker">' + esc(t[0]) + '</div><div class="v">' + int(t[1] || 0) + "</div></div>";
      }).join("") + "</div>" +
      '<div class="ld-tbl-wrap"><table class="ld-rtab" style="margin-top:18px"><thead><tr><th>Conviction</th><th class="r">Companies</th><th class="r">Open</th><th class="r">Watching</th><th class="r">Checked</th><th class="r">Right</th><th class="r">Beat similar companies</th></tr></thead><tbody>' + tiers + "</tbody></table></div>" +
      verdict +
      '<p class="ld-muted" style="font-size:13.5px;margin:12px 0 0;max-width:90ch">Each call is checked on its target date: was it right, and did the shares do better than similar companies over the same months? The table splits the calls by conviction, from 0 to 5, because conviction is only worth recording if the confident calls turn out better than the cautious ones.</p></section>';
  }
  /* Research ends with a pointer to the model portfolios, which have their own pages. */
  function portfoliosLinkHTML() {
    return '<section class="ld-sec" aria-labelledby="ld-pfl-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-pfl-h">Model portfolios</h2></div>' +
      '<p class="ld-pm-p">Eight paper portfolios: six that each hold one size and style of company, a hedge fund strategy, and a book where the portfolio manager has a free hand. ' +
      '<a class="ld-inl" href="' + ctx.href("portfolios") + '">See the portfolios</a>.</p></section>';
  }

  /* ---------------------------------------------------------------- portfolios */
  /* portfolios.html (one card per book) and book.html#ID (one book). CFG.portfolios comes from
     portfolio.engine.site_data. Every figure is from the ledger and the stored closes; where a close is
     missing the value is left blank and the day is called partial, never filled from another day. */

  var PM = CFG.portfolios || {};
  var SIZE_WORD = { large: "Large", mid: "Mid", small: "Small" };
  var STYLE_WORD = { growth: "Growth", value: "Value" };
  var ACTION_WORD = { inception: "Inception", rebalance: "Rebalance", hold: "Hold", trade: "Trade", mandate_change: "Mandate change" };
  var SIDE_WORD = { deposit: "Cash in", withdraw: "Cash out", buy: "Buy", sell: "Sell", short: "Sell short", cover: "Buy to cover" };

  function pctTxt(f, dp) { return f == null || !isFinite(f) ? "n/a" : signed(f * 100, dp == null ? 2 : dp) + "%"; }
  function pctCls(f) { return f == null || !isFinite(f) ? "" : chgCls(f * 100); }
  function plainPct(f, dp) { return f == null || !isFinite(f) ? "n/a" : num(f * 100, dp == null ? 1 : dp) + "%"; }
  function usd0(v) { return v == null || !isFinite(v) ? "n/a" : (v < 0 ? MINUS : "") + "$" + int(Math.abs(v)); }
  function bookHref(id) { return "book.html#" + encodeURIComponent(id); }
  function benchShort(b) { return String(b.benchmarkName || b.benchmark || "").replace(/\s*\(.*\)$/, ""); }
  function inCash(b) { return b.inception && !(b.holdingsCount > 0); }
  function isStyle(b) { return b.kind === "style"; }

  function priceNote() {
    return '<p class="ld-note">Returns are price-only: dividends are not counted, in the books or in the benchmarks. Every trade costs 5 basis points of its value (a basis point is one hundredth of a percent, so 5 is 0.05%). ' +
      "A holding is valued at the stored close for that day only. When a close is missing, the day is marked partial and its value is left blank rather than estimated.</p>";
  }

  /* The instructions the agents run on: the claude.ai routine's prompt (mirrored in theses/routines/ and
     portfolio/routines/) and the PROMPTS.md section it points to. The pipeline renders them from the files in
     the repository, escaping everything first; they are printed here as they stand. */
  function repoLink(path) {
    return CFG.repoUrl ? '<a class="ld-inl" href="' + esc(CFG.repoUrl + path) + '" target="_blank" rel="noopener">' + esc(path) + "</a>" : esc(path);
  }
  function docBlock(summary, lede, html) {
    return '<details class="ld-det ld-doc"><summary>' + esc(summary) + '</summary><div class="ld-notebody ld-docbody">' +
      (lede ? '<p class="ld-doc-src">' + lede + "</p>" : "") + cleanHtml(html) + "</div></details>";
  }
  function analystInstructionsHTML(d) {
    if (!d) return "";
    var r = d.routine;
    return '<section class="ld-sec" aria-labelledby="ld-how-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-how-h">How the analyst works</h2></div>' +
      '<p class="ld-pm-p">The analyst is a scheduled claude.ai routine' + (r && r.schedule ? " that runs " + esc(r.schedule) : "") + ". Its prompt is short and points to the full instructions in the repository, which say what to read, how to write a memo and how to record it. Both are printed below as they stand in the repository, so a change to either shows here after the next build.</p>" +
      (r ? docBlock("The routine prompt", "From " + repoLink(d.routinePath) + (r.schedule ? ". Runs " + esc(r.schedule) + "." : "."), r.html) : "") +
      (d.prompt ? docBlock("The full instructions", "From " + repoLink(d.promptPath) + ", the section “" + esc(d.promptSection) + "”.", d.prompt) : "") +
      "</section>";
  }
  function pmInstructionsHTML(d) {
    if (!d) return "";
    var shared = d.prompt ? docBlock("The instructions every PM follows", "From " + repoLink(d.promptPath) + ", the section “" + esc(d.promptSection) + "”. Each routine follows it for its own books only.", d.prompt) : "";
    var blocks = (d.pms || []).map(function (p) {
      var when = p.schedule || "Not scheduled yet";
      return '<details class="ld-det ld-doc"><summary>' + esc(p.name) + " " + MID + " " + esc(when) + '</summary><div class="ld-notebody ld-docbody">' +
        '<p class="ld-doc-src">Runs ' + esc(p.books.charAt(0).toLowerCase() + p.books.slice(1)) + ". Schedule: " + esc(when.charAt(0).toLowerCase() + when.slice(1)) + ". The routine prompt, from " + repoLink(p.path) + ":</p>" +
        cleanHtml(p.html) + "</div></details>";
    }).join("");
    var briefs = (d.briefs || []).map(function (b) {
      return docBlock(b.name, "For " + esc(b.books) + ". From " + repoLink(b.path) + ".", b.html);
    }).join("");
    return '<section class="ld-sec" aria-labelledby="ld-pmhow-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-pmhow-h">How the PMs work</h2></div>' +
      '<p class="ld-pm-p">Two PMs run the books. The Style PM runs the six style books and the hedge book; the Neural PM runs the neural book on its own, so its reasoning does not lean on the others. Each is a scheduled claude.ai routine with a short prompt that points to one shared process, and each book has a brief that says how a manager of that kind of book thinks. The analyst’s memos give no position size: the PM sizes every position. A PM trades only through portfolio/bin/trade.py, which checks each order against the book’s limits and writes it to the ledger. The prompts, the process and the briefs are printed as they stand in the repository.</p>' +
      blocks + shared + briefs + "</section>";
  }

  function boxTableHTML(counts) {
    var head = "<tr><th></th>" + ["value", "growth"].map(function (s) { return '<th class="r">' + STYLE_WORD[s] + "</th>"; }).join("") + "</tr>";
    var body = ["large", "mid", "small"].map(function (z) {
      return "<tr><td>" + SIZE_WORD[z] + "</td>" + ["value", "growth"].map(function (s) {
        return '<td class="r ld-num">' + int(counts[z + "-" + s] || 0) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    return '<div class="ld-tbl-wrap"><table class="ld-rtab ld-pm-tab ld-pm-box"><thead>' + head + "</thead><tbody>" + body + "</tbody></table></div>";
  }

  function renderPortfolios(main) {
    var books = PM.books || [];
    var live = books.filter(function (b) { return b.inception; }).length;
    var counts = PM.boxCounts || {}, cov = PM.coverage || {};
    var rule = (PM.classificationRule || []).map(function (p) { return '<p class="ld-pm-p">' + esc(p) + "</p>"; }).join("");
    var planned = (PM.planned || []).map(function (b) { return esc(b.name); });
    var drafts = (CFG.drafts || []).map(function (d) {
      return "<li>" + (CFG.repoUrl ? '<a class="ld-inl" href="' + esc(CFG.repoUrl + d.path) + '" target="_blank" rel="noopener">' + esc(d.label) + "</a>" : esc(d.label)) + "</li>";
    }).join("");
    main.innerHTML = '<div class="ld-wrap">' +
      '<div class="ld-head"><div><div class="ld-kicker"><b>Portfolios</b> ' + MID + " " + plural(live, "book started", "books started") + (PM.asof ? " " + MID + " as of the close on " + esc(dateMid(PM.asof)) : "") + "</div>" +
      '<h1 class="ld-h1">Model portfolios</h1><p class="ld-deck">Eight paper portfolios, each started with $' + int(PM.capital || 1000000) + " of pretend cash. Six hold one size and style of company, are chosen by written rules and reviewed by the portfolio manager (PM), and are measured against the matching Russell index fund. The other two are the PM’s own: a hedge fund strategy that bets on some companies and against others, and a book where the PM has a free hand.</p></div></div>" +
      priceNote() +
      '<section class="ld-sec" aria-labelledby="ld-pm-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-pm-h">The books</h2><span class="ld-kicker">One column per book, one card per holding</span></div>' +
      '<p class="ld-pm-p">Each column is one book: its value, its return against its benchmark, and a card for each company it holds. The PMs trade only through the ledger, and every figure here is worked out from that ledger and the stored closing prices. Open a book from its heading, or a company from its card. A bet against a company (a short position) has a red edge.</p>' +
      (CFG.board ? cleanHtml(CFG.board) : '<p class="ld-empty">The board could not be built on this run.</p>') +
      '<p class="ld-pm-p ld-muted">The hedge fund strategy may hold up to 200% of its value in positions, counting bets against companies at their size, and must keep its net position (what it owns minus what it has bet against) between minus 20% and plus 60% of its value. It is also compared with what its cash would have earned in Treasury bills. The free hand book may do anything short of positions worth more than twice its value, and works only from the data in this project, with no internet.</p></section>' +
      '<section class="ld-sec" aria-labelledby="ld-pmc-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-pmc-h">How companies are sorted into the six boxes</h2></div>' + rule +
      '<p class="ld-pm-p">A robust z-score says how far a figure sits from the middle of the group: the median, measured in units of the typical spread around it, and capped at 5 either way so a single extreme figure cannot stretch the scale.</p>' +
      '<p class="ld-pm-p">Companies in each box on ' + (PM.asof ? esc(dateMid(PM.asof)) : "the latest date") + ":</p>" + boxTableHTML(counts) +
      '<p class="ld-pm-p ld-muted">' + int(cov.with_growth || 0) + " of the " + int(cov.sized || 0) + " companies ranked by size have a three-year growth figure so far. The figures are read from each company’s annual reports as the weekly pass over SEC filings reaches it. Left out altogether: " + int(counts.micro || 0) + " micro caps, and " + int(counts.unclassified || 0) + " listings that are not operating companies, are depositary shares or foreign filers, have no market cap, repeat another share class, or have no three-year history yet.</p></section>" +
      '<section class="ld-sec" aria-labelledby="ld-pmr-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-pmr-h">How the rules choose a book</h2></div>' +
      '<p class="ld-pm-p">' + esc(PM.candidateRule || "") + "</p>" +
      '<p class="ld-pm-p">The rules run again after every close and their choice is shown on each book’s page beside what the book holds. They do not trade. Each style book is bought from the rules on its first day; after that, only the PM trades, and it says why whenever it departs from the rules.</p></section>' +
      pmInstructionsHTML(CFG.howPms) +
      (planned.length ? '<section class="ld-sec" aria-labelledby="ld-pmp-h"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-pmp-h">Still to come</h2></div><p class="ld-pm-p">Core books for each size (the middle between growth and value), tax-managed books and a momentum book are planned but not built: ' + esc(andList(planned)) + ".</p></section>" : "") +
      (drafts ? '<details class="ld-det"><summary>Earlier draft</summary><p class="ld-pm-p">Before these books existed, the plan was a single portfolio built from the analyst’s notes. The draft is kept in the repository.</p><ul class="ld-pm-list">' + drafts + "</ul></details>" : "") +
      "</div>";
  }

  function navChart(host, b) {
    var pts = (b.series || []).filter(function (p) { return p.nav != null && p.capital; });
    if (pts.length < 2) {
      host.innerHTML = '<p class="ld-muted ld-pm-p">The chart starts once two closes have been recorded for this book.' + ((b.series || []).length ? " So far: " + plural(pts.length, "complete day", "complete days") + "." : "") + "</p>";
      return;
    }
    var b0 = null;
    pts.forEach(function (p) { if (b0 == null && p.bench) b0 = p.bench; });
    function draw() {
      var W = Math.max(280, host.clientWidth || 640), H = W < 520 ? 200 : 250;
      var pl = 4, pr = 58, pt = 10, pb = 26, iw = W - pl - pr, ih = H - pt - pb;
      var a = pts.map(function (p) { return (p.nav / p.capital - 1) * 100; });
      var c = pts.map(function (p) { return p.bench && b0 ? (p.bench / b0 - 1) * 100 : null; });
      var vs = a.concat(c.filter(function (v) { return v != null; }));
      var mn = Math.min.apply(null, vs.concat([0])), mx = Math.max.apply(null, vs.concat([0]));
      if (mx - mn < 0.5) { mn -= 0.25; mx += 0.25; }
      var step = niceStep(mx - mn, 4), y0 = Math.floor(mn / step) * step, y1 = Math.ceil(mx / step) * step;
      function X(j) { return pl + iw * j / Math.max(1, pts.length - 1); }
      function Y(v) { return pt + ih * (1 - (v - y0) / (y1 - y0)); }
      var g = [];
      for (var y = y0; y <= y1 + step / 2; y += step) {
        g.push('<line class="gl" x1="' + pl + '" x2="' + (pl + iw) + '" y1="' + Y(y).toFixed(1) + '" y2="' + Y(y).toFixed(1) + '"/>');
        g.push('<text x="' + (pl + iw + 8) + '" y="' + (Y(y) + 3.5).toFixed(1) + '">' + (y < 0 ? MINUS : y > 0 ? "+" : "") + num(Math.abs(y), step < 1 ? 1 : 0) + "%</text>");
      }
      function path(vals) {
        var d = "", pen = false;
        vals.forEach(function (v, j) { if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + X(j).toFixed(1) + " " + Y(v).toFixed(1); pen = true; });
        return d;
      }
      host.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" height="' + H + '" role="img" aria-label="' + esc("Return since inception, " + b.name + " and " + benchShort(b)) + '">' +
        g.join("") + '<path class="ln ld-pm-bench" d="' + path(c) + '"/><path class="ln" d="' + path(a) + '"/>' +
        '<text x="' + pl + '" y="' + (H - 8) + '">' + esc(dateShort(pts[0].date)) + '</text><text x="' + (pl + iw) + '" y="' + (H - 8) + '" text-anchor="end">' + esc(dateShort(pts[pts.length - 1].date)) + "</text></svg>";
    }
    draw();
    redrawers.push(draw);
  }

  function mandateHTML(m, b) {
    if (!m) return "";
    var items;
    if (isStyle(b)) {
      items = [
        ["Number of holdings", m.holdings_range ? m.holdings_range[0] + " to " + m.holdings_range[1] : "n/a"],
        ["Largest position", plainPct(m.max_position, 0) + " of the book"],
        ["Largest sector", plainPct(m.sector_cap, 0) + " of the book"],
        ["Cash", m.cash_band ? plainPct(m.cash_band[0], 0) + " to " + plainPct(m.cash_band[1], 0) + " of the book" : "n/a"],
        ["Turnover", m.turnover_budget != null ? "Up to " + plainPct(m.turnover_budget, 0) + " of the book a year, counting purchases or sales, whichever is smaller" : "n/a"],
        ["Bets against companies", "Not allowed"],
        ["Companies it may buy", "Only those in its own box"],
        ["Weighting", m.weighting === "equal" ? "Equal weight" : String(m.weighting || "")]
      ];
    } else if (b.kind === "hedge") {
      items = [
        ["Bets against companies", "Allowed"],
        ["Gross exposure", "Up to " + plainPct(m.gross_max, 0) + " of the book: what it owns plus the size of what it has bet against"],
        ["Net exposure", m.net_range ? "Between " + (m.net_range[0] < 0 ? "minus " : "") + plainPct(Math.abs(m.net_range[0]), 0) + " and plus " + plainPct(m.net_range[1], 0) + ": what it owns minus what it has bet against" : "n/a"],
        ["Largest position", plainPct(m.max_long_position, 0) + " owned, " + plainPct(m.max_short_position, 0) + " bet against"],
        ["Number of holdings", m.holdings_range_per_side ? m.holdings_range_per_side[0] + " to " + m.holdings_range_per_side[1] + " on each side" : "n/a"],
        ["Companies it may trade", "Operating companies in the panel"],
        ["Measured against", "The S&P 500, and cash in Treasury bills"]
      ];
    } else {
      items = [
        ["Freedom", "Any number of companies, any weights, bets against companies and cash"],
        ["Gross exposure", "Up to " + plainPct(m.gross_max, 0) + " of the book"],
        ["Data", "Only what is in this project; no internet"],
        ["Measured against", "The S&P 500"]
      ];
    }
    return '<ul class="ld-pflim">' + items.map(function (x) { return '<li><span class="n">' + esc(x[0]) + '</span><span class="t">' + esc(x[1]) + "</span></li>"; }).join("") + "</ul>";
  }

  function renderBook(main) {
    var id = decodeURIComponent((location.hash || "").replace(/^#/, "")).trim();
    var books = PM.books || [];
    var b = null;
    books.forEach(function (x) { if (x.id === id) b = x; });
    if (!b) {
      main.innerHTML = '<div class="ld-wrap"><div class="ld-head"><div><div class="ld-kicker"><b>Model portfolio</b></div><h1 class="ld-h1">Choose a book</h1></div></div>' +
        '<ul class="ld-pm-list">' + books.map(function (x) { return '<li><a class="ld-inl" href="' + bookHref(x.id) + '">' + esc(x.name) + "</a></li>"; }).join("") + "</ul></div>";
      return;
    }
    var back = '<p class="ld-pm-back"><a class="ld-link" href="' + ctx.href("portfolios") + '">All portfolios</a></p>';
    var deck;
    if (isStyle(b)) {
      deck = "Holds " + (b.size === "large" ? "large" : b.size === "mid" ? "mid-sized" : "small") + " companies " +
        (b.style === "growth" ? "whose growth over three years ranks above the median for their size, after taking away how cheap they are" : "whose shares are cheap for what the company has and earns, ranking below the median for their size on growth minus value") +
        ". Measured against the " + esc(b.benchmarkName) + ".";
    } else if (b.kind === "hedge") {
      deck = "Owns some companies and bets against others, chosen by the PM each week. Measured against the S&P 500 and against what its cash would have earned in Treasury bills.";
    } else {
      deck = "The PM’s free hand: any companies, any weights, bets against companies or cash, using only the data in this project. Measured against the S&P 500.";
    }
    if (!b.inception) {
      main.innerHTML = '<div class="ld-wrap">' + back + '<div class="ld-head"><div><div class="ld-kicker"><b>Model portfolio</b></div><h1 class="ld-h1">' + esc(b.name) + '</h1><p class="ld-deck">' + deck + " It has not started yet: it starts when its box holds enough companies with three years of annual figures.</p></div></div>" +
        (isStyle(b) ? candidateHTML(b, []) : "") +
        '<section class="ld-sec" aria-labelledby="ld-bk-man"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bk-man">Mandate</h2><span class="ld-kicker">Set by the PM</span></div>' + mandateHTML(b.mandate, b) + "</section></div>";
      return;
    }
    var cashW = b.cash != null && b.pricedNav ? b.cash / b.pricedNav : null;
    var tiles = [
      ["Value", b.nav != null ? usd0(b.nav) : "n/a", b.partial ? "Partial: left blank" : "At the close on " + dateMid(b.asof)],
      ["Return since " + dateShort(b.inception), pctTxt(b.ret), "After trading costs", pctCls(b.ret)],
      [benchShort(b), pctTxt(b.benchRet), b.benchRet == null ? "Closes not stored yet" : "Same dates, price-only", pctCls(b.benchRet)]
    ];
    if (b.cash_benchmark) tiles.push(["Cash in Treasury bills", pctTxt(b.cashRet), b.cashRet == null ? "Rates not stored for every day" : "Same dates", pctCls(b.cashRet)]);
    if (isStyle(b)) tiles.push(["Cash", plainPct(cashW), usd0(b.cash)]);
    else tiles.push(["Gross and net", b.gross == null ? "n/a" : plainPct(b.gross, 0) + " / " + plainPct(b.net, 0), "Owned plus bet against / owned minus bet against"]);
    var stats = '<div class="ld-stats ld-pm-stats">' + tiles.map(function (t) {
      return '<div class="ld-stat" style="display:block"><div class="ld-kicker">' + esc(t[0]) + '</div><div class="v ld-num ' + (t[3] || "") + '">' + esc(t[1]) + '</div><div class="d">' + esc(t[2]) + "</div></div>";
    }).join("") + "</div>";
    var showSide = !isStyle(b);
    var hold = (b.holdings || []).map(function (h) {
      return '<tr><td><a class="ld-tk" href="' + ctx.href("company", h.ticker) + '">' + esc(h.ticker) + "</a></td><td>" + esc(h.name) + "</td><td>" + esc(h.sector || "Unclassified") +
        (showSide ? "</td><td>" + (h.side === "short" ? "Bet against" : "Owned") : "") +
        '</td><td class="r ld-num">' + int(Math.abs(h.shares)) + '</td><td class="r ld-num">' + money(h.avgCost) + '</td><td class="r ld-num">' + money(h.close) +
        '</td><td class="r ld-num">' + usd0(h.value) + '</td><td class="r ld-num">' + plainPct(h.weight) + '</td><td class="r ld-num ' + pctCls(h.ret) + '">' + pctTxt(h.ret, 1) + "</td></tr>";
    }).join("");
    var unpriced = (b.unpriced || []).map(function (u) { return esc(u.ticker) + " (" + esc(u.why) + ")"; });
    var sectors = (b.sectors || []).map(function (s) {
      return '<div class="ld-pm-bar"><span class="n">' + esc(s[0]) + '</span><span class="b"><i style="width:' + Math.max(0.5, s[1] * 100).toFixed(1) + '%"></i></span><span class="v ld-num">' + plainPct(s[1]) + "</span></div>";
    }).join("");
    var decisions = (b.decisions || []).slice().reverse().map(function (d) {
      return '<tr><td class="ld-num">' + esc(dateMid(d.date)) + "</td><td>" + esc(ACTION_WORD[d.action] || d.action) + '</td><td class="ld-pm-wrap">' + esc(d.reason) + "</td><td>" + esc(d.author === "pm" ? "PM" : "Rules") + "</td></tr>";
    }).join("");
    var trades = (b.trades || []).map(function (t) {
      var cash = t.side === "deposit" || t.side === "withdraw";
      return '<tr><td class="ld-num">' + esc(dateMid(t.date)) + "</td><td>" + esc(SIDE_WORD[t.side] || t.side) + "</td><td>" + (cash ? "Cash" : esc(t.ticker)) +
        '</td><td class="r ld-num">' + (cash ? "" : int(+t.shares)) + '</td><td class="r ld-num">' + (cash ? "" : money(+t.price)) + '</td><td class="r ld-num">' + usd0(+t.shares * +t.price) + '</td><td class="r ld-num">' + (cash ? "" : money(+t.cost)) + "</td></tr>";
    }).join("");
    var holdSec = inCash(b)
      ? '<p class="ld-empty">All in cash. The PM builds this book at its next weekly run.</p>'
      : (hold ? '<div class="ld-tbl-wrap"><table class="ld-rtab ld-pm-tab"><thead><tr><th>Ticker</th><th>Company</th><th>Sector</th>' + (showSide ? "<th>Side</th>" : "") + '<th class="r">Shares</th><th class="r">' + (showSide ? "Price in" : "Bought at") + '</th><th class="r">Close</th><th class="r">Value</th><th class="r">Weight</th><th class="r">Return</th></tr></thead><tbody>' + hold + "</tbody></table></div>" : '<p class="ld-empty">No holdings valued on this date.</p>') +
        (unpriced.length ? '<p class="ld-pm-warn">Not valued on ' + esc(dateMid(b.asof)) + ": " + unpriced.join(", ") + ".</p>" : "") +
        '<p class="ld-muted ld-pm-p">' + (showSide ? "“Price in” is the average price at which a position was bought or, for a bet against a company, sold short. A bet against a company shows a negative value and gains when the price falls. " : "“Bought at” is the average price paid per share, before the trading cost. ") + "Weight is the share of the book’s value on " + esc(dateMid(b.asof)) + ".</p>";
    main.innerHTML = '<div class="ld-wrap">' + back +
      '<div class="ld-head"><div><div class="ld-kicker"><b>Model portfolio</b> ' + MID + " started " + esc(dateMid(b.inception)) + " with $" + int(b.capital || 0) + "</div>" +
      '<h1 class="ld-h1">' + esc(b.name) + '</h1><p class="ld-deck">' + deck + "</p></div></div>" +
      stats + priceNote() +
      '<section class="ld-sec" aria-labelledby="ld-bk-perf"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bk-perf">Return since inception</h2><span class="ld-kicker">Book, solid; benchmark, dashed</span></div><div class="ld-chart" id="ld-pm-chart"></div></section>' +
      '<section class="ld-sec" aria-labelledby="ld-bk-hold"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bk-hold">Holdings</h2><span class="ld-kicker">' + plural(b.holdingsCount || 0, "company", "companies") + ", largest first</span></div>" + holdSec + "</section>" +
      (sectors ? '<section class="ld-sec" aria-labelledby="ld-bk-sec"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bk-sec">Sector mix</h2>' + (showSide ? '<span class="ld-kicker">Companies owned</span>' : "") + '</div><div class="ld-pm-bars">' + sectors + "</div></section>" : "") +
      (isStyle(b) ? candidateHTML(b, b.holdings || []) : "") +
      '<section class="ld-sec" aria-labelledby="ld-bk-dec"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bk-dec">Decisions</h2><span class="ld-kicker">Newest first</span></div>' +
      '<div class="ld-tbl-wrap"><table class="ld-rtab ld-pm-tab"><thead><tr><th>Date</th><th>Decision</th><th>Reason</th><th>By</th></tr></thead><tbody>' + decisions + "</tbody></table></div></section>" +
      '<section class="ld-sec" aria-labelledby="ld-bk-man"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bk-man">Mandate</h2><span class="ld-kicker">Set by the PM</span></div>' +
      '<p class="ld-pm-p">The limits the book is run within. The PM may change them, and every change is recorded as a decision with its reason.</p>' + mandateHTML(b.mandate, b) + "</section>" +
      '<details class="ld-det"><summary>Every trade (' + int((b.trades || []).length) + ")</summary>" +
      '<div class="ld-tbl-wrap"><table class="ld-rtab ld-pm-tab"><thead><tr><th>Date</th><th>Trade</th><th>Ticker</th><th class="r">Shares</th><th class="r">Price</th><th class="r">Amount</th><th class="r">Cost</th></tr></thead><tbody>' + trades + "</tbody></table></div>" +
      '<p class="ld-muted ld-pm-p">Each price is the stored close for the trade’s date.</p></details>' +
      "</div>";
    navChart(document.getElementById("ld-pm-chart"), b);
  }

  function candidateHTML(b, holdings) {
    var cand = b.candidate || [];
    var held = {};
    holdings.forEach(function (h) { held[h.ticker] = 1; });
    var rows = cand.map(function (p) {
      return '<tr><td class="r ld-num">' + p.rank + '</td><td><a class="ld-tk" href="' + ctx.href("company", p.ticker) + '">' + esc(p.ticker) + "</a></td><td>" + esc(p.name) + "</td><td>" + esc(p.sector || "Unclassified") +
        '</td><td class="r ld-num">' + zShort2(p.score) + '</td><td class="r ld-num">' + plainPct(p.weight) + "</td><td>" + (held[p.ticker] ? "Yes" : "No") + "</td></tr>";
    }).join("");
    var adds = b.candidateAdds || [], drops = b.candidateDrops || [];
    var cmp = b.inception ? (adds.length || drops.length
      ? "Compared with what the book holds, the rules would buy " + (adds.length ? plural(adds.length, "company", "companies") + " (" + esc(adds.slice(0, 12).join(", ")) + (adds.length > 12 ? " and more" : "") + ")" : "nothing") +
        " and sell " + (drops.length ? plural(drops.length, "company", "companies") + " (" + esc(drops.slice(0, 12).join(", ")) + (drops.length > 12 ? " and more" : "") + ")" : "nothing") + ". Only the PM decides whether to trade."
      : "The rules would hold exactly what the book holds.") : "";
    return '<section class="ld-sec" aria-labelledby="ld-bk-cand"><div class="ld-sec-h"><h2 class="ld-h2" id="ld-bk-cand">What the rules would hold today</h2><span class="ld-kicker">From the panel of ' + esc(b.candidateAsof ? dateMid(b.candidateAsof) : "n/a") + "</span></div>" +
      '<p class="ld-pm-p">' + plural(b.boxCount || 0, "company is", "companies are") + " in this box. The rules take the top " + int(b.targetCount || 0) + ", the middle of the mandate’s holdings range. " + cmp + "</p>" +
      (rows ? '<details class="ld-det"><summary>The rules book (' + int(cand.length) + ')</summary><div class="ld-tbl-wrap"><table class="ld-rtab ld-pm-tab"><thead><tr><th class="r">Rank</th><th>Ticker</th><th>Company</th><th>Sector</th><th class="r">Score</th><th class="r">Weight</th><th>Held</th></tr></thead><tbody>' + rows + "</tbody></table></div>" +
        '<p class="ld-muted ld-pm-p">The score is the box score plus half the quality score, both in robust z-scores within the company’s size.</p></details>' : "") +
      "</section>";
  }
  function zShort2(z) { return z == null || !isFinite(z) ? "n/a" : signed(z, 2); }
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
      '<p class="ld-deck">' + esc(e && e.message || e) + ". Reload the page. If that does not help, the last update may not have published the data file (stocks-data.json).</p></div></div></div>";
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
      if (mq.addEventListener) mq.addEventListener("change", function () { paintThemeBtn(); themeChanged(); });
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
    else if (page === "portfolios") renderPortfolios(main);
    else if (page === "book") {
      renderBook(main);
      listen(window, "hashchange", function () { redrawers = []; renderBook(main); window.scrollTo(0, 0); });
    }
    else renderHome(main);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
