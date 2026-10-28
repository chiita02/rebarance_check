# -*- coding: utf-8 -*-
"""
fetch_data.py  (v5 / 2026-10-03)
GitHub Actions で実行し docs/data.json を生成。HTML は同一オリジンでこれを読む。

【v4 の仮説は診断で否定された】
  v4 は「Yahoo が直近日の adjclose を null で返す」と考えたが、--diag の結果
  Adj Close は全行埋まっており、auto_adjust=True でも最新日が取れていた。
  → パースの問題ではない。取得できる「時刻」または「場所」の問題。

【v5 が対処する残り2つの仮説】
  A. 遅延説 : Yahoo の ETF 日次バーの確定が引けの数時間後。指数(^SOX)は先に確定。
              → 実行回数を増やし、遅い回で取り直せば直る。
  B. 経路説 : Actions のデータセンターIPに Yahoo が古いキャッシュを返す。
              → 時刻をずらしても直らない。Yahoo 以外の経路(Stooq)が必要。

【v5 の変更】
  (1) 後退禁止（最重要）: 既存 docs/data.json より古い日付では上書きしない。
      銘柄ごとに比較し、古ければ前回レコードをそのまま維持して carried を立てる。
      これにより「遅い回の実行は改善しかしない」= 何度走らせても悪化しない。
  (2) 取得経路(trail)を常にログと JSON に残す。Actions のログとローカルの --diag を
      突き合わせれば A か B かが判別できる。
  (3) Stooq を強化: 記号を URL エンコードし、失敗理由（HTML/上限超過/空）を区別。
  (4) --diag に Stooq の応答先頭を表示（none の原因を見るため）。

  v4 から引き継ぎ: auto_adjust=False + 末尾欠損のみ生 Close で水準合わせ補完、
  3y -> 1mo -> 5d -> Ticker.history -> Stooq の多段フォールバック（日付はずらさない）。
"""
import json, os, sys, io, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
import pandas as pd
import yfinance as yf

MA = 200
DAYS_OUT = 504
RETRY_PERIODS = ("1mo", "5d")
SATS = [
    {"tk": "TQQQ", "und": "QQQ",  "yf": "QQQ",  "stooq": "qqq.us"},
    {"tk": "SOXL", "und": "^SOX", "yf": "^SOX", "stooq": "^sox"},
    {"tk": "FAS",  "und": "XLF",  "yf": "XLF",  "stooq": "xlf.us"},
    {"tk": "ERX",  "und": "XLE",  "yf": "XLE",  "stooq": "xle.us"},
]
OUT = "docs/data.json"
STOOQ_URL = "https://stooq.com/q/d/l/?s={}&i=d"


# ============================================================
# 営業日ユーティリティ
# ============================================================
def expected_last_session(now_utc):
    """実行時点で終値が確定しているはずの直近営業日（祝日は考慮しない）。"""
    d = now_utc.date()
    if now_utc.hour < 21 or (now_utc.hour == 21 and now_utc.minute < 30):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def weekdays_between(a, b):
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ============================================================
# 列取り出し（単一銘柄でも MultiIndex で返ることがある）
# ============================================================
def _col(df, name):
    if df is None or len(df) == 0:
        return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if name not in set(df.columns.get_level_values(0)):
                return None
            s = df[name]
        else:
            if name not in df.columns:
                return None
            s = df[name]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        return s
    except Exception:
        return None


def _close(df):
    """Adj Close を主系列とし、【末尾の欠損だけ】生 Close で補完する。
    補完分は接続点でスケールを合わせるため、水準の段差は発生しない。
    Adj Close が無い場合（Ticker.history 等）は Close をそのまま使う。"""
    adj = _col(df, "Adj Close")
    raw = _col(df, "Close")
    if adj is None and raw is None:
        return None
    if adj is None:
        s = raw.dropna()
        return s if len(s) else None
    if raw is None:
        s = adj.dropna()
        return s if len(s) else None
    lv = adj.last_valid_index()
    if lv is None:
        s = raw.dropna()
        return s if len(s) else None
    out = adj.loc[:lv].dropna()
    trail = raw.index[(raw.index > lv) & raw.notna().values]
    if len(trail):
        a, r = float(adj.loc[lv]), float(raw.loc[lv])
        if r > 0 and not pd.isna(a):
            out = pd.concat([out, raw.loc[trail] * (a / r)])   # 末尾を水準合わせして継ぎ足し
    return out if len(out) else None


def _splice(base, extra):
    """extra を base に継ぎ足す。重複日の最終日で水準を合わせてから結合。
    既存日の値は base のまま = 日付のズレは起こらない。"""
    common = base.index.intersection(extra.index)
    if len(common):
        b, e = float(base.loc[common[-1]]), float(extra.loc[common[-1]])
        if e > 0:
            extra = extra * (b / e)
    out = pd.concat([base, extra])
    return out[~out.index.duplicated(keep="last")].sort_index()


