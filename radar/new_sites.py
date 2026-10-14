"""Signal ③b: new sites that already draw search traffic.

A word that catches on gets sites built for it: ai-hailuo.com was registered in August
2026 for the Hailuo video model and ranked for 258 keywords within weeks. DataForSEO's
domain whois overview (through AIsa) finds such domains: registered AND first seen by
DataForSEO within `window_days`, with estimated organic traffic over `min_etv` and at
least `min_keywords` ranking keywords. Both dates are needed: whois creation dates get
rewritten (dagbladet.no, a 2020 site, showed a July 2026 "created"). The keyword floor
drops gambling and parked domains, which rank for a handful of brand terms.

xhorizon fork: with DATAFORSEO_AUTH ("login:password") set, the query goes straight to
DataForSEO instead of through AIsa, whose parameter check has rejected these filters since
2026-10-06. Same endpoint and body; $0.12 a request plus $0.0012 a result (Oct 2026).

One query per entry of config.json's new_sites.queries (a name and a SQL LIKE pattern
on the domain, e.g. "%ai%"), about $0.13 each, so the radar runs this once a week
(new_sites.weekday, 0 = Monday). Each site is reported once; the words of its name go
to the name judge with the other leads ("hailuo").
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from . import UA, aisa, snap
from .i18n import t
from .terms import STOP

TOOL = "post_dataforseo_domains_whois_overview_live"
DIRECT = "https://api.dataforseo.com/v3/domain_analytics/whois/overview/live"
LIMIT = 30
DOMAIN_STOP = {"com", "net", "org", "app", "io", "co", "online", "free", "tool", "tools", "the", "my", "get",
               "official", "site", "hub", "pro", "best", "video", "videos"}


def name_terms(domain: str) -> list[str]:
    """'ai-hailuo.com' -> ['hailuo']; 'livephoto.video' -> ['livephoto']."""
    label = domain.rsplit(".", 1)[0] if domain.count(".") >= 1 else domain
    words = [w for w in re.split(r"[-_.]+", label.lower()) if w]
    return [w for w in words if w not in STOP and w not in DOMAIN_STOP and len(w) >= 4 and not w.isdigit()]


def keep(item: dict, min_etv: int = 1000, min_keywords: int = 20) -> bool:
    organic = (item.get("metrics") or {}).get("organic") or {}
    return (organic.get("etv") or 0) > min_etv and (organic.get("count") or 0) >= min_keywords


def site_row(item: dict, query: str) -> dict:
    organic = (item.get("metrics") or {}).get("organic") or {}
    return {"domain": item["domain"], "query": query, "created": str(item.get("created_datetime"))[:10],
            "first_seen": str(item.get("first_seen"))[:10], "etv": int(organic.get("etv") or 0),
            "keywords": int(organic.get("count") or 0)}


def _calls(c: dict, today: date) -> list[dict]:
    since = (today - timedelta(days=c["window_days"])).isoformat() + " 00:00:00 +00:00"
    return [{"call_id": name, "tool": TOOL, "arguments": {"body": [{
        "limit": LIMIT, "order_by": ["metrics.organic.etv,desc"],
        "filters": [["created_datetime", ">", since], "and", ["first_seen", ">", since], "and",
                    ["metrics.organic.etv", ">", c["min_etv"]], "and", ["domain", "like", pattern]]}]}}
        for name, pattern in c["queries"].items()]


def _direct(calls: list[dict], auth: str) -> tuple[dict[str, dict], int]:
    """The same calls sent to DataForSEO itself: ({call_id: response body}, cost in micro-USD)."""
    headers = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode(),
               "Content-Type": "application/json", "User-Agent": UA}
    out, cost = {}, 0
    for c in calls:
        req = urllib.request.Request(DIRECT, data=json.dumps(c["arguments"]["body"]).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                body = json.load(res)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"DataForSEO HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:160]}") from exc
        task = (body.get("tasks") or [{}])[0]
        cost += round(float(task.get("cost") or 0) * 1e6)
        if task.get("status_code") != 20000:
            raise RuntimeError(f"DataForSEO {task.get('status_code')}: {task.get('status_message')}")
        out[c["call_id"]] = body
    return out, cost


def new_sites(cfg: dict, state: Path, today: date | None = None) -> dict:
    c = cfg["new_sites"]
    today = today or date.today()
    auth = os.environ.get("DATAFORSEO_AUTH", "").strip()
    data, cost = _direct(_calls(c, today), auth) if auth else aisa.call_all(_calls(c, today))
    sites: dict[str, dict] = {}
    for name in c["queries"]:
        task = ((data.get(name) or {}).get("tasks") or [{}])[0]
        for item in ((task.get("result") or [{}])[0] or {}).get("items") or []:
            if keep(item, c["min_etv"], c["min_keywords"]) and item["domain"] not in sites:
                sites[item["domain"]] = site_row(item, name)
    path = state / "new_sites.json.gz"
    seen: dict[str, str] = snap.load(path) or {}
    rows = sorted(sites.values(), key=lambda r: -r["etv"])
    for r in rows:
        r["new"] = r["domain"] not in seen
        seen.setdefault(r["domain"], today.isoformat())
    if data:
        snap.save(path, seen)
    terms = [{"term": w, "recent": 1, "examples": [f"{r['domain']} · {r['keywords']} keywords"],
              "note": t("note_site", domain=r["domain"], created=r["created"], etv=r["etv"], keywords=r["keywords"])}
             for r in rows if r["new"] for w in name_terms(r["domain"])]
    return {"queries": len(c["queries"]), "answered": len(data), "cost_micros": cost, "sites": rows, "terms": terms}
