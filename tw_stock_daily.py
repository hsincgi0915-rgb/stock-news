#!/usr/bin/env python3
"""
台股盤後日報：每天抓取當日盤後資訊 + 可能影響隔天走勢的重要新聞，輸出成 Markdown。

資料來源
  - 台灣證券交易所 OpenAPI / 官網 JSON（大盤、個股、三大法人、外資買賣超）
  - Google News RSS（財經新聞，依關鍵字與時間排序）
  - yfinance（選用：美股四大指數、費半、VIX、美元/台幣等）
  - Anthropic API（選用：產生「隔天可能走勢」摘要，需 ANTHROPIC_API_KEY）

用法
  pip install requests yfinance anthropic      # yfinance、anthropic 可省略
  python tw_stock_daily.py                      # 產生今天的報告（非交易日會自動略過）
  python tw_stock_daily.py --force              # 強制以「最近一個交易日」資料重產（例如隔天早上更新新聞）
  python tw_stock_daily.py --no-ai --no-intl    # 不用 AI 摘要、不抓國際市場

選用環境變數
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL    AI 摘要
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  報告完成後推播到 Telegram
  TWSE_VERIFY_SSL=0                      若證交所 OpenAPI 出現 SSL 憑證錯誤，才建議暫時關閉驗證

排程（建議 17:30 以後，等三大法人資料出爐）
  Linux/macOS cron：30 17 * * 1-5  cd /path/to && python3 tw_stock_daily.py
  Windows：用「工作排程器」每天 17:30 執行 python tw_stock_daily.py

注意：僅供資訊整理，不構成投資建議。
"""
import argparse
import datetime as dt
import html
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Taipei")
UA = {"User-Agent": "Mozilla/5.0 (tw-stock-daily-report)"}
OPENAPI = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"
VERIFY_SSL = os.getenv("TWSE_VERIFY_SSL", "1") != "0"

NEWS_QUERIES = [
    "台股 盤後",
    "台股 明天 展望",
    "美股 收盤 科技股",
    "聯準會 利率 通膨",
    "台積電 外資",
    "新台幣 匯率 外資",
    "關稅 半導體 出口管制",
]
# 標題含有這些關鍵字的新聞，視為較可能影響隔天盤勢（權重可自行調整）
NEWS_KEYWORDS = {
    "聯準會": 3, "Fed": 3, "降息": 3, "升息": 3, "CPI": 3, "非農": 3, "通膨": 2, "利率": 2,
    "台積電": 3, "輝達": 3, "NVIDIA": 3, "AI": 1, "半導體": 2, "費半": 2, "蘋果": 1,
    "關稅": 3, "川普": 2, "出口管制": 3, "制裁": 2, "地緣": 2, "戰爭": 2,
    "外資": 2, "法人": 1, "期貨": 2, "夜盤": 2, "匯率": 2, "新台幣": 2, "美元": 1,
    "財報": 2, "法說": 2, "營收": 1, "除息": 1, "融資": 1, "崩": 2, "暴跌": 2, "大漲": 2,
}
INTL_SYMBOLS = [
    ("S&P 500", "^GSPC"), ("那斯達克", "^IXIC"), ("道瓊", "^DJI"), ("費城半導體", "^SOX"),
    ("VIX 恐慌指數", "^VIX"), ("美元/台幣", "TWD=X"), ("美國10年期殖利率", "^TNX"),
    ("WTI 原油", "CL=F"), ("黃金", "GC=F"),
]


# ----------------------------------------------------------------- 小工具
def warn(msg):
    print(f"[警告] {msg}", file=sys.stderr)


def get(url, params=None, retries=3, as_json=True):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20, verify=VERIFY_SSL)
            r.raise_for_status()
            return r.json() if as_json else r
        except requests.exceptions.SSLError as e:
            raise RuntimeError(
                f"SSL 憑證錯誤（{url}）。若確定要略過，可設定環境變數 TWSE_VERIFY_SSL=0。") from e
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"請求失敗 {url}: {last}")


def safe(label, fn, *a, **kw):
    """單一資料來源失敗時不讓整份報告掛掉。"""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        warn(f"{label} 取得失敗：{e}")
        return None