# ============================================================
# データソース
# ============================================================
def src_download(sym, period):
    return _close(yf.download(sym, period=period, interval="1d",
                              auto_adjust=False, progress=False))


def src_history(sym):
    return _close(yf.Ticker(sym).history(period="1mo", interval="1d",
                                         auto_adjust=False))


def stooq_raw(stooq_sym):
    """Stooq の生テキストを返す（診断用）。"""
    url = STOOQ_URL.format(urllib.parse.quote(stooq_sym, safe=""))
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "text/csv,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def src_stooq(stooq_sym):
    if not stooq_sym:
        return None
    txt = stooq_raw(stooq_sym)
    head = txt.lstrip()[:200].lower()
    if head.startswith("<") or "<html" in head:
        return None                      # HTML が返った（記号不正 or ブロック）
    if "exceeded" in head or "limit" in head:
        return None                      # 日次アクセス上限
    first = txt.split("\n", 1)[0]
    if "Date" not in first:
        return None
    df = pd.read_csv(io.StringIO(txt), parse_dates=["Date"]).set_index("Date")
    if "Close" not in df.columns:
        return None
    s = df["Close"].dropna()
    return s if len(s) else None


def fetch(cfg, want_date=None, diag=False):
    """primary -> 短レンジ -> Ticker.history -> Stooq の順に、足りるまで継ぎ足す。"""
    sym = cfg["yf"]
    trail = []
    s = src_download(sym, "3y")
    if s is None or len(s) <= MA + 5:
        return None, {"source": None, "trail": ["3y:NG"]}
    trail.append(f"3y:{s.index[-1].date()}")
    source = "yahoo-3y"
    if want_date is None or s.index[-1].date() >= want_date:
        return s, {"source": source, "trail": trail}

    attempts = ([("yahoo-" + p, lambda p=p: src_download(sym, p)) for p in RETRY_PERIODS]
                + [("yahoo-history", lambda: src_history(sym)),
                   ("stooq", lambda: src_stooq(cfg.get("stooq")))])
    for name, fn in attempts:
        try:
            s2 = fn()
        except Exception as e:
            trail.append(f"{name}:ERR")
            if diag:
                print(f"      {name}: 例外 {e}")
            continue
        if s2 is None or not len(s2):
            trail.append(f"{name}:none")
            continue
        trail.append(f"{name}:{s2.index[-1].date()}")
        if s2.index[-1].date() <= s.index[-1].date():
            continue                      # 新しくなければ使わない
        s = _splice(s, s2)
        source = name
        if s.index[-1].date() >= want_date:
            break
    return s, {"source": source, "trail": trail}


# ============================================================
# 前回の data.json（後退禁止のため）
# ============================================================
def load_prev():
    try:
        with open(OUT) as f:
            return json.load(f)
    except Exception:
        return None


def prev_last_date(prev, tk):
    try:
        d = prev["series"][tk]["dates"][-1]
        return datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return None


# ============================================================
# 診断モード
# ============================================================
def diagnose():
    now = datetime.now(timezone.utc)
    exp = expected_last_session(now)
    print(f"now  : {now:%Y-%m-%d %H:%M} UTC  (JST {now+timedelta(hours=9):%m/%d %H:%M})")
    print(f"期待 : {exp} ({exp.strftime('%a')})\n")
    cols = ["3y(adj+raw)", "3y(auto_adj)", "1mo", "5d", "history", "stooq"]
    print("各ソースが返す最終日（原因切り分け用）")
    print(f"  {'ticker':<7}" + "".join(f"{c:>14}" for c in cols))
    print("  " + "-" * (7 + 14 * len(cols)))
    for cfg in SATS:
        sym = cfg["yf"]
        fns = [lambda: src_download(sym, "3y"),
               lambda: _close(yf.download(sym, period="3y", interval="1d",
                                          auto_adjust=True, progress=False)),
               lambda: src_download(sym, "1mo"),
               lambda: src_download(sym, "5d"),
               lambda: src_history(sym),
               lambda: src_stooq(cfg.get("stooq"))]
        row = []
        for fn in fns:
            try:
                s = fn()
                row.append(str(s.index[-1].date()) if s is not None and len(s) else "none")
            except Exception:
                row.append("ERR")
        print(f"  {cfg['tk']:<7}" + "".join(f"{v:>14}" for v in row))
    print("\n  読み方:")
    print("   ・Yahoo 系が古く stooq だけ新しい -> Yahoo 側の遅れ。stooq が救う")
    print("   ・全部古い -> まだ公開されていない。実行時刻を遅らせる")
    print("   ・ここでは全部新しいのに Actions の data.json が古い -> 経路説(B)。")
    print("     Actions のログの trail と、この表を同時刻で比べること")

    print("\nStooq の応答先頭（none の原因確認用）")
    for cfg in SATS:
        try:
            txt = stooq_raw(cfg["stooq"]).replace("\n", " / ")[:110]
        except Exception as e:
            txt = f"例外 {e}"
        print(f"  {cfg['tk']:<6} {cfg['stooq']:<8} {txt}")

    print("\n直近5行の生データ（QQQ, auto_adjust=False）")
    try:
        df = yf.download("QQQ", period="1mo", interval="1d",
                         auto_adjust=False, progress=False)
        cc = [c for c in ("Close", "Adj Close") if _col(df, c) is not None]
        print(pd.DataFrame({c: _col(df, c) for c in cc}).tail(5).to_string())
    except Exception as e:
        print(f"  取得失敗: {e}")


