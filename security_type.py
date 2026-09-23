"""What kind of security a listing is, so the screen can tell a company from a note.

The universe is every US listing in the NASDAQ Trader directory plus the S&P
1500, and that directory lists far more than operating companies. On 2026-09-21
it held 292 blank-check shells, 138 exchange-traded notes and bonds, 10
repackaged trust certificates, 3 mandatory-convertible unit listings and 229
closed-end funds. None of them is a business with a P/E, and several carry one
anyway: a note ticker resolves to its parent's CIK in SEC's own map, so ADAMG
(a 9.125% senior note) was shown with Adamas Trust's shares, a $2.25bn market
cap and a P/E of 14.9 built from the note's $25 price.

Rejecting them at the source was measured and is worse. A listing that stops
appearing is marked dropped and then revived for RETAIN_DROPPED_DAYS as
in_index=0, which writes 400 days of false delistings into an append-only
panel, and the carry-forward merge brings the parent's numbers back with it. So
every listing is kept and labeled, and the label is what the scoring, the
screener and the analyst scripts key on.

Imported by lambda_function.py and by theses/bin/common.py, so both sides use
the same rules. Standard library only; no network; pure functions.

Values (security_type):

    operating      ordinary operating-company equity (the default)
    lp             partnership or LLC units of an OPERATING business (ET, EPD,
                   MPLX). Labeled for visibility and scored like any company.
    bdc            business development company: a 1940 Act lender that files
                   10-Ks with XBRL income statements. Labeled, still scored.
    royalty_trust  grantor royalty trust. Labeled, still scored.
    spac           blank-check shell before its merger          (non-operating)
    debt           exchange-traded notes, debentures and bonds   (non-operating)
    structured     repackaged trust certificates (STRATS, CorTS) (non-operating)
    equity_units   mandatory-convertible "Corporate Units"       (non-operating)
    cef            closed-end fund                               (non-operating)

The label is point in time: a SPAC becomes an operating company when its merger
closes and its name or its filings change, so it is recomputed on every run and
written into each day's panel row rather than fixed in the registry.
"""
import math
import re

SECURITY_TYPES = ("operating", "lp", "bdc", "royalty_trust",
                  "spac", "debt", "structured", "equity_units", "cef")

# Not a business: excluded from peer cohorts, never scorable, hidden by default
# in the screener and refused by the analyst scripts. BDCs and royalty trusts
# are deliberately absent: both report real income, and both are labeled so a
# reader can see what they are without losing them from the screen.
NON_OPERATING = frozenset({"spac", "debt", "structured", "equity_units", "cef"})

# A claim on a parent rather than a share of it. Every issuer-level number on
# one of these rows is the parent's, so none of them is shown or ranked.
DEBT_LIKE = frozenset({"debt", "structured", "equity_units"})

_I = re.I

# ---- debt -------------------------------------------------------------------
# Plural security nouns only. The singular "note" in _NT_BAD_NAME cannot match
# inside "Notes" (no word boundary after the "e"), which is how 130 senior-note
# listings got through; widening that regex instead would also turn "unit" into
# "units" and drop ET, MPLX, CQP and ARLP. So the label catches them here.
_DEBT_NOUN = r"(?:Notes|Debentures|Bonds|ZONES)"
DEBT_COUPON = re.compile(r"\d+(?:\.\d+)?\s?%.*?\b" + _DEBT_NOUN + r"\b", _I)
DEBT_DUE = re.compile(r"\b" + _DEBT_NOUN + r"\b.*?\bdue\b", _I)
DEBT_KIND = re.compile(
    r"\b(?:Senior|Subordinated|Perpetual|Secured|Mortgage|Global|Exchangeable|"
    r"Deferrable\s+Interest|Fixed[- ]Rate|Fixed[- ]to[- ]Floating)"
    r"(?:\s+(?:Secured|Subordinated|Deferrable\s+Interest))?\s+" + _DEBT_NOUN + r"\b", _I)
