#!/usr/bin/env python3
"""beefy_page.py -- standalone collector + renderer for /beefy.html
Splits each vault's advertised APY into counterparty-paid yield and emissions-funded yield.

Reads the Beefy public API (NO keys, NO auth, read-only), joins it by vault id,
and writes beefy.html atomically. Public build: keyless, read-only, no account data.

Endpoints used (all keyless, all GET):
  /vaults/all      every vault family. NOTE: bare /vaults is STANDARD-ONLY and
                   misses all 505 active CLM + 508 active gov vaults, so the
                   HyperEVM card would be empty if we used it.
  /apy/breakdown   per-vault APR components (preferred over /apy: /apy carries
                   1,476 stale ids and disagrees on 742).
  /tvl             two-level {numericChainId: {vaultId: usd}}, has an
                   "undefined" bucket -- guarded.
  /fees            authoritative performance/withdraw/deposit fees.
  /boosts + /apy/boosts   optional boost APRs (degrade silently).

THE POINT OF THE PAGE -- what counts as "APY from fees":
  FEE APR (paid by counterparties: swappers, borrowers)
      tradingApr + clmApr + rewardPoolTradingApr + lendingApr
  INCENTIVE APR (token issuance / emissions / campaigns)
      vaultApr + merklApr + rewardPoolApr + liquidStakingApr
      + composablePoolApr + lineaIgnitionApr + stellaSwapApr + boost
  Fee basis trap: clmApr / vaultApr / lendingApr are already NET of the Beefy
  performance fee; everything else is gross. Never re-apply the fee to totalApy.

House conventions honoured: report-family CSS (divergence_page.py), atomic
write, a self-reporting staleness banner, fail-soft -- every card is wrapped so
one bad section can never blank the page.

Usage:
  python3 beefy_page.py              # fetch + write html
  python3 beefy_page.py --once       # same (explicit; no daemon mode exists)
  python3 beefy_page.py --dry-run    # print the tables to stdout, write nothing
  python3 beefy_page.py --no-cache   # ignore the on-disk cache
"""
import argparse
import datetime as _dt
import html as _h
import json
import os
import re
import sys
import time

import requests

# --------------------------------------------------------------------- paths
_HERE = os.path.dirname(os.path.abspath(__file__))
_CMD = _HERE


def _pick(env, deployed, local_name):
    """Env override, else a path next to this script."""
    p = os.environ.get(env)
    if p:
        return p
    return os.path.join(_HERE, local_name)


HTML_OUT = _pick("BEEFY_HTML_OUT", _CMD + "/html/beefy.html", "beefy.html")
CACHE_PATH = _pick("BEEFY_CACHE", _CMD + "/beefy_cache.json", "beefy_cache.json")
HIST_PATH = _pick("BEEFY_HIST", _CMD + "/beefy_apy_history.json", "beefy_apy_history.json")

API = "https://api.beefy.finance"
TIMEOUT = 15           # 6 endpoints x 15s = 90s worst case
CACHE_TTL = 600           # 10 min; upstream refreshes every 5-15 min anyway
PUBLIC = True   # this build is always the public one
STALE_MIN = 960           # scheduled every 6h; tolerate two missed runs + delay
UA = "beefy-yield-decomposition/1.0 (+read-only public dashboard)"

MIN_TVL_A = 1_000_000     # card A floor
MIN_TVL_B = 250_000       # card B floor -- see _card_b(): only 2 ETH/BTC/SOL
                          # vaults on the whole platform clear $1M, so the
                          # brief's $1M floor would render a 2-row card.
TOP_N = 15
MAX_APY = 10.0            # 1000% -- the API returns unfiltered 1e23 garbage
HIST_KEEP_DAYS = 120      # a 90d mean needs >90d retained; the DefiLlama seed backfills this far
HIST_MIN_TVL = 250_000
HIST_MIN_GAP_H = 20       # at most ~1 sample/day per vault

# --------------------------------------------------------------- apr classes
FEE_KEYS = ("tradingApr", "clmApr", "rewardPoolTradingApr", "lendingApr")
INC_KEYS = ("vaultApr", "merklApr", "rewardPoolApr", "liquidStakingApr",
            "composablePoolApr", "lineaIgnitionApr", "stellaSwapApr")
SHORT = {"tradingApr": "trade", "clmApr": "clm", "rewardPoolTradingApr": "rp-trade",
         "lendingApr": "lend", "vaultApr": "farm", "merklApr": "merkl",
         "rewardPoolApr": "rp", "liquidStakingApr": "lst",
         "composablePoolApr": "bpt", "lineaIgnitionApr": "linea",
         "stellaSwapApr": "stella", "boostApr": "boost"}

# ---------------------------------------------------------------- classifier
USD_STABLE = re.compile(
    r"(usd|dai|frax|gho|dola|mim|crvusd|susde?$|bold|ousd|pyusd|rlusd|deusd|"
    r"syrup|alusd|eusd|hyusd|musd|vusd|trusd|reusd|savusd|scrvusd|sfrxusd|"
    r"fxusd|stusds)", re.I)
NON_USD_FIAT = re.compile(r"^(jEUR|EURC|EURe|agEUR|EURA|BRZ|CADC|AUDD|GYEN|XSGD)$", re.I)
ETH_FAM = re.compile(
    r"^(w?eth|eth\+|weeth|weeths|wsteth|steth|cbeth|reth|frxeth|sfrxeth|ezeth|"
    r"rseth|wrseth|rsweth|oseth|oeth|mseth|ankreth|beth|eeth|ageth|yneth|pxeth|"
    r"meth|wbeth|lseth|unieth|waethweth|waetheth)$", re.I)
BTC_FAM = re.compile(
    r"^(w?btc|btcb|wbtc\.e|cbbtc|tbtc|lbtc|ebtc|fbtc|solvbtc|xsolvbtc|xbtc|"
    r"ibtc|ubtc|bbtc|hemibtc|pumpbtc|unibtc|enzobtc|swbtc|kbtc|brbtc|stbtc|"
    r"sbtc|obtc)$", re.I)
SOL_FAM = re.compile(r"^(w?sol|jitosol|msol|bnsol|jupsol|bsol|solvsol|inf)$", re.I)
LST_FAM = re.compile(
    r"^(wsteth|steth|cbeth|reth|sfrxeth|frxeth|ezeth|rseth|oseth|mseth|ankreth|"
    r"weeth|eeth|pxeth|meth|wbeth|lseth|unieth|jitosol|msol|bnsol|jupsol|bsol|"
    r"khype|sthype|sthype|wsthype)$", re.I)

# Only what this page actually reads -- /vaults/all is 6.7 MB on the wire and
# there is no reason to park all of it in the cache file.
VAULT_FIELDS = ("id", "name", "chain", "status", "type", "assets", "platformId",
                "earnContractAddress", "tokenAddress", "risks",
                "lastHarvest")


