"""Offline, synthetic before/after report. No market data or API calls."""
from __future__ import annotations
import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "work"))
import fetch_official_data as current
import fundamental_store as store

REFERENCE = "d180ee9"
NOW = "2026-10-19T16:10:00+09:00"


def baseline_module(path):
    source = subprocess.run(["git", "show", f"{REFERENCE}:{path}"], cwd=ROOT, check=True, capture_output=True).stdout.decode("utf-8")
    namespace = {"__name__": "fixture_baseline", "__file__": str(ROOT / path)}
    exec(compile(source, path, "exec"), namespace)
    return namespace


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 19)


def report():
    old = baseline_module("work/fetch_official_data.py")
    old_calc = baseline_module("work/edinet_connector.py")["calculate_valuation_metrics"]
    old["date"] = FixedDate
    rows = []
    state = store.seed_state({})
    labels = ["complete", "new-financial-data", "zero-sell-missing-dps", "zero-dividend"]
    for i, label in enumerate(labels):
        code = str(1000 + i)
        row = {"code": code, "name": label, "industry": "fixture", "isStandard": True,
               "latestClose": 2000., "priceAsOf": NOW[:10], "margin": None if i == 2 else 2.,
               "theme": 60, "technical": 75, "monthsFromHigh": .1, "earnings": 70,
               "risk": 35, "relative": 70, "liquidity": 80, "averageTurnover20": 100000000., "return20": .05,
               "sources": {key: {"url": "https://example.test", "updatedAt": NOW, "status": "available"}
                           for key in ("edinet", "dividend", "marginRatio", "priceHistory")}}
        facts = {"eps": 100. + i * 10, "bps": 1000., "roe": .1, "profit": 100., "equity": 1000.,
                 "dps": 20. if i < 2 else None if i == 2 else 0., "asOf": "2026-03-31", "url": "https://example.test"}
        facts.update(actualDps=facts["dps"], actualDpsAsOf=facts["asOf"])
        forecast = {"dps": 40. if i < 2 else 0., "dpsKind": "forecast", "dividendYield": .02 if i < 2 else 0.,
                    "dividendYieldKind": "forecast", "dpsUrl": "https://example.test"} if i != 2 else {}
        state["companies"][code] = {"financial": facts,
            "forecast": store.merge_forecast({}, forecast, NOW),
            "edinetAttempt": {"status": "available", "at": NOW}, "yahooAttempt": {"status": "available", "at": NOW}}
        rows.append(row)
    before = {"searchUniverse": copy.deepcopy(rows)}
    for i, row in enumerate(before["searchUniverse"]):
        if i == 1:
            row["sources"].pop("edinet")
            row["sources"].pop("dividend")
            continue
        entry = state["companies"][row["code"]]
        facts = dict(entry["financial"])
        facts.pop("actualDps")
        facts.pop("actualDpsAsOf")
        if i == 3:
            facts["dps"] = None
        else:
            facts.update({k:v for k,v in entry["forecast"].items() if k != "observedAt"})
        row["fundamentals"] = old_calc(facts, row["latestClose"])
        row.update({k:row["fundamentals"].get(k) for k in store.FIELDS})
        if i == 2:
            row["sources"]["marginRatio"]["status"] = "unavailable"
    old["_attach_scores"](before)
    after = {"searchUniverse": copy.deepcopy(rows)}
    after["searchUniverse"][2].update(marginRatioStatus="zero-sales", marginSales=0, marginPurchases=1000)
    store.attach_facts(after, state, NOW)
    original_date = current.date
    try:
        current.date = FixedDate
        current._attach_scores(after)
    finally:
        current.date = original_date
    ranks = []
    for data in (before, after):
        ranks.append({row["code"]: i + 1 for i, row in enumerate(sorted(data["searchUniverse"], key=lambda r:(-r["score"], r["code"])))})
    result = []
    for left, right in zip(before["searchUniverse"], after["searchUniverse"]):
        result.append({"fixture": left["name"], "scoreBefore": left["score"], "scoreAfter": right["score"],
                       "rankBefore": ranks[0][left["code"]], "rankAfter": ranks[1][right["code"]],
                       "doeBefore": left.get("doe"), "doeAfter": right.get("doe")})
    assert result[2]["doeBefore"] == .1 and result[2]["doeAfter"] is None
    assert result[3]["doeAfter"] == 0
    assert result[1]["scoreAfter"] > result[1]["scoreBefore"]
    return {"basis": "synthetic fixtures, not actual securities", "baselineCommit": REFERENCE, "rows": result}


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
