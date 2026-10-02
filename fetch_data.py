# -*- coding: utf-8 -*-
"""
fetch_data.py  (v3 / 2026-10-02)
GitHub Actions で毎営業日実行。サテライト原資産の日次終値を取得し docs/data.json を生成。
HTML は同一オリジンでこの JSON を読むだけ -> CORS/プロキシ不要。

判定ロジックは確定版と同一（原資産の200日SMA、±0.5%バンド）。
JSON には生データ（直近2年の日付・終値・SMA）のみ入れ、判定は HTML 側で行う。

【v3 の変更 — 銘柄ごとの取得遅れ対策】
  症状: 同じ実行なのに SOXL(^SOX) だけ最新営業日、TQQQ/FAS/ERX は1営業日古い。
  原因: Yahoo は長レンジ(period="3y")の応答と短レンジの応答で最新バーの反映が
        食い違うことがある（銘柄・リクエスト時刻により差が出る）。
  対策: 期待する直近営業日に届いていない銘柄だけ、短レンジ("1mo"→"5d")で再取得し、
        【重複日の水準を合わせてから】継ぎ足す。
        ※継ぎ足しは必ず「同じ日付に同じ値」を保つスケール接続で行う。
          合成レバ系列で起きた1日ズレ・バグと同じ失敗を繰り返さないため。
  さらに: 再取得後もまだ古い銘柄は JSON の "stale" に残し、HTML 側で警告表示する。

【注意】cron の時刻について
  米国の引けは夏時間 20:00 UTC / 冬時間 21:00 UTC。
  cron を 21:30 UTC にすると冬時間は引けの30分後になり、取得遅れを踏みやすい。
  22:00 UTC 以降（= JST 07:00 以降）を推奨。
"""
import json, sys
from datetime import datetime, timezone, timedelta
import pandas as pd
import yfinance as yf

MA = 200
DAYS_OUT = 504          # 2年分をHTMLに渡す
RETRY_PERIODS = ("1mo", "5d")   # 遅れている銘柄に対する再取得レンジ
SATS = [
    {"tk": "TQQQ", "und": "QQQ",  "yf": "QQQ"},
    {"tk": "SOXL", "und": "^SOX", "yf": "^SOX"},
    {"tk": "FAS",  "und": "XLF",  "yf": "XLF"},
    {"tk": "ERX",  "und": "XLE",  "yf": "XLE"},
]
OUT = "docs/data.json"


def expected_last_session(now_utc):
    """実行時点で「終値が確定しているはず」の直近営業日。
    米国クローズ = 20:00 UTC(夏) / 21:00 UTC(冬)。余裕をみて 21:30 UTC を境界とする。
    ※祝日は考慮しないので、祝日明けは1日ずれた警告が出ることがある(無害)。"""
    d = now_utc.date()
    if now_utc.hour < 21 or (now_utc.hour == 21 and now_utc.minute < 30):
        d -= timedelta(days=1)          # 当日ぶんはまだ確定していない
    while d.weekday() >= 5:             # 土(5)・日(6)は遡る
        d -= timedelta(days=1)
    return d


def weekdays_between(a, b):
    """a(古) から b(新) までの営業日数(祝日無視)"""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _close(df):
    """yfinance の戻りから Close 系列を取り出す（単一銘柄でも列名付きで返ることがある）"""
    if df is None or len(df) == 0:
        return None
    if "Close" not in df:
        return None
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    return s if len(s) else None


def _splice(base, extra):
    """extra を base に継ぎ足す。重複日があればその最終日で水準を合わせてから結合。
    既存日の値は base のまま保たれる = 日付のズレは起こらない。"""
    common = base.index.intersection(extra.index)
    if len(common):
        b = float(base.loc[common[-1]])
        e = float(extra.loc[common[-1]])
        if e > 0:
            extra = extra * (b / e)
    out = pd.concat([base, extra])
    return out[~out.index.duplicated(keep="last")].sort_index()


def fetch(sym, want_date=None):
    """長レンジで取得し、期待営業日に届いていなければ短レンジで再取得して継ぎ足す。"""
    df = yf.download(sym, period="3y", interval="1d",
                     auto_adjust=True, progress=False)
    s = _close(df)
    if s is None or len(s) <= MA + 5:
        return None, None
    first_last = s.index[-1].date()
    used_retry = None
    if want_date is not None and first_last < want_date:
        for p in RETRY_PERIODS:
            try:
                s2 = _close(yf.download(sym, period=p, interval="1d",
                                        auto_adjust=True, progress=False))
            except Exception:
                s2 = None
            if s2 is None or not len(s2):
                continue
            if s2.index[-1].date() <= s.index[-1].date():
                continue                      # 短レンジも古ければ使わない
            s = _splice(s, s2)
            used_retry = p
            if s.index[-1].date() >= want_date:
                break
    return s, {"first_last": first_last, "retry": used_retry}


def main():
    now = datetime.now(timezone.utc)
    exp = expected_last_session(now)
    print(f"run  : {now:%Y-%m-%d %H:%M} UTC  (JST {now+timedelta(hours=9):%m/%d %H:%M})")
    print(f"期待 : 直近確定セッション = {exp} ({exp.strftime('%a')})\n")
    payload = {"generated_at": now.isoformat(),
               "ma_window": MA, "expected_session": exp.isoformat(), "series": {}}
    errors, stale, repaired = [], [], []
    for cfg in SATS:
        try:
            s, info = fetch(cfg["yf"], want_date=exp)
            if s is None:
                errors.append(f'{cfg["tk"]}: no data')
                print(f'{cfg["tk"]:<5} NO DATA')
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
            last = sub.index[-1].date()
            behind = weekdays_between(last, exp)
            if behind > 0:
                stale.append(f'{cfg["tk"]}:{last}')
            if info["retry"] and last > info["first_last"]:
                repaired.append(f'{cfg["tk"]}:{info["first_last"]}->{last}({info["retry"]})')
            mark = "OK " if behind <= 0 else f"OLD({behind}営業日遅れ)"
            note = ""
            if info["retry"]:
                note = (f'  [retry {info["retry"]}: {info["first_last"]} -> {last}]'
                        if last > info["first_last"] else
                        f'  [retry {info["retry"]}: 改善せず]')
            tail = " | ".join(f"{d.date()} {float(v):.2f}"
                              for d, v in zip(sub.index[-3:], sub.values[-3:]))
            print(f'{cfg["tk"]:<5} {mark:<16} last3: {tail}{note}')
        except Exception as e:
            errors.append(f'{cfg["tk"]}: {e}')
            print(f'{cfg["tk"]:<5} ERROR {e}')
    if not payload["series"]:
        print("FATAL: 全銘柄取得失敗", errors)
        sys.exit(1)
    payload["errors"] = errors
    payload["stale"] = stale
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"\n-> {OUT} written ({len(payload['series'])} tickers)"
          + (f" errors={errors}" if errors else ""))
    if repaired:
        print(f"✓ 再取得で復旧: {repaired}")
    if stale:
        print(f"⚠ まだ遅れあり: {stale}")
        print("  祝日明けなら正常。そうでなければ Yahoo 側の更新遅延 -> 数時間後に再実行。")
    else:
        print("✓ 全銘柄が最新セッションまで取得済み")


if __name__ == "__main__":
    main()