def classify(assets):
    if not assets:
        return None
    if all(USD_STABLE.search(a) and not NON_USD_FIAT.match(a) for a in assets):
        return "STABLE"
    if all(ETH_FAM.match(a) for a in assets):
        return "ETH"
    if all(BTC_FAM.match(a) for a in assets):
        return "BTC"
    if all(SOL_FAM.match(a) for a in assets):
        return "SOL"
    return None


# Tokenized equities. The ticker alone is NOT a safe signal: CVX is both Chevron and Convex,
# and CAT/KO/PG/HD/BA collide with memecoins. What IS reliable is the WRAPPER MARKER — every
# tokenized share on Beefy carries a lowercase issuer suffix (NVDAc on Base, MSFTrh on the
# robinhood chain) or lives on a chain that only lists tokenized equities. So we require a
# ticker match AND a tokenization marker. That lets the ticker list stay generous, including
# the colliding names, because a bare "CVX" on ethereum can never satisfy the marker test.
EQUITY_TICKER = re.compile(
    r"^(NVDA|GOOGL|GOOG|AAPL|META|MSFT|TSLA|AMZN|INTC|AMD|AVGO|MU|MRVL|COIN|HOOD|MSTR|PLTR|"
    r"ORCL|DELL|CRCL|NBIS|SPY|QQQ|IWM|SOXL|SOXX|TQQQ|BRK|JPM|WMT|XOM|UNH|LLY|JNJ|PG|HD|CVX|"
    r"ABBV|KO|PEP|COST|MRK|ADBE|CRM|NFLX|DIS|BA|CAT|UBER|ABNB|SHOP|PYPL|RBLX|RIVN|LCID|NIO|"
    r"SNDK|SMSN|SKHX|EWY|RKLB|BE|F|GM|T|C)(?P<mark>c|rh|x|s|\.b)?$")

# Chains that list tokenized equities exclusively; a bare ticker there needs no suffix.
EQUITY_CHAINS = {"robinhood"}


def is_equity(assets, chain=None):
    """True only when a recognised ticker ALSO carries a tokenization marker: an issuer suffix
    (NVDAc / MSFTrh) or an equity-only chain. Suffix-less tickers on ordinary chains are
    rejected, which is what keeps Convex's CVX out of the Chevron slot."""
    for a in (assets or []):
        m = EQUITY_TICKER.match((a or "").strip())
        if not m:
            continue
        if m.group("mark") or (chain in EQUITY_CHAINS):
            return True
    return False


def is_lst(assets):
    return bool(assets) and any(LST_FAM.match(a) for a in assets)


# --------------------------------------------------------------------- fetch
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip",
                         "Accept": "application/json"})


def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:  # noqa: BLE001  missing/corrupt cache is not an error
        return {}


def _save_cache(cache):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(tmp, CACHE_PATH)
    except Exception as e:  # noqa: BLE001  a dashboard must not die on cache IO
        sys.stderr.write("cache write failed: %s\n" % e)


def _trim(key, data):
    """Shrink /vaults/all (6.7 MB) before it hits the cache file."""
    if key == "vaults" and isinstance(data, list):
        return [{k: v.get(k) for k in VAULT_FIELDS if k in v} for v in data]
    if key == "boosts" and isinstance(data, list):
        return [{k: b.get(k) for k in ("id", "poolId", "chain", "status")} for b in data]
    return data


class Fetcher(object):
    """Cache-first fetch. Never raises: on failure it serves the stale cache
    entry and records the reason, so the page always renders."""

    def __init__(self, use_cache=True):
        self.cache = _load_cache() if use_cache else {}
        self.use_cache = use_cache
        self.meta = {}      # key -> {"src", "age_s", "err"}
        self.dirty = False

    def get(self, key, path):
        now = time.time()
        ent = self.cache.get(key) or {}
        ts = ent.get("fetched_at") or 0
        if self.use_cache and ent.get("data") is not None and now - ts < CACHE_TTL:
            self.meta[key] = {"src": "cache", "age_s": now - ts, "err": None}
            return ent["data"]
        try:
            r = _session.get(API + path, timeout=TIMEOUT)
            r.raise_for_status()
            data = _trim(key, r.json())
            self.cache[key] = {"fetched_at": now, "data": data}
            self.dirty = True
            self.meta[key] = {"src": "live", "age_s": 0.0, "err": None}
            return data
        except Exception as e:  # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, str(e)[:120])
            if ent.get("data") is not None:
                self.meta[key] = {"src": "stale-cache", "age_s": now - ts, "err": err}
                return ent["data"]
            self.meta[key] = {"src": "FAILED", "age_s": None, "err": err}
            return None

    def flush(self):
        if self.dirty:
            _save_cache(self.cache)


# ------------------------------------------------------------------- history
def _load_hist():
    try:
        with open(HIST_PATH, "r", encoding="utf-8") as f:
            h = json.load(f)
        return h if isinstance(h, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_hist(hist):
    try:
        tmp = HIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, separators=(",", ":"))
        os.replace(tmp, HIST_PATH)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("history write failed: %s\n" % e)


def update_history(rows, now, write=True):
    """Append at most one APY sample per vault per ~day; return {id: 7d delta}.

    The Beefy API exposes no APY history, so the 7d column is built from our
    own samples. It reads '--' until the workflow has been running a week.
    """
    hist = _load_hist()
    cut = now - HIST_KEEP_DAYS * 86400
    for r in rows:
        if r["tvl"] < HIST_MIN_TVL:
            continue
        s = hist.get(r["id"]) or []
        s = [p for p in s if isinstance(p, list) and len(p) == 2 and p[0] > cut]
        if not s or now - s[-1][0] > HIST_MIN_GAP_H * 3600:
            s.append([round(now), round(r["apy"], 8)])
        hist[r["id"]] = s
    if write:
        _save_hist(hist)

    out = {}
    for r in rows:
        s = hist.get(r["id"]) or []
        # newest sample that is at least 6 days old
        old = [p for p in s if now - p[0] >= 6 * 86400]
        if old:
            out[r["id"]] = r["apy"] - old[-1][1]
    return out


