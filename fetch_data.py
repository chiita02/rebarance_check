# -*- coding: utf-8 -*-
"""
fetch_data.py  (GitHub Actions で毎営業日実行)
サテライト原資産の日次終値を取得し、docs/data.json を生成する。
HTMLは同一オリジンでこのJSONを読むだけ -> CORS/プロキシ不要。

判定ロジックは確定版と同一(原資産の200日SMA、±0.5%バンド)。
JSONには生データ(直近2年の日付・終値・SMA)のみ入れ、判定はHTML側で行う。
"""
import json, sys
from datetime import datetime, timezone, timedelta
import pandas as pd
import yfinance as yf

MA = 200
DAYS_OUT = 504          # 2年分をHTMLに渡す
SATS = [
    {"tk": "TQQQ", "und": "QQQ",  "yf": "QQQ"},
    {"tk": "SOXL", "und": "^SOX", "yf": "^SOX"},
    {"tk": "FAS",  "und": "XLF",  "yf": "XLF"},
    {"tk": "ERX",  "und": "XLE",  "yf": "XLE"},
]
OUT = "docs/data.json"

def fetch(sym):
    df = yf.download(sym, period="3y", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or len(df) == 0:
        return None
    s = df["Close"]
    if isinstance(s, pd.DataFrame):      # 単一銘柄でも列名付きで返る場合がある
        s = s.iloc[:, 0]
    s = s.dropna()
    return s if len(s) > MA + 5 else None

def main():
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "ma_window": MA, "series": {}}
    errors = []
    for cfg in SATS:
        try:
            s = fetch(cfg["yf"])
            if s is None:
                errors.append(f'{cfg["tk"]}: no data')
                continue
            sma = s.rolling(MA).mean()
            sub = s.iloc[-DAYS_OUT:]
            sub_ma = sma.iloc[-DAYS_OUT:]
            payload["series"][cfg["tk"]] = {
                "underlying": cfg["und"],
                "dates":  [d.strftime("%Y-%m-%d") for d in sub.index],
                "close":  [round(float(v), 4) for v in sub.values],
                "sma200": [None if pd.isna(v) else round(float(v), 4) for v in sub_ma.values],
            }
            print(f'{cfg["tk"]:<5} OK  last={sub.index[-1].date()} '
                  f'close={float(sub.iloc[-1]):.2f} sma={float(sub_ma.iloc[-1]):.2f}')
        except Exception as e:
            errors.append(f'{cfg["tk"]}: {e}')
    if not payload["series"]:
        print("FATAL: 全銘柄取得失敗", errors); sys.exit(1)
    payload["errors"] = errors
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"-> {OUT} written ({len(payload['series'])} tickers)"
          + (f" errors={errors}" if errors else ""))

if __name__ == "__main__":
    main()