def num(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def pick(row, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def parse_date(s):
    """支援 1151116 / 115/11/16 / 20261116 / 2026-11-16。"""
    d = re.sub(r"\D", "", str(s))
    if len(d) == 7:
        return dt.date(int(d[:3]) + 1911, int(d[3:5]), int(d[5:7]))
    if len(d) == 8:
        return dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
    raise ValueError(f"無法解析日期：{s}")


def pct_str(p):
    if p is None:
        return "-"
    return f"{'▲' if p > 0 else '▼' if p < 0 else '－'}{abs(p):.2f}%"


def yi(x):  # 元 → 億元
    return x / 1e8


# ----------------------------------------------------------------- 台股資料
def fetch_market_summary():
    rows = get(f"{OPENAPI}/exchangeReport/FMTQIK")
    last, prev = rows[-1], rows[-2]
    close = num(pick(last, "TAIEX", "發行量加權股價指數"))
    chg = num(pick(last, "Change", "漲跌點數"))
    value = num(pick(last, "TradeValue", "成交金額"))
    prev_value = num(pick(prev, "TradeValue", "成交金額"))
    return {
        "date": parse_date(pick(last, "Date", "日期")),
        "close": close,
        "change": chg,
        "pct": chg / (close - chg) * 100 if close and chg is not None else None,
        "value": value,
        "volume": num(pick(last, "TradeVolume", "成交股數")),
        "trades": num(pick(last, "Transaction", "成交筆數")),
        "value_vs_prev": (value / prev_value - 1) * 100 if value and prev_value else None,
    }


def fetch_stocks():
    rows = get(f"{OPENAPI}/exchangeReport/STOCK_DAY_ALL")
    out = []
    for r in rows:
        code = str(pick(r, "Code", "證券代號") or "")
        if not re.fullmatch(r"\d{4}", code) or code.startswith("00"):  # 只留一般個股，排除 ETF/權證
            continue
        close = num(pick(r, "ClosingPrice", "收盤價"))
        chg = num(pick(r, "Change", "漲跌價差"))
        if close is None or chg is None or close - chg <= 0:
            continue
        out.append({
            "code": code,
            "name": pick(r, "Name", "證券名稱"),
            "close": close,
            "chg": chg,
            "pct": chg / (close - chg) * 100,
            "vol": num(pick(r, "TradeVolume", "成交股數")) or 0,
            "value": num(pick(r, "TradeValue", "成交金額")) or 0,
        })
    return out


def fetch_institutional(date):
    j = get(f"{RWD}/fund/BFI82U", {"type": "day", "dayDate": date.strftime("%Y%m%d"), "response": "json"})
    if j.get("stat") != "OK":
        raise RuntimeError(f"三大法人資料尚未公布（{j.get('stat')}）")
    return [(r[0], num(r[1]), num(r[2]), num(r[3])) for r in j["data"]]


def fetch_foreign_top(date, n=10):
    j = get(f"{RWD}/fund/T86", {"date": date.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"})
    if j.get("stat") != "OK":
        raise RuntimeError(f"外資買賣超資料尚未公布（{j.get('stat')}）")
    f = j["fields"]
    i_code = f.index("證券代號")
    i_name = f.index("證券名稱")
    i_for = next(i for i, x in enumerate(f) if "外" in x and "買賣超" in x)
    i_trust = next((i for i, x in enumerate(f) if x.startswith("投信買賣超")), None)
    rows = []
    for r in j["data"]:
        code = r[i_code].strip()
        if not re.fullmatch(r"\d{4}", code) or code.startswith("00"):
            continue
        rows.append({
            "code": code, "name": r[i_name].strip(),
            "foreign": (num(r[i_for]) or 0) / 1000,                     # 張
            "trust": (num(r[i_trust]) or 0) / 1000 if i_trust is not None else None,
        })
    rows.sort(key=lambda x: x["foreign"])
    return {"sell": rows[:n], "buy": rows[::-1][:n]}


# ----------------------------------------------------------------- 國際市場（選用）
def fetch_intl():
    import yfinance as yf  # 沒安裝就會丟 ImportError，由 safe() 處理
    out = []
    for name, sym in INTL_SYMBOLS:
        try:
            h = yf.Ticker(sym).history(period="7d")["Close"].dropna()
            if len(h) >= 2:
                out.append((name, h.index[-1].date(), float(h.iloc[-1]), float(h.iloc[-1] / h.iloc[-2] * 100 - 100)))
        except Exception as e:  # noqa: BLE001
            warn(f"{name} 取得失敗：{e}")
    return out


# ----------------------------------------------------------------- 新聞
def fetch_news(hours=36, limit=15):
    now = dt.datetime.now(dt.timezone.utc)
    seen, items = set(), []
    for q in NEWS_QUERIES:
        try:
            r = get("https://news.google.com/rss/search",
                    {"q": f"{q} when:2d", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}, as_json=False)
            root = ET.fromstring(r.content)
        except Exception as e:  # noqa: BLE001
            warn(f"新聞查詢「{q}」失敗：{e}")
            continue
        for it in root.iter("item"):
            title = html.unescape(it.findtext("title", "")).strip()
            link = it.findtext("link", "")
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate", ""))
            except Exception:  # noqa: BLE001
                continue
            age_h = (now - pub).total_seconds() / 3600
            key = re.sub(r"[\W_]+", "", title)[:18]
            if not title or key in seen or age_h > hours:
                continue
            seen.add(key)
            score = sum(w for k, w in NEWS_KEYWORDS.items() if k.lower() in title.lower())
            score += max(0.0, 3 - age_h / 12)  # 越新越加分
            items.append({"title": title, "link": link, "pub": pub.astimezone(TZ),
                          "source": it.findtext("source", ""), "score": score})
    items.sort(key=lambda x: (-x["score"], -x["pub"].timestamp()))
    return items[:limit]


# ----------------------------------------------------------------- AI 摘要（選用）
def ai_outlook(context):
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        max_tokens=1500,
        system=("你是台股盤後分析助理。只能根據使用者提供的資料與新聞標題回答，不得編造數字或消息；"
                "資訊不足時要明說。使用繁體中文，不提供買賣建議。"),
        messages=[{"role": "user", "content": (
            "以下是今天台股盤後資料與近期新聞標題，請用條列式寫出：\n"
            "1. 今日盤勢重點（3 點內）\n2. 隔天可能影響走勢的因素（分『偏多』『偏空』『待觀察事件』）\n"
            "3. 明日需留意的風險\n每點簡短，並標註依據來自哪則資料。\n\n" + context)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ----------------------------------------------------------------- 報告
def build_report(trade_date, mkt, stocks, inst, foreign, intl, news, ai_text):
    L = []
    L.append(f"# 台股盤後日報 {trade_date:%Y-%m-%d}（週{'一二三四五六日'[trade_date.weekday()]}）")
    L.append(f"_產生時間：{dt.datetime.now(TZ):%Y-%m-%d %H:%M}（台北時間）_\n")

    L.append("## 一、大盤概況")
    if mkt:
        L.append(f"- 加權指數：**{mkt['close']:,.2f}**（{mkt['change']:+,.2f} 點，{pct_str(mkt['pct'])}）")
        L.append(f"- 成交金額：{yi(mkt['value']):,.0f} 億元"
                 + (f"（較前一日 {mkt['value_vs_prev']:+.1f}%）" if mkt['value_vs_prev'] is not None else ""))
        L.append(f"- 成交量：{mkt['volume'] / 1e8:,.2f} 億股；成交筆數：{mkt['trades'] / 1e4:,.0f} 萬筆")
    else:
        L.append("- （大盤資料取得失敗）")

    L.append("\n## 二、三大法人買賣超（億元）")
    if inst:
        L.append("| 法人 | 買進 | 賣出 | 買賣超 |\n|---|---:|---:|---:|")
        for name, b, s, d in inst:
            L.append(f"| {name} | {yi(b):,.1f} | {yi(s):,.1f} | **{yi(d):+,.1f}** |")
    else:
        L.append("- （尚未公布或取得失敗）")

    if foreign:
        L.append("\n## 三、外資買賣超個股（張）")
        L.append("| 外資買超前10 | 買超 | 外資賣超前10 | 賣超 |\n|---|---:|---|---:|")
        for b, s in zip(foreign["buy"], foreign["sell"]):
            L.append(f"| {b['code']} {b['name']} | {b['foreign']:+,.0f} | {s['code']} {s['name']} | {s['foreign']:+,.0f} |")

    if stocks:
        L.append("\n## 四、個股排行（上市一般個股，不含 ETF）")
        top_val = sorted(stocks, key=lambda x: -x["value"])[:10]
        liquid = [s for s in stocks if s["vol"] >= 1_000_000]  # 成交量≥1000張
        up = sorted(liquid, key=lambda x: -x["pct"])[:10]
        down = sorted(liquid, key=lambda x: x["pct"])[:10]
        up_n = sum(1 for s in stocks if s["chg"] > 0)
        down_n = sum(1 for s in stocks if s["chg"] < 0)
        L.append(f"- 上漲 {up_n} 檔／下跌 {down_n} 檔／平盤 {len(stocks) - up_n - down_n} 檔")
        L.append("\n**成交值前10**\n\n| 股票 | 收盤 | 漲跌幅 | 成交值(億) |\n|---|---:|---:|---:|")
        for s in top_val:
            L.append(f"| {s['code']} {s['name']} | {s['close']:,.2f} | {pct_str(s['pct'])} | {yi(s['value']):,.1f} |")
        L.append("\n**漲幅前10 / 跌幅前10（成交量≥1000張）**\n\n| 漲幅榜 | 漲跌幅 | 跌幅榜 | 漲跌幅 |\n|---|---:|---|---:|")
        for u, d in zip(up, down):
            L.append(f"| {u['code']} {u['name']} {u['close']:,.2f} | {pct_str(u['pct'])} | "
                     f"{d['code']} {d['name']} {d['close']:,.2f} | {pct_str(d['pct'])} |")

    L.append("\n## 五、國際市場參考（各自最近一個交易日收盤）")
    if intl:
        L.append("| 項目 | 日期 | 收盤 | 漲跌幅 |\n|---|---|---:|---:|")
        for name, d, c, p in intl:
            L.append(f"| {name} | {d:%m/%d} | {c:,.2f} | {pct_str(p)} |")
    else:
        L.append("- （未啟用或取得失敗；需安裝 yfinance）")

    L.append("\n## 六、可能影響隔天走勢的重要新聞")
    if news:
        for n in news:
            L.append(f"- [{n['title']}]({n['link']}) — {n['source']} {n['pub']:%m/%d %H:%M}")
    else:
        L.append("- （新聞取得失敗）")

    if ai_text:
        L.append("\n## 七、隔天走勢重點（AI 依上列資料整理）\n")
        L.append(ai_text)
    L.append("\n---\n_資料來源：臺灣證券交易所、Google News、Yahoo Finance。僅供資訊整理，不構成投資建議。_")
    return "\n".join(L)


def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    for i in range(0, len(text), 3800):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text[i:i + 3800], "disable_web_page_preview": True},
                          timeout=20)
        if not r.ok:
            warn(f"Telegram 推播失敗：{r.text[:200]}")
            return
    print("已推播到 Telegram")


def main():
    ap = argparse.ArgumentParser(description="台股盤後日報")
    ap.add_argument("--force", action="store_true", help="資料日期不是今天也照樣產生（使用最近一個交易日）")
    ap.add_argument("--no-ai", action="store_true", help="不產生 AI 摘要")
    ap.add_argument("--no-intl", action="store_true", help="不抓國際市場")
    ap.add_argument("--out", default="reports", help="輸出資料夾（預設 reports）")
    args = ap.parse_args()

    today = dt.datetime.now(TZ).date()
    mkt = safe("大盤", fetch_market_summary)
    if not mkt:
        print("無法取得大盤資料，結束。", file=sys.stderr)
        return 1
    trade_date = mkt["date"]
    if trade_date != today and not args.force:
        print(f"最新資料日期為 {trade_date}，不是今天（{today}）：可能休市或資料尚未發布。"
              "若要用最近交易日資料產生報告請加 --force。")
        return 0

    stocks = safe("個股行情", fetch_stocks)
    inst = safe("三大法人", fetch_institutional, trade_date)
    foreign = safe("外資買賣超", fetch_foreign_top, trade_date)
    intl = None if args.no_intl else safe("國際市場", fetch_intl)
    news = safe("新聞", fetch_news) or []

    ai_text = None
    if not args.no_ai:
        ctx = build_report(trade_date, mkt, stocks, inst, foreign, intl, news, None)
        ai_text = safe("AI 摘要", ai_outlook, ctx)

    report = build_report(trade_date, mkt, stocks, inst, foreign, intl, news, ai_text)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{trade_date:%Y-%m-%d}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n已儲存：{path}")
    send_telegram(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