# --------------------------------------------------------------------- build
def build(fetch):
    vaults = fetch.get("vaults", "/vaults/all")
    brk = fetch.get("breakdown", "/apy/breakdown")
    tvl_raw = fetch.get("tvl", "/tvl")
    fees = fetch.get("fees", "/fees")
    boosts = fetch.get("boosts", "/boosts")
    boost_apy = fetch.get("boost_apy", "/apy/boosts")

    if not vaults or not brk:
        raise RuntimeError("core endpoints unavailable (vaults=%s breakdown=%s)"
                           % (bool(vaults), bool(brk)))
    tvl_raw = tvl_raw or {}
    fees = fees or {}

    # /tvl is {numericChainId: {vaultId: usd}} and carries an "undefined" key.
    vault_chain = {v.get("id"): v.get("chain") for v in vaults if v.get("id")}
    tvl_flat, chain_tvl = {}, {}
    for cid, d in tvl_raw.items():
        if cid == "undefined" or not isinstance(d, dict):
            continue
        # /tvl is keyed by NUMERIC chain id; every other endpoint uses the
        # chain NAME. Recover the name from the vault ids in the bucket.
        names = {}
        for vid in d:
            n = vault_chain.get(vid) or vault_chain.get(vid[:-3] if vid.endswith("-rp") else vid)
            if n:
                names[n] = names.get(n, 0) + 1
        label = max(names, key=names.get) if names else ("chain " + str(cid))
        chain_tvl[label] = (chain_tvl.get(label, 0.0)
                            + sum(v for v in d.values() if isinstance(v, (int, float))))
        tvl_flat.update(d)

    # boost APR by vault id (a boost is a separate stake; flagged in the legend)
    boost_by_vault = {}
    if isinstance(boosts, list) and isinstance(boost_apy, dict):
        for b in boosts:
            if b.get("status") != "active":
                continue
            apr = boost_apy.get(b.get("id"))
            if isinstance(apr, (int, float)) and apr > 0 and b.get("poolId"):
                boost_by_vault[b["poolId"]] = boost_by_vault.get(b["poolId"], 0.0) + apr

    rows, outliers = [], []
    n_active = n_eol = 0
    types = {}
    for v in vaults:
        if v.get("status") != "active":
            n_eol += 1
            continue
        n_active += 1
        types[v.get("type")] = types.get(v.get("type"), 0) + 1
        vid = v.get("id")
        if not vid or v.get("type") == "gov":
            # gov "-rp" wrappers are the reward-pool side of a CLM; they carry
            # the TVL and a duplicate totalApy. Folded into the CLM row below.
            continue
        b = dict(brk.get(vid) or {})
        rp = brk.get(vid + "-rp")
        t = float(tvl_flat.get(vid) or 0.0)
        rp_t = float(tvl_flat.get(vid + "-rp") or 0.0)
        if rp_t > t:
            # CLM: the deposits sit in the paired "-rp" gov reward pool, and
            # the bare cow id reports a dust TVL (often <$2). The -rp
            # breakdown is a superset -- it also carries rewardPoolApr.
            t = rp_t
            if isinstance(rp, dict):
                for k, val in rp.items():
                    b.setdefault(k, val)
        apy = b.get("totalApy")
        if not isinstance(apy, (int, float)):
            continue
        if apy > MAX_APY:
            outliers.append((vid, apy, t))
            continue

        def _n(k):
            x = b.get(k, 0)
            try:
                return float(x)
            except (TypeError, ValueError):        # some APRs arrive as strings
                return 0.0

        fee_parts = [(k, _n(k)) for k in FEE_KEYS if _n(k) != 0]
        inc_parts = [(k, _n(k)) for k in INC_KEYS if _n(k) != 0]
        bst = boost_by_vault.get(vid, 0.0)
        if bst:
            inc_parts.append(("boostApr", bst))
        fee_apr = sum(x for _, x in fee_parts)
        inc_apr = sum(x for _, x in inc_parts)

        f = fees.get(vid)          # absent (not zero) for every gov vault
        assets = v.get("assets") or []
        rows.append({
            "id": vid, "name": v.get("name") or vid, "chain": v.get("chain") or "?",
            "type": v.get("type"), "platform": v.get("platformId") or "",
            "assets": assets, "cls": classify(assets), "lst": is_lst(assets),
            "eq": is_equity(assets, v.get("chain")),
            "vault_addr": v.get("earnContractAddress") or "",
            "want_addr": v.get("tokenAddress") or "",
            "tvl": float(t or 0), "apy": float(apy),
            "fee_apr": fee_apr, "inc_apr": inc_apr,
            "fee_parts": fee_parts, "inc_parts": inc_parts,
            "perf": (f or {}).get("performance", {}).get("total"),
            "wdr": (f or {}).get("withdraw"), "has_fee_row": f is not None,
            "last_harvest": v.get("lastHarvest"),
        })

    stats = {
        "n_active": n_active, "n_eol": n_eol, "types": types,
        "chain_tvl": chain_tvl,
        "total_tvl": sum(chain_tvl.values()),
        "n_chains": len({v.get("chain") for v in vaults
                         if v.get("status") == "active"}),
        "outliers": sorted(outliers, key=lambda x: -x[2])[:5],
        "n_outliers": len(outliers),
        "n_rows": len(rows),
        "meta": fetch.meta,
    }
    return rows, stats


def select(rows, cls_set=None, lst=False, chain=None, min_tvl=0.0, n=TOP_N, eq=False):
    def ok(r):
        if eq:
            return bool(r.get("eq")) and r["tvl"] >= min_tvl
        if chain is not None:
            return r["chain"] == chain
        if r["tvl"] < min_tvl:
            return False
        if cls_set and r["cls"] in cls_set:
            return True
        return bool(lst and r["lst"])
    sel = [r for r in rows if ok(r)]
    sel.sort(key=lambda r: r["apy"], reverse=True)
    # 222 CLMs also ship a classic "-vault" wrapper holding the same position.
    # When both clear the floor and report the same APY they are one pool shown
    # twice, so keep the larger and don't burn two of the top-N rows on it.
    have = {r["id"] for r in sel}
    out = []
    for r in sel:
        bare = r["id"][:-6] if r["id"].endswith("-vault") else None
        if bare and bare in have:
            twin = next((x for x in sel if x["id"] == bare), None)
            if twin and abs(twin["apy"] - r["apy"]) < 1e-9 and twin["tvl"] > r["tvl"]:
                continue
        elif r["id"] + "-vault" in have:
            twin = next((x for x in sel if x["id"] == r["id"] + "-vault"), None)
            if twin and abs(twin["apy"] - r["apy"]) < 1e-9 and twin["tvl"] >= r["tvl"]:
                continue
        out.append(r)
    return out[:n]


# ------------------------------------------------------------------ printing
def _pct(x, dp=2):
    return "--" if x is None else "%.*f%%" % (dp, 100 * x)


def _usd(x):
    if x is None:
        return "--"
    if x >= 1_000_000:
        return "$%.2fM" % (x / 1e6)
    if x >= 1_000:
        return "$%.0fk" % (x / 1e3)
    return "$%.0f" % x


def _parts(parts):
    return " ".join("%s %.2f" % (SHORT.get(k, k), 100 * v) for k, v in parts)


def print_table(title, sel, d7):
    print("\n" + "=" * 150)
    print(title)
    print("=" * 150)
    print("%-9s %-40s %10s %9s %9s %9s %6s %6s %7s %8s"
          % ("chain", "vault", "TVL", "totalAPY", "feeAPR", "incAPR",
             "fee%", "perf", "wdr", "7dAPYd"))
    print("-" * 150)
    if not sel:
        print("(none matched)")
        return
    for r in sel:
        tot = r["fee_apr"] + r["inc_apr"]
        share = "%.0f%%" % (100 * r["fee_apr"] / tot) if tot > 0 else "--"
        print("%-9s %-40s %10s %9s %9s %9s %6s %6s %7s %8s"
              % (r["chain"][:9], r["id"][:40], _usd(r["tvl"]), _pct(r["apy"]),
                 _pct(r["fee_apr"]), _pct(r["inc_apr"]), share,
                 _pct(r["perf"], 1) if r["has_fee_row"] else "--",
                 _pct(r["wdr"], 3) if r["has_fee_row"] else "--",
                 ("%+.2fpp" % (100 * d7[r["id"]])) if r["id"] in d7 else "--"))
        print("          fee[%s] inc[%s]" % (_parts(r["fee_parts"]) or "-",
                                             _parts(r["inc_parts"]) or "-"))


