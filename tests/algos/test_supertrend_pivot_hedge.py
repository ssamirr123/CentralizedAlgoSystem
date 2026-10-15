"""Hedge leg: 1000 pts away normally, 500 on expiry day; bought first, sold last."""
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2] / "trading" / "algos" / "supertrendPivotAlgo"
sys.path.insert(0, str(ROOT))

import option_chain as oc_mod  # noqa: E402
import order_manager as om_mod  # noqa: E402
import config  # noqa: E402


def _chain(expiry_today):
    exp = datetime.now().strftime("%d%b%Y").upper() if expiry_today else "31DEC2099"
    ins = []
    for k in range(20000, 30000, 50):
        for t in ("CE", "PE"):
            ins.append({"exch_seg": "NFO", "name": "NIFTY", "instrumenttype": "OPTIDX",
                        "symbol": f"NIFTY{exp}{k}{t}", "token": f"{k}{t}",
                        "expiry": exp, "strike": str(k * 100), "lotsize": "75"})
    oc = oc_mod.OptionChain()
    oc._instruments = ins
    return oc


def test_hedge_distance_normal_day():
    oc = _chain(False)
    pe = oc.get_atm_contract(25000, "PE")
    ce = oc.get_atm_contract(25000, "CE")
    assert oc.get_hedge_contract(pe)["strike"] == 24000
    assert oc.get_hedge_contract(ce)["strike"] == 26000


def test_hedge_distance_expiry_day():
    oc = _chain(True)
    pe = oc.get_atm_contract(25000, "PE")
    ce = oc.get_atm_contract(25000, "CE")
    assert oc.get_hedge_contract(pe)["strike"] == 24500
    assert oc.get_hedge_contract(ce)["strike"] == 25500


def test_order_sequence_and_pnl(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    om = om_mod.OrderManager(smart=None)
    prices = {"S": [100.0, 110.0], "H": [10.0, 4.0]}  # entry, exit
    calls = []

    def fake_place(contract, side):
        key = contract["symbol"]
        calls.append((side, key))
        return {"order_id": "X", "price": prices[key].pop(0), "qty": 75}

    monkeypatch.setattr(om, "_place_order", fake_place)
    short = {"symbol": "S", "token": "1", "strike": 25000, "option_type": "PE"}
    hedge = {"symbol": "H", "token": "2", "strike": 24000, "option_type": "PE"}
    om.sell_option(short, hedge)
    om.exit_position()
    assert calls == [("BUY", "H"), ("SELL", "S"), ("BUY", "S"), ("SELL", "H")]
    # short: (100-110)*75 = -750 ; hedge: (4-10)*75 = -450
    assert om.realized_pnl == -1200.0