# ============================================================
# メイン
# ============================================================
def main():
    now = datetime.now(timezone.utc)
    exp = expected_last_session(now)
    prev = load_prev()
    print(f"run  : {now:%Y-%m-%d %H:%M} UTC  (JST {now+timedelta(hours=9):%m/%d %H:%M})")
    print(f"期待 : 直近確定セッション = {exp} ({exp.strftime('%a')})")
    print(f"前回 : {prev['generated_at'][:16] if prev else 'なし'}\n")

    payload = {"generated_at": now.isoformat(), "ma_window": MA,
               "expected_session": exp.isoformat(),
               "series": {}, "source": {}, "trail": {}, "carried": []}
    errors, stale, rescued = [], [], []

    for cfg in SATS:
        tk = cfg["tk"]
        pld = prev_last_date(prev, tk)
        try:
            s, info = fetch(cfg, want_date=exp)
        except Exception as e:
            s, info = None, {"source": None, "trail": [f"EXC:{e}"]}
            errors.append(f"{tk}: {e}")

        # ---- 後退禁止: 今回が前回より古ければ前回レコードを維持 ----
        new_last = s.index[-1].date() if s is not None and len(s) else None
        if pld is not None and (new_last is None or new_last < pld):
            payload["series"][tk] = prev["series"][tk]
            payload["source"][tk] = (prev.get("source") or {}).get(tk)
            payload["trail"][tk] = info["trail"]
            payload["carried"].append(tk)
            got = new_last if new_last else "取得失敗"
            print(f"{tk:<5} KEEP(前回{pld}を維持 / 今回は{got})")
            print(f'      trail: {" -> ".join(info["trail"])}')
            if weekdays_between(pld, exp) > 0:
                stale.append(f"{tk}:{pld}")
            continue

        if s is None:
            errors.append(f"{tk}: no data")
            print(f'{tk:<5} NO DATA  trail: {" -> ".join(info["trail"])}')
            continue

        sma = s.rolling(MA).mean()
        sub, sub_ma = s.iloc[-DAYS_OUT:], sma.iloc[-DAYS_OUT:]
        payload["series"][tk] = {
            "underlying": cfg["und"],
            "dates":  [d.strftime("%Y-%m-%d") for d in sub.index],
            "close":  [round(float(v), 4) for v in sub.values],
            "sma200": [None if pd.isna(v) else round(float(v), 4) for v in sub_ma.values],
        }
        payload["source"][tk] = info["source"]
        payload["trail"][tk] = info["trail"]

        behind = weekdays_between(new_last, exp)
        if behind > 0:
            stale.append(f"{tk}:{new_last}")
        if info["source"] != "yahoo-3y":
            rescued.append(f'{tk}<-{info["source"]}')
        mark = "OK " if behind <= 0 else f"OLD({behind}営業日遅れ)"
        tail = " | ".join(f"{d.date()} {float(v):.2f}"
                          for d, v in zip(sub.index[-3:], sub.values[-3:]))
        print(f"{tk:<5} {mark:<16} last3: {tail}")
        print(f'      trail: {" -> ".join(info["trail"])}')

    if not payload["series"]:
        print("FATAL: 全銘柄取得失敗", errors)
        sys.exit(1)

    payload["errors"], payload["stale"] = errors, stale

    # ---- 改善が無ければ書かない（1日に何度も走らせてもコミットが増えない）----
    if prev is not None and "--force" not in sys.argv:
        def fingerprint(p):
            return {k: (v["dates"][-1], v["close"][-1]) for k, v in p["series"].items()}
        if fingerprint(payload) == fingerprint(prev):
            print("\n= 前回から変化なし -> 書き込みスキップ（コミットも発生しない）")
            if stale:
                print(f"⚠ まだ遅れあり: {stale}  -> あとの回の実行で取り直す")
            return

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"\n-> {OUT} written ({len(payload['series'])} tickers)"
          + (f" errors={errors}" if errors else ""))
    if payload["carried"]:
        print(f"= 後退禁止により前回値を維持: {payload['carried']}")
    if rescued:
        print(f"✓ フォールバックで復旧: {rescued}")
    if stale:
        print(f"⚠ まだ遅れあり: {stale}  -> あとの回の実行で自動的に取り直される")
    else:
        print("✓ 全銘柄が最新セッションまで取得済み")


if __name__ == "__main__":
    if "--diag" in sys.argv:
        diagnose()
    else:
        main()