# ---------------------------------------------------------------------- html
_CSS = """
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;margin:0 auto;padding:16px;max-width:1240px}
h1{font-size:1.3em;margin:8px 0}
h1 a{color:#58a6ff;text-decoration:none}
h2{font-size:1.02em;color:#8b949e;border-bottom:1px solid #21262d;padding-bottom:4px;margin:24px 0 6px}
table{width:100%;border-collapse:collapse}
th,td{padding:5px 7px;border-bottom:1px solid #161b22;font-size:.88em;vertical-align:top;text-align:left}
th{color:#8b949e;font-weight:600;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
.ts{color:#8b949e;font-size:.85em;margin-left:10px}
.badge{display:inline-block;padding:6px 14px;border-radius:8px;font-weight:700}
.bd{display:inline-block;padding:1px 8px;border-radius:6px;font-size:.78em;font-weight:700;white-space:nowrap}
.b-RED{background:#4a1212;border:1px solid #e74c3c;color:#ff7b72}
.b-AMBER{background:#4a3a10;border:1px solid #f1c40f;color:#e3b341}
.b-INFO{background:#10304a;border:1px solid #388bfd;color:#79c0ff}
.b-GREEN{background:#123f22;border:1px solid #2ecc71;color:#3fb950}
.pos{color:#3fb950}.neg{color:#ff7b72}.dim{color:#8b949e}
.sub{color:#6e7681;font-size:.82em;font-family:ui-monospace,monospace;white-space:nowrap}
.vid{font-family:ui-monospace,monospace;font-size:.92em;color:#e6edf3}
.err{color:#e3b341;font-size:.88em}
.note{background:#11161d;border:1px solid #21262d;border-radius:8px;padding:10px;margin:10px 0;font-size:.86em;color:#8b949e}
.explain{background:#0f1620;border:1px solid #21344a;border-left:3px solid #388bfd;border-radius:8px;padding:14px 16px;margin:14px 0}
.explain p{margin:8px 0;color:#c9d1d9;font-size:.95em;line-height:1.55}
.explain h2.plain{margin:0 0 4px;font-size:1.05em;border:0;padding:0;color:#e6edf3}
.keyidea{background:#11161d;border:1px solid #21262d;border-radius:8px;padding:10px 14px;margin:12px 0 2px}
.keyidea ul{margin:6px 0 6px 18px;padding:0}
.keyidea li{margin:5px 0;color:#c9d1d9;font-size:.93em;line-height:1.5}
p.lead{color:#c9d1d9;font-size:.97em;line-height:1.55;margin:6px 0 10px}
details.tech{margin:8px 0}
details.tech summary{cursor:pointer;color:#58a6ff;font-size:.86em;padding:6px 0;list-style:none}
details.tech summary::-webkit-details-marker{display:none}
details.tech summary:before{content:"\25b8  ";color:#6e7681}
details.tech[open] summary:before{content:"\25be  "}
.bar{display:inline-block;width:52px;height:6px;background:#21262d;border-radius:3px;overflow:hidden;vertical-align:middle;margin-left:7px}
.bar i{display:block;height:100%;background:#3fb950}
.bar i.lo{background:#6e7681}
.filterbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:7px;flex-wrap:wrap;background:#0d1117;border-bottom:1px solid #21262d;padding:10px 0;margin:14px 0 0}
.filterbar .fl{color:#8b949e;font-size:.86em;margin-right:2px}
.fb{background:#11161d;color:#8b949e;border:1px solid #21262d;border-radius:999px;padding:6px 13px;font:600 .82em system-ui,sans-serif;cursor:pointer;min-height:34px}
.fb:hover{color:#e6edf3;border-color:#2b3448}
.fb.active{background:#1e293b;color:#fff;border-color:#3b82f6}
.fcount{color:#6e7681;font-size:.8em;margin-left:4px}
tr.vrow.hid{display:none}
.addr{display:flex;align-items:center;gap:6px}
.ca{font-family:ui-monospace,monospace;font-size:.85em;color:#8b949e}
.cp{background:#1c2230;color:#58a6ff;border:1px solid #2b3448;border-radius:5px;font-size:.72em;padding:3px 7px;cursor:pointer}
.cp:hover{border-color:#58a6ff}.cp.ok{color:#3fb950;border-color:#3fb950}
.howto{background:#11161d;border:1px solid #21262d;border-radius:8px;padding:12px 16px;margin:14px 0}
.howto ol{margin:8px 0 4px 20px;padding:0}.howto li{margin:6px 0;color:#c9d1d9;font-size:.93em;line-height:1.5}
.note b{color:#c9d1d9}
.risk{border-color:#8a6d1f;background:#1a160d;color:#e3b341}
.kpi{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}
.kb{flex:1 1 150px;background:#11161d;border:1px solid #21262d;border-radius:8px;padding:10px}
.kb .kl{font-size:.75em;color:#6e7681;text-transform:uppercase;letter-spacing:.06em}
.kb .kv{font-size:1.25em;font-weight:700;color:#e6edf3;font-variant-numeric:tabular-nums}
#stale{display:none;background:#4a1212;border:1px solid #e74c3c;padding:10px;border-radius:8px;margin:10px 0;font-weight:700}
.foot{margin:26px 0 8px;font-size:.85em;color:#8b949e}
.foot a{color:#58a6ff;text-decoration:none;margin-right:16px}
@media(max-width:760px){body{padding:10px}th,td{padding:8px 6px;font-size:.85em}.foot a{display:inline-block;min-height:44px;line-height:44px}}
"""

_FALLBACK = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
             "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
             "<title>ZD Beefy Yield</title></head>"
             "<body style=\"background:#0d1117;color:#c9d1d9;font-family:sans-serif;"
             "padding:20px\"><h1>\U0001f33e Beefy — APY on fees</h1>"
             "<p style=\"color:#e3b341\">page generation failed: %s</p>"
             "<p>The scheduled rebuild did not complete. Check the workflow run log.</p>"
             "<p><a style=\"color:#58a6ff\" href=\"/\">&larr; Command dashboard</a></p>"
             "</body></html>")


def _e(x):
    return _h.escape(str(x), quote=True)


def _card(title, fn, *a, **kw):
    """Render one card; a failure degrades to an 'unavailable' box."""
    try:
        return fn(*a, **kw)
    except Exception as ex:  # noqa: BLE001
        return ('<h2>%s</h2><div class="note risk">UNAVAILABLE — %s</div>'
                % (_e(title), _e("%s: %s" % (type(ex).__name__, str(ex)[:200]))))