# "Comcast Holdings ZONES": zero-premium exchangeable subordinated notes. Case
# sensitive, since "zones" is an ordinary word in a company name.
DEBT_ZONES = re.compile(r"\bZONES\b")
# TVA has no common stock. Its NYSE listings (TVC, TVE) are power bonds even
# though the directory calls one of them "Common Stock".
DEBT_ISSUER = re.compile(r"^Tennessee Valley Authority\b", _I)

# ---- structured and repackaged ----------------------------------------------
STRUCTURED = re.compile(
    r"\bSTRATS\b|\bCorTS\b|\bPPlus\b|Trust\s+Certificates|\bTr\s+Ctf\b|\bTr\s+Certs\b|"
    r"Corporate\s+Backed\s+Trust|Asset-Backed\s+Trust\s+Securities|"
    r"\bCapital\s+Trust\s+[IVX]+\b", _I)

# ---- equity units (mandatory convertibles) ----------------------------------
# DUKU, PPLC and SOMN were scorable in Utilities with their parent's full
# fundamentals and caps of $34bn to $52bn. "Common Units" is NOT matched here:
# that is how operating partnerships describe their equity.
EQUITY_UNITS = re.compile(
    r"\b(?:Corporate|Tangible\s+Equity|Equity|Purchase\s+Contract)\s+Units\b", _I)

# ---- closed-end funds and BDCs ----------------------------------------------
CEF_DESCRIPTOR = re.compile(r"\s-\s+Closed\s+End\s+Fund\s*$", _I)
BDC_DESCRIPTOR = re.compile(r"\s-\s+Business\s+Development\s+Company\s*$", _I)
# Inside the Nasdaq "Closed End Fund" descriptor, the lender names are BDCs.
# Measured on 2026-09-21: all 17 names matching this have EDGAR 10-K data and
# all 14 others are vendor-only, once OXLC and OCCI (CLO equity funds) are
# excluded. "Financial Corp" is deliberately absent: ArrowMark Financial Corp
# (BANX) is a true closed-end fund. Daxor (DXR), an operating medical-device
# maker registered under the 1940 Act, is the one row a name cannot get right;
# the revenue guard in classify_row handles it.
BDC_NAME = re.compile(
    r"\bBDC\b|\bCapital\s+(?:Corp(?:oration)?|Inc\.?|Ltd\.?)|Investment\s+Corp(?:oration)?\b|"
    r"\bLending\b|\bFinance\b", _I)
# Outside the descriptor only the self-describing "BDC" token is safe: "Investment
# Corp" also names mortgage REITs (AGNC, CIM, CHMI) and SPACs.
BDC_TOKEN = re.compile(r"\bBDC\b")
BDC_EXCLUDE = re.compile(r"Oxford\s+Lane|OFS\s+Credit", _I)   # CLO equity funds

# NYSE rows carry no descriptor suffix, so fund names are matched on the entity
# noun. "Fund" is a whole word here, never "Funding" or "Fundamental": 0
# operating hits on 2026-09-21.
FUND_NAME = re.compile(r"\bFunds?\b", _I)
# Fund-only vocabulary, 0 operating hits outside the S&P rows.
FUND_WORD = re.compile(r"\bMunicipals?\b|\bTax[- ]Advantage|\bTax[- ]Free\b", _I)
# "... Income Trust", "... Municipal Trust": fund-family trusts. The guard below
# is what keeps Universal Health Realty Income Trust (UHT) operating.
FUND_TRUST = re.compile(
    r"\b(?:Income|Municipal|Municipals|Muni\w*|Bond|Credit|Dividend|Yield|Rate|Term|"
    r"Equity|Utility|Multi-Media|Sciences|Resources|Investors|Securities|Opportunities|"
    r"Opportunity|Universal|Micro-Cap|Small-Cap|Global|Power|Allocation|Core|Duration)\s+"
    r"(?:Income\s+)?Trust\b|\bTrust\s+for\s+Investment\s+Grade", _I)
TRUST_OPERATING_GUARD = re.compile(
    r"Realty|Real\s+Estate|Propert|Mortgage|REIT|Hospitality|Health\s*care|Homes|"
    r"Bancorp|Bank|Digital\s+Infrastructure|Finance\s+Trust", _I)
