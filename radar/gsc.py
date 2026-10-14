"""Signal ④ (optional): queries your own site got impressions for, for the first time.

Search Console, fresh data (about a day behind instead of three; the numbers are revised
later, which is fine for spotting). A query is new when it has impressions in the last
RECENT days and none in the BEFORE days ahead of them.

Set in config.json:

    "gsc": {"site": "sc-domain:example.com", "token_file": "gsc_token.json"}

`site` may also be a list of properties, or "all": every property the token can read
(xhorizon fork). Each site is read on its own, so one failing site does not drop the
others; their new queries are merged, at most `per_site` (default 5) per site, and the
landing page shows which site it was. `min_impressions` (default 5) is the floor for a row;
small sites rarely reach 5 in three days, so with many sites 2 is a better floor.

`token_file` is a Google "authorized user" JSON holding client_id, client_secret and
refresh_token for the scope webmasters.readonly (what google-auth's
Credentials.to_json() writes; see the README). It is read from the config's directory,
or from the environment variable GSC_TOKEN_JSON when the file is missing (for CI).
Queries that use search operators (quotes, site:, -word) are dropped: those are tools
probing the results, not people.
"""
from __future__ import annotations

import json
from collections import Counter
import os
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import UA

LAG_DAYS = 1
RECENT, BEFORE = 3, 60
MIN_IMPRESSIONS = 5
TOP = 30
PER_SITE = 5          # with several sites: at most this many rows per site
ROW_LIMIT = 25000
_OPERATOR = re.compile(r'"|\b(site|inurl|intitle|intext|filetype):|(^|\s)-\w|\*|\bOR\b')


def _token(cfg: dict) -> str:
    path = cfg["_dir"] / cfg["gsc"].get("token_file", "gsc_token.json")
    raw = path.read_text() if path.exists() else os.environ.get("GSC_TOKEN_JSON", "")
    if not raw:
        raise RuntimeError(f"no Search Console token: {path.name} or GSC_TOKEN_JSON")
    tok = json.loads(raw)
    body = urllib.parse.urlencode({"client_id": tok["client_id"], "client_secret": tok["client_secret"],
                                   "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(tok.get("token_uri") or "https://oauth2.googleapis.com/token", data=body,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)["access_token"]


def _query(access: str, site: str, start: date, end: date, dimensions: list[str], fresh: bool) -> list[dict]:
    body = {"startDate": str(start), "endDate": str(end), "dimensions": dimensions, "rowLimit": ROW_LIMIT}
    if fresh:
        body["dataState"] = "all"
    url = f"https://www.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(site, safe='')}/searchAnalytics/query"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {access}", "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as res:
        rows = json.load(res).get("rows", [])
    return [{**dict(zip(dimensions, r["keys"])), "impressions": int(r["impressions"]), "clicks": int(r["clicks"])}
            for r in rows if not _OPERATOR.search(r["keys"][0])]


def fresh_queries(recent: list[dict], before: list[dict]) -> list[dict]:
    """Queries in `recent` (query, page rows) that `before` never saw, summed per query."""
    seen = {r["query"] for r in before}
    fresh: dict[str, dict] = {}
    for r in recent:
        if r["query"] in seen:
            continue
        f = fresh.setdefault(r["query"], {"query": r["query"], "impressions": 0, "clicks": 0, "page": r["page"]})
        f["impressions"] += r["impressions"]
        f["clicks"] += r["clicks"]
    return sorted(fresh.values(), key=lambda f: -f["impressions"])


READABLE = {"siteOwner", "siteFullUser", "siteRestrictedUser"}


def _sites(access: str) -> list[str]:
    """Every Search Console property the token can read."""
    req = urllib.request.Request("https://www.googleapis.com/webmasters/v3/sites",
                                 headers={"Authorization": f"Bearer {access}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as res:
        entries = json.load(res).get("siteEntry", [])
    return sorted(e["siteUrl"] for e in entries if e.get("permissionLevel") in READABLE)


def _site_new(access: str, site: str, start: date, end: date) -> list[dict]:
    recent = _query(access, site, start, end, ["query", "page"], fresh=True)
    before = _query(access, site, start - timedelta(days=BEFORE), start - timedelta(days=1), ["query"], fresh=False)
    return [{**f, "site": site} for f in fresh_queries(recent, before)]


def gsc_new(cfg: dict) -> dict:
    site = cfg["gsc"].get("site")
    if not site:
        return {"off": True}
    access = _token(cfg)
    sites = _sites(access) if site == "all" else ([site] if isinstance(site, str) else list(site))
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=RECENT - 1)
    fresh, errors = [], {}
    for s in sites:
        try:
            fresh += _site_new(access, s, start, end)
        except Exception as exc:  # noqa: BLE001 — one site down must not stop the others
            errors[s] = str(exc)[:120]
    if errors and len(errors) == len(sites):
        raise RuntimeError("; ".join(f"{s}: {e}" for s, e in errors.items()))
    fresh.sort(key=lambda f: -f["impressions"])
    floor = cfg["gsc"].get("min_impressions", MIN_IMPRESSIONS)
    shown = [f for f in fresh if f["impressions"] >= floor]
    if len(sites) > 1:
        # One growing site would fill the table: keep each site's top `per_site`.
        cap, per, kept = cfg["gsc"].get("per_site", PER_SITE), Counter(), []
        for f in shown:
            per[f["site"]] += 1
            if per[f["site"]] <= cap:
                kept.append(f)
        shown = kept
    else:
        shown = shown[:TOP]
    out = {"window": f"{start}..{end}", "total_new": len(fresh), "min_impressions": floor,
           "sites": sites, "queries": shown}
    if errors:
        out["site_errors"] = errors
    return out