EXPLORER = {
    "ethereum": "https://etherscan.io/address/", "arbitrum": "https://arbiscan.io/address/",
    "base": "https://basescan.org/address/", "optimism": "https://optimistic.etherscan.io/address/",
    "polygon": "https://polygonscan.com/address/", "bsc": "https://bscscan.com/address/",
    "avax": "https://snowscan.xyz/address/", "fraxtal": "https://fraxscan.com/address/",
    "linea": "https://lineascan.build/address/", "hyperevm": "https://hyperevmscan.io/address/",
    "monad": "https://monadexplorer.com/address/", "sonic": "https://sonicscan.org/address/",
    "gnosis": "https://gnosisscan.io/address/", "zksync": "https://era.zksync.network/address/",
    "mantle": "https://mantlescan.xyz/address/", "scroll": "https://scrollscan.com/address/",
}


def _addr_cell(r):
    """Vault contract + the two things you actually need to act: the Beefy page for this
    exact vault, and the address to verify on the chain explorer. Read-only links."""
    a = r.get("vault_addr") or ""
    app = "https://app.beefy.com/vault/" + _e(r["id"])
    if not a:
        return ('<div class=sub><a href="%s" target="_blank" rel="noopener">open on beefy</a>'
                '</div>' % app)
    ex = EXPLORER.get(r["chain"])
    short = a[:6] + "\u2026" + a[-4:]
    exl = ('<a href="%s%s" target="_blank" rel="noopener">verify</a>' % (ex, _e(a))) if ex else ""
    return ('<div class=addr><code class=ca title="%s">%s</code>'
            '<button class=cp type=button data-a="%s">copy</button></div>'
            '<div class=sub><a href="%s" target="_blank" rel="noopener">open on beefy</a>'
            ' %s</div>' % (_e(a), _e(short), _e(a), app, exl))


def _table(sel, d7, show_class=False):
    if not sel:
        return '<div class="note">No vault matched this filter at the current floor.</div>'
    h = ['<div class="scroll"><table><tr>',
         '<th>chain</th><th>vault</th>']
    if show_class:
        h.append('<th>class</th>')
    h.append('<th class=num>TVL</th><th class=num>total APY</th>'
             '<th class=num>real yield<div class=sub>fees / interest</div></th>'
             '<th class=num>promo yield<div class=sub>token emissions</div></th>'
             '<th class=num>how&nbsp;real</th><th class=num>perf fee</th>'
             '<th class=num>wdr fee</th><th class=num>7d &Delta;APY</th>'
             '<th>vault contract<div class=sub>to verify / deposit</div></th></tr>')
    for r in sel:
        tot = r["fee_apr"] + r["inc_apr"]
        share = ("%.0f%%" % (100 * r["fee_apr"] / tot)) if tot > 0 else "&mdash;"
        sharecls = "pos" if tot > 0 and r["fee_apr"] / tot >= 0.5 else "dim"
        d = d7.get(r["id"])
        dcell = ("&mdash;" if d is None else
                 '<span class="%s">%+.2fpp</span>'
                 % ("pos" if d >= 0 else "neg", 100 * d))
        frac0 = (r["fee_apr"] / tot) if tot > 0 else -1
        h.append('<tr class=vrow data-real="%.4f">' % frac0)
        h.append("<td>%s</td>" % _e(r["chain"]))
        h.append('<td><span class=vid>%s</span><div class=sub>%s &middot; %s</div></td>'
                 % (_e(r["id"]), _e("/".join(r["assets"])[:34]), _e(r["type"])))
        if show_class:
            h.append("<td>%s</td>" % _e(r["cls"] or ("LST" if r["lst"] else "-")))
        h.append('<td class=num>%s</td>' % _e(_usd(r["tvl"])))
        h.append('<td class=num><b>%s</b></td>' % _e(_pct(r["apy"])))
        h.append('<td class=num>%s<div class=sub>%s</div></td>'
                 % (_e(_pct(r["fee_apr"])), _e(_parts(r["fee_parts"]) or "-")))
        h.append('<td class=num>%s<div class=sub>%s</div></td>'
                 % (_e(_pct(r["inc_apr"])), _e(_parts(r["inc_parts"]) or "-")))
        frac = (r["fee_apr"] / tot) if tot > 0 else None
        bar = ("" if frac is None else
               '<span class=bar><i class="%s" style="width:%d%%"></i></span>'
               % ("" if frac >= 0.5 else "lo", max(0, min(100, int(round(100 * frac))))))
        h.append('<td class="num %s">%s%s</td>' % (sharecls, share, bar))
        h.append('<td class=num>%s</td>'
                 % (_e(_pct(r["perf"], 1)) if r["has_fee_row"] else "&mdash;"))
        h.append('<td class=num>%s</td>'
                 % (_e(_pct(r["wdr"], 3)) if r["has_fee_row"] else "&mdash;"))
        h.append('<td class=num>%s</td>' % dcell)
        h.append('<td>%s</td>' % _addr_cell(r))
        h.append("</tr>")
    h.append("</table></div>")
    return "".join(h)


def _n_qual(rows, floor, cls_set=None, lst=False):
    return len([r for r in rows if r["tvl"] >= floor
                and ((cls_set and r["cls"] in cls_set) or (lst and r["lst"]))])


def _card_a(rows, d7):
    sel = select(rows, cls_set={"STABLE"}, min_tvl=MIN_TVL_A)
    n = _n_qual(rows, MIN_TVL_A, {"STABLE"})
    n_lo = _n_qual(rows, 100_000, {"STABLE"})
    return ('<h2>\U0001f4b5 A &middot; DOLLAR-STABLE VAULTS</h2>'
            '<p class="lead">Deposits that stay worth a dollar, so the yield is the whole story '
            'and nothing here depends on a coin going up. This is where the real-versus-promo '
            'split matters most: the top of the list is usually a promotion, and the honest '
            'rows sit lower down with a full green bar.</p>'
            '<details class="tech"><summary>Technical detail &mdash; how this list is '
            'filtered</summary><div class="note">Top %d of <b>%d</b> active USD-stablecoin vaults with TVL '
            '&ge; %s, ranked by <b>total APY</b>. Every asset in the pair must be a '
            'USD-pegged stablecoin (EUR/BRL/CAD pegs excluded). %d qualify if the floor '
            'drops to $100k. Read the <b>fee share</b> column: a low number means the '
            'headline APY is emissions that can be switched off.</div></details>%s'
            % (len(sel), n, _usd(MIN_TVL_A), n_lo, _table(sel, d7)))