# Descriptors only a fund uses. REITs also issue "Common Shares of Beneficial
# Interest" (Camden Property Trust), so the same guard applies.
FUND_DESCRIPTOR = re.compile(
    r"Shares\s+of\s+Beneficial\s+(?:Interest|Ownership)|\bSBI\b|Cmn\s+Shs\s+of\s+BI", _I)

# ---- royalty trusts -----------------------------------------------------------
# Royalty CORPORATIONS (OR, GROY, RPRX) do not match: they are operating companies.
ROYALTY_TRUST = re.compile(r"\bRoyal\w*\s+Trust\b|^Mesabi\s+Trust\b", _I)

# ---- SPACs --------------------------------------------------------------------
# "acquisition" with no leading word boundary, so KRAKacquisition and
# "Acquisition1 Corp" match. Only together with a share-class descriptor, which
# is what keeps "Acquisition" in an operating company's product name out.
SPAC_WORD = re.compile(r"acquisition", _I)
SPAC_STRONG = re.compile(r"\bSPAC\b|\bMerger\s+Corp", _I)
# Sponsor series numbering ("Churchill Capital Corp XI", "Gores Holdings X",
# "GigCapital9"), only with an ordinary-share descriptor. The lookahead keeps
# the "V" in "N.V." from reading as a numeral.
SPAC_SERIES = re.compile(
    r"\b(?:Corp(?:oration)?|Company|Holdings|Partners|Capital|Financial)\.?,?\s+"
    r"(?:[IVX]{1,4}|Four|Five|Six|Seven|Eight)\b(?!\.)|\bGigCapital\d+|\b[IVX]{1,4}\s+Capital\s+Corp",
    _I)
SPAC_ORDINARY = re.compile(r"Ordinary\s+Shares?|Ord\s+Shares?", _I)
SPAC_SHARE = re.compile(
    r"Ordinary\s+Shares?|Ord\s+Shares?|Class\s+A\s+Common\s+Stock|Common\s+Stock|"
    r"\bUnits?\b|\bRights?\b", _I)

# ---- partnership units (operating) --------------------------------------------
LP_UNITS = re.compile(
    r"\bCommon\s+Units\b|Limited\s+Partner(?:ship)?\s+Interests?|Limited\s+Partnership\s+Units|"
    r"L\.?P\.?\s+Units\b|limited\s+liability\s+company\s+interests|\bLLC\s+Class\s+A\s+Units\b|"
    r"\bL\.?P\.?\s+(?:Common\s+Stock|Limited\s+Partnership)\b", _I)

# ---- data corrections -----------------------------------------------------------
# Yahoo's industry label for a blank-check company. It catches 21 shells whose
# names carry no tell at all (Aperture AC, Graf Global, Wilco 63 Corporation).
SHELL_SUB_INDUSTRY = "Shell Companies"
FUND_SUB_INDUSTRY = "Asset Management"
# A listed share class is an operating-company structure (up-C, dual class). A
# 1940 Act fund lists one class of common, so a class letter vetoes the
# vendor-only fund fingerprint. Measured: it keeps Vinci Compass (VINP), a
# foreign-filing asset manager with no US-GAAP companyfacts, operating.
SHARE_CLASS = re.compile(r"\bClass\s+[A-Z]\b", _I)

# Listings the vendor-only fund fingerprint gets wrong, checked by hand. Keyed on
# the listing name, not the ticker, so a reused ticker does not inherit the call.
# Each entry says what was checked; delete it once the row's own data (an EDGAR
# 10-Q) settles the label.
#
# RoboStrategy, Inc. (BOT). Listed on Nasdaq 2026-05-11; Yahoo files it under
# Asset Management with gross and operating margin 0 and there are no EDGAR
# companyfacts yet, which is the closed-end-fund fingerprint. But its one news
# item (citybiz, 2026-07-15) is a $16m private placement led by a $10m purchase
# by its CEO: a corporate capital raise that a registered fund, barred by the
# 1940 Act from selling common below NAV, does not do. Nothing on disk shows a
# NAV, a schedule of investments or a discount, all of which the real funds in
# the same fingerprint (DXYZ, PWRL, TY, CET) publish. Read as a holding company
# (a "Strategy" treasury vehicle), not a registered fund. Confidence: moderate,
# from one headline; revisit when its first 10-Q or N-CSR lands. The exception
# sits ahead of the EDGAR fingerprints too, because a revenue-less 10-Q with a
# net-income line would otherwise read as a BDC.
KNOWN_OPERATING = re.compile(r"^RoboStrategy,?\s+Inc\b", _I)