def _card_b(rows, d7):
    sel = select(rows, cls_set={"ETH", "BTC", "SOL"}, lst=True, min_tvl=MIN_TVL_B)
    n_1m = _n_qual(rows, 1_000_000, {"ETH", "BTC", "SOL"}, lst=True)
    n = _n_qual(rows, MIN_TVL_B, {"ETH", "BTC", "SOL"}, lst=True)
    return ('<h2>₿ B &middot; ETH / BTC / SOL VAULTS</h2>'
            '<p class="lead">The same question for the coins we actually follow. One thing to '
            'say out loud: <b>you are paid in that coin, not in dollars.</b> A 9%% yield on ETH '
            'still loses money if ETH falls 20%%, and a two-coin pool can end up holding more of '
            'whichever coin fell. The yield is a side income, never the reason to hold.</p>'
            '<details class="tech"><summary>Technical detail &mdash; how this list is '
            'filtered</summary><div class="note">Top %d of <b>%d</b> by total APY, TVL &ge; %s. A vault '
            'qualifies when <i>every</i> asset is in the ETH, BTC or SOL family, or when any '
            'asset is a liquid-staking token. <b>Floor is %s here, not the $1M used in card '
            'A</b> &mdash; only %d volatile-asset vault(s) on the whole platform clear $1M, '
            'so a $1M card would show %d rows.</div></details>'
            '<div class="note risk">Denominated in the volatile asset: the APY is paid in ETH '
            '/ BTC / SOL, not in USD, and says nothing about the price of the underlying. '
            'Two-asset pairs also carry impermanent loss.</div>%s'
            % (len(sel), n, _usd(MIN_TVL_B), _usd(MIN_TVL_B), n_1m, n_1m,
               _table(sel, d7, show_class=True)))


def _card_c(rows, d7):
    sel = select(rows, chain="hyperevm", n=40)
    if not sel:
        return ('<h2>⚡ C &middot; HYPEREVM</h2>'
                '<div class="note">No active Beefy vault on HyperEVM in this snapshot.</div>')
    tot = sum(r["tvl"] for r in sel)
    return ('<h2>⚡ C &middot; HYPEREVM</h2>'
            '<p class="lead">Beefy\'s footprint on HyperEVM, a newer chain with a small but active '
            'DeFi surface. The '
            'percentages look spectacular and the deposits are tiny \u2014 a few thousand '
            'dollars in most rows \u2014 which is <i>why</i> they look spectacular. A big number '
            'on a small pool is a rounding error, not an opportunity.</p>'
            '<details class="tech"><summary>Technical detail &mdash; scope of this '
            'list</summary><div class="note">All %d active HyperEVM (chainId 999) vaults, no TVL floor. '
            'Chain TVL in this set: <b>%s</b>. These are almost all concentrated-liquidity '
            '(CLM) positions whose deposits sit in the paired <code>-rp</code> reward pool.</div></details>'
            '<div class="note risk">These are volatile-pair CLM positions. The quoted APR is '
            'fee income on the current range; it does <b>not</b> net out impermanent loss or '
            'the position going out of range. Small TVL means a single withdrawal moves the '
            'number.</div>%s'
            % (len(sel), _usd(tot), _table(sel, d7, show_class=True)))


def _card_e(rows, d7):
    sel = select(rows, eq=True, n=20)
    if not sel:
        return ('<h2>\U0001f3e6 E &middot; TOKENIZED EQUITIES</h2>'
                '<p class="lead">No Beefy vault currently holds a tokenized stock.</p>')
    tot = sum(r["tvl"] for r in sel)
    n_fee = sum(1 for r in sel if r["fee_apr"] > 0)
    chains = ", ".join(sorted({r["chain"] for r in sel}))
    return ('<h2>\U0001f3e6 E &middot; TOKENIZED EQUITIES</h2>'
            '<p class="lead">Vaults holding a tokenized share \u2014 NVIDIA, Microsoft, Apple, Meta, '
            'Amazon, Tesla and the like \u2014 paired against a stablecoin. <b>%d vaults, %s in '
            'total, on %s.</b> This corner behaves differently from the rest of the page: '
            '<b>%d of %d earn from real trading activity</b> rather than token emissions, because '
            'people actually swap these pairs. That is the opposite of what the stablecoin and '
            'crypto cards show.</p>'
            '<div class="note risk"><b>Read the yields here with more suspicion, not less.</b> '
            'The fee figures on the concentrated-liquidity rows are a <b>trailing ~1.1-day fee '
            'window, annualised</b>. A row reading 200%% is one busy day extrapolated across a '
            'year, not a rate you can earn \u2014 the same vault can read a third of that '
            'tomorrow with nothing having changed. Treat these as a measure of how busy the pair '
            'was yesterday.</div>'
            '<div class="note risk"><b>Two hazards specific to tokenized stocks.</b> (1) These are '
            'two-sided pools, so if the stock runs you end up holding less of the stock and more '
            'of the stablecoin \u2014 the fees are paid for out of the upside you were there for. '
            '(2) A tokenized share is an issuer\'s claim, not the share: it adds that issuer\u2019s '
            'solvency and redemption terms on top of every other risk on this page. Beefy is not '
            'the issuer and does not vouch for the wrapper.</div>%s'
            % (len(sel), _usd(tot), chains, n_fee, len(sel),
               _table(sel, d7, show_class=True)))


def _card_d(stats, gen_utc):
    m = stats["meta"]
    kb = [("total TVL", _usd(stats["total_tvl"])),
          ("active vaults", "{:,}".format(stats["n_active"])),
          ("chains w/ active", str(stats["n_chains"])),
          ("rows ranked", "{:,}".format(stats["n_rows"]))]
    h = ['<h2>\U0001f5c4️ D &middot; PLATFORM</h2>'
         '<p class="lead">How big Beefy is, and whether the data on this page actually arrived '
         'this time. If a source below says FAILED, treat every table above as stale.</p>'
         '<div class="kpi">']
    for l, v in kb:
        h.append('<div class=kb><div class=kl>%s</div><div class=kv>%s</div></div>'
                 % (_e(l), _e(v)))
    h.append("</div>")

    ty = ", ".join("%s %d" % (k, v) for k, v in
                   sorted(stats["types"].items(), key=lambda x: -x[1]))
    h.append('<div class="note">Active by type: <b>%s</b> &nbsp;&middot;&nbsp; EOL/retired '
             'skipped: <b>%s</b>. Gov (<code>-rp</code>) rows are folded into their CLM '
             'parent, which is where their TVL actually sits.</div>'
             % (_e(ty), "{:,}".format(stats["n_eol"])))

    h.append('<div class="scroll"><table><tr><th>endpoint</th><th>source</th>'
             '<th class=num>age</th><th>error</th></tr>')
    for k in ("vaults", "breakdown", "tvl", "fees", "boosts", "boost_apy"):
        d = m.get(k) or {"src": "not fetched", "age_s": None, "err": None}
        cls = {"live": "b-GREEN", "cache": "b-INFO",
               "stale-cache": "b-AMBER", "FAILED": "b-RED"}.get(d["src"], "b-INFO")
        age = "--" if d["age_s"] is None else "%.0f s" % d["age_s"]
        h.append('<tr><td class=vid>%s</td><td><span class="bd %s">%s</span></td>'
                 '<td class=num>%s</td><td class=err>%s</td></tr>'
                 % (_e(k), cls, _e(d["src"]), _e(age), _e(d["err"] or "")))
    h.append("</table></div>")

    top = sorted(stats["chain_tvl"].items(), key=lambda x: -x[1])[:8]
    h.append('<div class="note">Top chains by TVL: %s</div>'
             % _e(" · ".join("%s %s" % (c, _usd(v)) for c, v in top)))

    if stats["n_outliers"]:
        ex = ", ".join("%s (%.3g%%, %s)" % (i, 100 * a, _usd(t))
                       for i, a, t in stats["outliers"])
        h.append('<div class="note risk"><b>%d vault(s) excluded</b> as broken data '
                 '(totalApy &gt; %d%%; the API returns values up to 1e25%% unfiltered). '
                 'Largest by TVL: %s</div>' % (stats["n_outliers"], int(100 * MAX_APY), _e(ex)))
    h.append('<div class="note">Page generated %s. Source: '
             '<code>api.beefy.finance</code>, keyless read-only. Upstream refresh: APY 15 min, '
             'TVL 15 min, vault list 5 min, fees 5 min, CDN edge 600 s &mdash; polling faster '
             'than the rebuild interval buys nothing.</div>' % _e(gen_utc))
    return "".join(h)


_INTRO = (
    '<div class="explain">'
    '<h2 class="plain">What you are looking at</h2>'
    '<p><b>Beefy is a yield-farm manager.</b> You deposit a token; it puts that token to work in '
    'a lending market or a trading pool, collects the rewards, sells them, reinvests them, and '
    'repeats \u2014 keeping 9.5% of each harvest. It is a separate world from our perp trading: '
    'nothing on this page touches our wallets or our bots.</p>'
    '<p><b>This page ranks every live Beefy vault by where its yield actually comes from.</b> '
    'It reads Beefy\'s public data twice an hour. No account, no key, no money at risk.</p>'
    '<div class="keyidea"><b>The one idea that makes every table below readable.</b>'
    '<p style="margin:6px 0">A headline APY blends two things that behave nothing alike:</p><ul>'
    '<li><b class="pos">Real yield</b> \u2014 money other people pay you: swap fees, loan '
    'interest. It keeps paying for as long as the pool is used.</li>'
    '<li><b class="dim">Promo yield</b> \u2014 freshly minted tokens handed out to attract '
    'deposits. It stops when the campaign stops, and most people receiving it sell it.</li>'
    '</ul><p style="margin:6px 0">So a vault paying <b>9%</b> that is 95% promo is a weaker '
    'proposition than one paying <b>4.7%</b> that is 100% real. The <b>how real</b> column is '
    'exactly that ratio \u2014 green bar means most of the yield is real.</p></div>'
    '<p style="margin-top:12px;color:#8b949e;font-size:.9em">Each section below opens with one '
    'line on what it tells you. The exact formulas are folded into the grey '
    '\u201ctechnical detail\u201d links \u2014 open them only if you want them.</p>'
    '</div>')


_FILTER = (
    '<div class="filterbar"><span class="fl">how real:</span>'
    '<button class="fb active" data-min="-1">all</button>'
    '<button class="fb" data-min="0.25">25%+</button>'
    '<button class="fb" data-min="0.5">50%+</button>'
    '<button class="fb" data-min="0.8">80%+</button>'
    '<button class="fb" data-min="1">100% only</button>'
    '<span class="fcount" id="fcount"></span></div>')


_HOWTO = (
    '<div class="howto"><b>\U0001f9ed If you ever decide to act on one of these</b>'
    '<p style="margin:6px 0;color:#8b949e;font-size:.9em">Copying a contract address into a '
    'wallet does <b>not</b> deposit anything \u2014 it only makes the token visible. A deposit '
    'is two on-chain transactions (approve, then deposit) and is normally done through Beefy\'s '
    'own site with a wallet connected.</p><ol>'
    '<li><b>The address in each row is the vault contract</b> (Beefy calls it '
    '<code>earnContractAddress</code>). Paste it into a block explorer with <i>verify</i>, or '
    'into a wallet\'s \u201cimport token\u201d box to watch the receipt token. Never send '
    'tokens to it directly.</li>'
    '<li><b>Check the address against Beefy\u2019s own page first</b> \u2014 the '
    '<i>open on beefy</i> link goes to that exact vault. If the address on their page differs '
    'from the one here, stop; do not proceed on the strength of this page alone.</li>'
    '<li><b>Getting funds there from our venue is the real cost.</b> Perp collateral is USDC on '
    'one chain; almost every vault above is on another. That means withdraw, bridge, and gas at '
    'both ends, each way. On a few hundred dollars those costs can exceed a year of the yield '
    '\u2014 work out the break-even before, not after.</li>'
    '<li><b>Deposit is approve + deposit on Beefy\u2019s site.</b> You receive a receipt token '
    '(a \u201cmoo\u201d token) whose value per share grows as the strategy compounds. You get '
    'the deposit back by withdrawing on the same site.</li>'
    '<li><b>What this page cannot tell you:</b> whether the yield survives your holding period, '
    'the price move of the underlying, impermanent loss on a two-coin pool, or contract risk. '
    'It is a screen, not a diligence report.</li>'
    '</ol></div>')


_LEGEND = (
    '<details class="tech"><summary>Technical detail &mdash; how the two yield columns are '
    'computed, and the traps</summary>'
    '<div class="note"><b>How the two APR columns are defined.</b> '
    '<span class="pos">fee APR</span> = <code>tradingApr + clmApr + rewardPoolTradingApr '
    '+ lendingApr</code> &mdash; yield paid by counterparties (swappers, borrowers). '
    '<span class="dim">incentive APR</span> = <code>vaultApr + merklApr + rewardPoolApr '
    '+ liquidStakingApr + composablePoolApr + lineaIgnitionApr + stellaSwapApr + boost</code> '
    '&mdash; token issuance and campaigns, which stop when the programme stops. '
    '<b>vaultApr is farm emissions, not fees</b>, which is why it sits on the incentive side. '
    'The two columns are components: they do not sum to total APY, because Beefy compounds '
    'the compoundable parts &mdash; <code>totalApy</code> is the authoritative headline.</div>'
    '<div class="note"><b>Fee basis trap:</b> <code>clmApr</code>, <code>vaultApr</code> and '
    '<code>lendingApr</code> are already <b>net</b> of the Beefy performance fee; '
    '<code>tradingApr</code>, <code>merklApr</code> and the reward-pool APRs are gross. '
    'Never re-apply the perf fee to <code>totalApy</code>. A <b>&mdash;</b> in the perf/wdr '
    'columns means the vault has no <code>/fees</code> row at all (every gov reward pool); '
    'it is not a zero. <b>boost</b> APR only accrues if you separately stake the moo token '
    'in the boost contract. <b>7d &Delta;APY</b> is computed from this page\'s own daily '
    'samples (the API publishes no APY history), so it reads &mdash; until the workflow has run '
    'for a week.</div>'
    '<div class="note risk"><b>What these numbers are not.</b> An advertised APY is a forward '
    'extrapolation of a recent window, not a realized return. It excludes impermanent loss, '
    'the price move of the underlying, gas, bridging, and smart-contract risk. Stablecoin '
    'rows carry depeg risk. Nothing here is a recommendation to deploy capital.</div>'
    '</details>')