def _num(value):
    """A finite float from a dict value or a panel CSV cell, else None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _text(value):
    return " ".join(str(value or "").split())


def classify_name_rule(name, index=""):
    """(security_type, rule_id) from the listing's name and venue alone.

    Rule ids starting "x_" match an explicit exchange descriptor (the Nasdaq
    " - <descriptor>" suffix or a security phrase such as "Senior Notes due");
    ids starting "n_" are name-structure rules needed for NYSE rows, which carry
    no descriptor. S&P 500/400/600 constituents always return operating: S&P
    eligibility already excludes funds, BDCs, SPACs, debt and trusts, and those
    rows carry the Wikipedia company name, not an exchange security name."""
    name = _text(name)
    index = _text(index)
    if index.startswith("S&P"):
        return "operating", "sp_index"

    # Debt first: a note issued by a trust, a REIT or a BDC is still a note.
    if DEBT_COUPON.search(name):
        return "debt", "x_debt_coupon"
    if DEBT_DUE.search(name):
        return "debt", "x_debt_due"
    if DEBT_KIND.search(name):
        return "debt", "x_debt_kind"
    if DEBT_ZONES.search(name):
        return "debt", "x_debt_zones"
    if STRUCTURED.search(name):
        return "structured", "x_structured"
    if EQUITY_UNITS.search(name):
        return "equity_units", "x_equity_units"
    if DEBT_ISSUER.search(name):
        return "debt", "n_debt_issuer"

    if BDC_DESCRIPTOR.search(name):
        return "bdc", "x_bdc_descriptor"
    if CEF_DESCRIPTOR.search(name):
        if BDC_NAME.search(name) and not BDC_EXCLUDE.search(name):
            return "bdc", "x_cef_descriptor_bdc_name"
        return "cef", "x_cef_descriptor"

    if SPAC_WORD.search(name) and SPAC_SHARE.search(name):
        return "spac", "x_spac_acquisition"
    if SPAC_STRONG.search(name) and SPAC_SHARE.search(name):
        return "spac", "n_spac_merger_word"
    if SPAC_SERIES.search(name.split(" - ")[0]) and SPAC_ORDINARY.search(name):
        return "spac", "n_spac_series"
    if BDC_TOKEN.search(name):
        return "bdc", "n_bdc_token"

    if ROYALTY_TRUST.search(name):
        return "royalty_trust", "n_royalty_trust"
    if FUND_DESCRIPTOR.search(name) and not TRUST_OPERATING_GUARD.search(name):
        return "cef", "x_fund_descriptor"
    if FUND_NAME.search(name):
        if re.search(r"\bLending\b", name, _I):
            return "bdc", "n_fund_name_lending"
        return "cef", "n_fund_name"
    if FUND_WORD.search(name):
        return "cef", "n_fund_word"
    if FUND_TRUST.search(name) and not TRUST_OPERATING_GUARD.search(name):
        return "cef", "n_fund_trust"

    if LP_UNITS.search(name):
        return "lp", "x_lp_units"
    return "operating", "default"


def classify_name(name, index=""):
    """security_type from name and venue alone (see classify_name_rule)."""
    return classify_name_rule(name, index)[0]


def has_edgar_revenue(row):
    """Real reported revenue: an EDGAR sweep, positive TTM revenue, and margins
    that are not both exactly zero (Yahoo's placeholder for a shell)."""
    if not row.get("edgar_updated"):
        return False
    rev = _num(row.get("ttm_revenue"))
    if rev is None or rev <= 0:
        return False
    return not (_num(row.get("gross_margin")) == 0 and _num(row.get("operating_margin")) == 0)


def _no_real_gross_margin(row):
    """Absent, or the vendor's 0 or 1 that a lender or fund gets for a line it
    does not report. Absent counts too, because the labeler itself blanks this
    field on funds and BDCs; a fingerprint that needed the vendor value would
    flip its own label back on the next run."""
    gm = _num(row.get("gross_margin"))
    return gm is None or gm in (0.0, 1.0)


def _bdc_fingerprint(row):
    """A lender filing 10-Ks: EDGAR companyfacts with net income and equity
    lines but no revenue line (investment income is not tagged as Revenues),
    and no real gross margin. Only inside Yahoo's Asset Management industry,
    since pre-revenue biotechs and banks match the rest of it. On 2026-09-21
    this is exactly the 19 undescribed BDCs (MAIN, FSK, OBDC, HTGC, ...) plus
    MSC Income Fund (MSIF), whose name says fund and whose 10-K says BDC."""
    return (row.get("sub_industry") == FUND_SUB_INDUSTRY
            and bool(row.get("edgar_updated"))
            and _num(row.get("ttm_revenue")) is None
            and _num(row.get("ttm_net_income")) is not None
            and _no_real_gross_margin(row))


def _vendor_fund_fingerprint(row):
    """An Asset Management listing with no EDGAR companyfacts at all and no real
    gross margin. Funds file N-CSR, which has no XBRL income statement, so a
    listed fund is exactly what this looks like (Tri-Continental, Central
    Securities, Pershing Square USA, and the private-tech funds Destiny Tech100
    and Powerlaw, which publish a monthly NAV). A class letter in the name
    vetoes it; KNOWN_OPERATING overrides it for the rows checked by hand."""
    return (row.get("sub_industry") == FUND_SUB_INDUSTRY
            and not row.get("edgar_updated")
            and _no_real_gross_margin(row)
            and not SHARE_CLASS.search(_text(row.get("name"))))


def classify_row_rule(row):
    """(security_type, rule_id) for one listing, from its name plus the data
    already on the row: Yahoo's sub_industry and the EDGAR sweep's footprint.

    `row` is a stock dict or a panel CSV row; both spell the fields the same.
    Pure and idempotent, including over its own output: nothing it reads is a
    field the labeler blanks, except gross_margin, which it reads as "no real
    gross margin" so a blank still counts.

    The revenue guard runs last and only turns spac, cef or bdc back into
    operating. Never debt, structured or equity_units: those resolve to the
    PARENT's CIK and inherit its revenue (78 of 138 debt rows on 2026-09-21)."""
    cat, rule = classify_name_rule(row.get("name"), row.get("index"))
    if rule == "sp_index":
        return cat, rule
    if cat == "operating" and KNOWN_OPERATING.search(_text(row.get("name"))):
        return "operating", "e_known_operating"
    if cat == "operating":
        if (row.get("sub_industry") or "") == SHELL_SUB_INDUSTRY:
            cat, rule = "spac", "d_shell_sub_industry"
        elif _bdc_fingerprint(row):
            cat, rule = "bdc", "d_bdc_edgar"
        elif _vendor_fund_fingerprint(row):
            cat, rule = "cef", "d_cef_vendor"
    elif cat == "cef" and _bdc_fingerprint(row):
        cat, rule = "bdc", "d_bdc_edgar"
    if cat in ("spac", "cef", "bdc") and has_edgar_revenue(row):
        return "operating", "d_revenue_guard"
    return cat, rule


def classify_row(row):
    """security_type for one listing (see classify_row_rule)."""
    return classify_row_rule(row)[0]


def is_operating(value):
    """True unless the label is one of NON_OPERATING. A blank or unknown label
    counts as operating: callers that can see a name should classify it first
    (theses/bin/common.py does), and an allowlist on a blank label would empty
    a universe read from panel rows written before the column existed."""
    return (value or "").strip() not in NON_OPERATING