def render(rows, stats, d7, gen_ms, gen_utc):
    body = []
    body.append(_card("A", _card_a, rows, d7))
    body.append(_card("B", _card_b, rows, d7))
    body.append(_card("C", _card_c, rows, d7))
    body.append(_card("E", _card_e, rows, d7))
    body.append(_card("D", _card_d, stats, gen_utc))
    bad = [k for k, v in stats["meta"].items() if v["src"] in ("FAILED", "stale-cache")]
    if bad:
        verdict, bg, bd = "DEGRADED", "#4a3a10", "#f1c40f"
    else:
        verdict, bg, bd = "LIVE", "#123f22", "#2ecc71"
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            "<link rel=icon type=\"image/svg+xml\" href=\"/favicon.svg\">\n"
            "<title>ZD Beefy Yield</title><style>" + _CSS + "</style></head><body>\n"
            '<h1>'
            + "\U0001f33e Beefy — where the yield comes from</h1>\n"
            '<div><span class="badge" style="background:' + bg + ";border:1px solid "
            + bd + '">' + verdict + '</span><span class="ts">generated '
            + _e(gen_utc) + "</span></div>\n"
            '<div id="stale">STALE PAGE — older than ' + str(STALE_MIN) +
            " min &mdash; the scheduled rebuild has not run. See the workflow log.</div>\n"
            + _INTRO + _HOWTO + _LEGEND + _FILTER + "".join(body) +
            ('\n<div class="foot"><span>Data from the public api.beefy.finance, read-only '
               'and keyless. Figures are point-in-time and move fast. Not investment advice.'
               '</span></div>\n' if PUBLIC else
               '\n<div class="foot"><a href="/">&larr; Command dashboard</a>'
               '<a href="/morning.html">Morning check</a>'
               '<a href="/divergence.html">Divergence</a>'
               '<span>read-only, keyless · api.beefy.finance</span></div>\n') +
            "<script>var GEN=" + str(gen_ms) + ";function bchk(){"
            "var m=(Date.now()-GEN)/60000;if(GEN&&m>" + str(STALE_MIN) + "){"
            "document.getElementById('stale').style.display='block';}}"
            "bchk();setInterval(bchk,15000);"
            "(function(){var bs=document.querySelectorAll('.fb');"
            "function apply(min){var rows=document.querySelectorAll('tr.vrow'),hid=0;"
            "for(var i=0;i<rows.length;i++){var v=parseFloat(rows[i].getAttribute('data-real'));"
            "var show=(min<0)||(v>=0&&(min>=1?v>=0.999:v>=min));"
            "rows[i].classList.toggle('hid',!show);if(!show)hid++;}"
            "var c=document.getElementById('fcount');"
            "c.textContent=hid?(hid+' row'+(hid==1?'':'s')+' hidden'):'';}"
            "for(var i=0;i<bs.length;i++){bs[i].addEventListener('click',function(e){"
            "for(var j=0;j<bs.length;j++){bs[j].classList.remove('active');}"
            "e.target.classList.add('active');apply(parseFloat(e.target.getAttribute('data-min')));});}"
            "})();"
            "document.addEventListener('click',function(e){var b=e.target.closest('.cp');"
            "if(!b)return;var t=b.getAttribute('data-a');"
            "function done(){var o=b.textContent;b.textContent='copied';b.classList.add('ok');"
            "setTimeout(function(){b.textContent=o;b.classList.remove('ok');},1200);}"
            "if(navigator.clipboard&&navigator.clipboard.writeText){"
            "navigator.clipboard.writeText(t).then(done,function(){});}else{"
            "var x=document.createElement('textarea');x.value=t;document.body.appendChild(x);"
            "x.select();try{document.execCommand('copy');done();}catch(err){}"
            "document.body.removeChild(x);}});</script>"
            "</body></html>")


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Build /beefy.html from the Beefy public API.")
    ap.add_argument("--once", action="store_true",
                    help="run a single pass (the default; no daemon mode exists)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the tables to stdout, write no html and no history")
    ap.add_argument("--no-cache", action="store_true", help="ignore the on-disk cache")
    ap.add_argument("--out", default=None, help="override the output path")
    a = ap.parse_args()

    t0 = time.time()
    now = time.time()
    gen_utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fetch = Fetcher(use_cache=not a.no_cache)
    rows, stats = build(fetch)
    if not a.dry_run:
        fetch.flush()
    d7 = update_history(rows, now, write=not a.dry_run)

    if a.dry_run:
        for k in ("vaults", "breakdown", "tvl", "fees", "boosts", "boost_apy"):
            d = fetch.meta.get(k, {})
            sys.stderr.write("  %-10s %-12s %s\n" % (k, d.get("src"), d.get("err") or ""))
        sys.stderr.write("built %d rows in %.2fs (%d outliers dropped)\n"
                         % (len(rows), time.time() - t0, stats["n_outliers"]))
        print_table("A. STABLECOIN VAULTS -- top %d by APY, TVL >= %s"
                    % (TOP_N, _usd(MIN_TVL_A)),
                    select(rows, cls_set={"STABLE"}, min_tvl=MIN_TVL_A), d7)
        print_table("B. ETH/BTC/SOL + LST VAULTS -- top %d by APY, TVL >= %s"
                    % (TOP_N, _usd(MIN_TVL_B)),
                    select(rows, cls_set={"ETH", "BTC", "SOL"}, lst=True,
                           min_tvl=MIN_TVL_B), d7)
        print_table("C. HYPEREVM -- all active, no TVL floor",
                    select(rows, chain="hyperevm", n=40), d7)
        print("\n" + "=" * 150)
        print("D. PLATFORM")
        print("=" * 150)
        print("total TVL      %s" % _usd(stats["total_tvl"]))
        print("active vaults  %d (%s)   EOL skipped %d"
              % (stats["n_active"],
                 ", ".join("%s %d" % kv for kv in sorted(stats["types"].items(),
                                                         key=lambda x: -x[1])),
                 stats["n_eol"]))
        print("chains         %d" % stats["n_chains"])
        print("outliers       %d dropped at totalApy > %d%%"
              % (stats["n_outliers"], int(100 * MAX_APY)))
        for i, av, t in stats["outliers"]:
            print("               %-46s %.3g%%  %s" % (i, 100 * av, _usd(t)))
        print("generated      %s" % gen_utc)
        return 0

    page = render(rows, stats, d7, int(now * 1000), gen_utc)
    dest = a.out or HTML_OUT
    atomic_write(dest, page)
    sys.stderr.write("wrote %s (%d bytes, %d rows, %.2fs)\n"
                     % (dest, len(page), len(rows), time.time() - t0))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001  the page must never simply vanish
        msg = "%s: %s" % (type(e).__name__, str(e)[:300])
        sys.stderr.write("FATAL %s\n" % msg)
        try:
            if "--dry-run" not in sys.argv:
                atomic_write(HTML_OUT, _FALLBACK % _h.escape(msg))
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
