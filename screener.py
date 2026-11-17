#!/usr/bin/env python3
"""
台股選股篩選器：每天盤後產生一個可以在網頁上調整條件的篩選頁（docs/screener/index.html）。

預設條件（網頁上都可以再改）
  1. 殖利率 ≥ 5%
  2. 外資連續買超 ≥ 3 天
  3. 投信連續買超 ≥ 3 天
  4. 5、10、20 日均線皆向上（今日均線 > 昨日均線）
  5. KD 糾結後出現起漲訊號：
       - 近 3 日內 K 值由下往上穿越 D 值（黃金交叉）
       - 交叉前 5 日 K、D 平均差距 ≤ 8（糾結）
       - 今日 K > D，且 K、D 皆較前一日上升

資料來源：臺灣證券交易所（殖利率、法人買賣超、歷史股價），範圍為上市個股。
需要與 tw_stock_daily.py 放在同一個資料夾。僅供資訊整理，不構成投資建議。

用法
  python screener.py --out docs/screener            # 非交易日自動略過
  python screener.py --out docs/screener --force    # 強制用最近一個交易日
"""
import argparse
import datetime as dt
import json
import sys
import time

from tw_stock_daily import CSS, RWD, OPENAPI, TZ, fetch_stocks, get, num, parse_date, pick, safe, warn

SLEEP = 1.2          # 每次請求間隔（秒），避免被證交所限流
DAYS_CHIP = 5        # 往前看幾個交易日的法人買賣超


# ----------------------------------------------------------------- 資料抓取
def month_starts(today, n=3):
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(dt.date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def fetch_history(code, months):
    """回傳依日期排序的 [{date,o,h,l,c}]（證交所個股日成交資訊，一次一個月）。"""
    rows = []
    for ym in months:
        j = get(f"{RWD}/afterTrading/STOCK_DAY",
                {"date": ym.strftime("%Y%m01"), "stockNo": code, "response": "json"})
        time.sleep(SLEEP)
        if j.get("stat") != "OK":
            continue
        f = j["fields"]
        ix = {k: f.index(k) for k in ("日期", "開盤價", "最高價", "最低價", "收盤價")}
        for r in j["data"]:
            vals = [num(r[ix[k]]) for k in ("開盤價", "最高價", "最低價", "收盤價")]
            if None in vals:
                continue
            rows.append({"date": parse_date(r[ix["日期"]]), "o": vals[0], "h": vals[1], "l": vals[2], "c": vals[3]})
    rows.sort(key=lambda x: x["date"])
    return rows


def fetch_yield():
    rows = get(f"{OPENAPI}/exchangeReport/BWIBBU_ALL")
    out = {}
    for r in rows:
        code = str(pick(r, "Code", "證券代號") or "")
        out[code] = (num(pick(r, "DividendYield", "殖利率(%)")),
                     num(pick(r, "PEratio", "本益比")),
                     num(pick(r, "PBratio", "股價淨值比")))
    return out


def fetch_chips(date):
    """某日三大法人個股買賣超，回傳 {code: (外資張, 投信張)}；尚未公布回傳 None。"""
    j = get(f"{RWD}/fund/T86", {"date": date.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"})
    time.sleep(SLEEP)
    if j.get("stat") != "OK":
        return None
    f = j["fields"]
    i_code = f.index("證券代號")
    i_for = next(i for i, x in enumerate(f) if "外" in x and "買賣超" in x)
    i_trust = next(i for i, x in enumerate(f) if x.startswith("投信買賣超"))
    return {r[i_code].strip(): ((num(r[i_for]) or 0) / 1000, (num(r[i_trust]) or 0) / 1000) for r in j["data"]}


# ----------------------------------------------------------------- 指標計算
def sma(vals, n):
    return [None if i + 1 < n else sum(vals[i + 1 - n:i + 1]) / n for i in range(len(vals))]


def kd(rows, n=9):
    """台灣常用 KD：RSV=(收-9日低)/(9日高-9日低)；K=2/3前K+1/3RSV；D=2/3前D+1/3K；起始 50。"""
    K, D = [None] * len(rows), [None] * len(rows)
    k = d = 50.0
    for i in range(n - 1, len(rows)):
        w = rows[i + 1 - n:i + 1]
        hh, ll = max(x["h"] for x in w), min(x["l"] for x in w)
        rsv = 50.0 if hh == ll else (rows[i]["c"] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        K[i], D[i] = k, d
    return K, D


def indicators(rows):
    """回傳指標 dict；資料不足回傳 None。"""
    if len(rows) < 30:
        return None
    c = [r["c"] for r in rows]
    n = len(c)
    m5, m10, m20 = sma(c, 5), sma(c, 10), sma(c, 20)
    K, D = kd(rows)
    out = {
        "m5": m5[-1] > m5[-2], "m10": m10[-1] > m10[-2], "m20": m20[-1] > m20[-2],
        "k": round(K[-1], 1), "d": round(D[-1], 1),
        "k_up": K[-1] > K[-2], "d_up": D[-1] > D[-2],
        "sell_ma10": c[-1] < m10[-1],
        "cross_ago": None, "tangle": None, "sell_kd": False,
    }
    for i in range(n - 1, max(n - 7, 9), -1):          # 最近 6 天內最新一次黃金交叉
        if K[i - 1] is not None and K[i - 1] <= D[i - 1] and K[i] > D[i]:
            out["cross_ago"] = n - 1 - i
            seg = [abs(K[j] - D[j]) for j in range(i - 5, i) if j >= 8 and K[j] is not None]
            if len(seg) == 5:
                out["tangle"] = round(sum(seg) / 5, 1)
            break
    for i in range(n - 1, n - 4, -1):                   # 近 3 日內死亡交叉
        if K[i - 1] is not None and K[i - 1] >= D[i - 1] and K[i] < D[i]:
            out["sell_kd"] = True
            break
    return out


def streak(series):
    """series 為由新到舊的買賣超張數；回傳連續買超天數。"""
    n = 0
    for v in series:
        if v is not None and v > 0:
            n += 1
        else:
            break
    return n


# ----------------------------------------------------------------- 網頁
PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>台股選股篩選器</title>
<style>__CSS__
.params{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px 18px}
.params label{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.params input[type=number]{width:66px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text)}
.params select{padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text)}
.funnel{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.funnel span.box{background:var(--head);border-radius:8px;padding:4px 10px}
.funnel b{font-size:1.05rem}
th.s{cursor:pointer;user-select:none}th.s:hover{text-decoration:underline}
.ok{color:var(--down);font-weight:600}.no{color:var(--muted)}
.badge{display:inline-block;min-width:1.7em;text-align:center;border-radius:6px;padding:0 4px;margin-right:3px;font-size:.8rem}
.badge.ok{background:rgba(24,128,56,.15)}.badge.no{background:var(--head)}
.warn{color:var(--up);font-size:.85rem}
.note{color:var(--muted);font-size:.85rem}
</style></head><body><main>
<h1>台股選股篩選器</h1>
<p class="note">資料日期：<b id="dt"></b>　｜　候選池 <b id="pool"></b> 檔（殖利率≥__POOLY__% 且外資或投信買超）　｜　資料每個交易日盤後更新</p>
<section><h2>篩選條件（可自行調整）</h2><div class="params">
<label>殖利率 ≥ <input type="number" id="y" value="5" step="0.5" min="0"> %</label>
<label>外資連續買超 ≥ <input type="number" id="fr" value="3" min="0" max="5"> 天</label>
<label>投信連續買超 ≥ <input type="number" id="tr" value="3" min="0" max="5"> 天</label>
<label>均線向上：<input type="checkbox" id="m5" checked>5日 <input type="checkbox" id="m10" checked>10日 <input type="checkbox" id="m20" checked>20日</label>
<label><input type="checkbox" id="kdOn" checked>KD 糾結後起漲</label>
<label>　黃金交叉在近 <input type="number" id="ago" value="3" min="0" max="5"> 日內</label>
<label>　交叉前5日 K、D 平均差距 ≤ <input type="number" id="gap" value="8" step="1" min="1"></label>
<label>　<input type="checkbox" id="kdUp" checked>今日 K、D 皆上升</label>
<label>至少符合 <select id="min"><option>5</option><option>4</option><option>3</option></select> 項（共5項）</label>
</div></section>
<section><h2>逐步篩選結果</h2><div class="funnel" id="funnel"></div></section>
<section><h2>符合的股票 <span class="note" id="cnt"></span></h2><div class="tw"><table id="tbl"></table></div>
<p class="note">項目：殖＝殖利率、外＝外資連買、投＝投信連買、均＝均線向上、K＝KD 起漲。點表頭可排序。「警示」為出場參考（跌破10日線、KD 近3日死亡交叉），尚非正式賣出規則。</p></section>
<p class="note">殖利率為證交所公布值（最近一次股利÷收盤價）。僅涵蓋上市個股。資訊整理工具，不構成投資建議。</p>
</main><script>
const D = __DATA__;
const $ = id => document.getElementById(id);
$('dt').textContent = D.date; $('pool').textContent = D.stocks.length;
const P = () => ({y:+$('y').value, fr:+$('fr').value, tr:+$('tr').value, m5:$('m5').checked, m10:$('m10').checked,
  m20:$('m20').checked, kdOn:$('kdOn').checked, ago:+$('ago').value, gap:+$('gap').value, kdUp:$('kdUp').checked, min:+$('min').value});
function evalRow(s,p){
  return {
    y: s.yield!=null && s.yield>=p.y,
    f: s.fr>=p.fr,
    t: s.tr>=p.tr,
    m: (!p.m5||s.m5)&&(!p.m10||s.m10)&&(!p.m20||s.m20),
    k: !p.kdOn || (s.cross_ago!=null && s.cross_ago<=p.ago && s.tangle!=null && s.tangle<=p.gap && s.k>s.d && (!p.kdUp||(s.k_up&&s.d_up)))
  };
}
const LAB = {y:'殖',f:'外',t:'投',m:'均',k:'K'};
const esc = x => String(x).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let sortKey='score', dir=-1;
const COLS = [
  ['code','代號',s=>s.code], ['name','名稱',s=>esc(s.name)],
  ['close','收盤',s=>s.close.toFixed(2)],
  ['pct','漲跌幅',s=>`<span class="${s.pct>0?'up':s.pct<0?'down':''}">${s.pct>0?'▲':s.pct<0?'▼':''}${Math.abs(s.pct).toFixed(2)}%</span>`],
  ['yield','殖利率%',s=>s.yield==null?'-':s.yield.toFixed(2)],
  ['fr','外資連買(天)',s=>s.fr], ['tr','投信連買(天)',s=>s.tr],
  ['f3','外資近3日(張)',s=>(Math.round(s.f3)||0).toLocaleString()], ['t3','投信近3日(張)',s=>(Math.round(s.t3)||0).toLocaleString()],
  ['kd','K / D',s=>`${s.k} / ${s.d}`],
  ['cross_ago','交叉(幾日前)',s=>s.cross_ago==null?'-':s.cross_ago],
  ['tangle','交叉前糾結度',s=>s.tangle==null?'-':s.tangle],
  ['score','符合項目',s=>Object.keys(LAB).map(k=>`<span class="badge ${s.c[k]?'ok':'no'}">${LAB[k]}</span>`).join('')],
  ['warn','警示',s=>`<span class="warn">${[s.sell_ma10?'跌破10日線':'',s.sell_kd?'KD死叉':''].filter(Boolean).join('、')}</span>`],
];
function val(s,k){ return k==='kd'?s.k : k==='warn'?(s.sell_ma10+s.sell_kd) : (s[k]==null?-1e9:s[k]); }
function render(){
  const p=P();
  const rows = D.stocks.map(s=>{const c=evalRow(s,p); return {...s,c,score:Object.values(c).filter(Boolean).length};});
  // 逐步篩選（累計）
  let cur = rows, html='';
  [['殖利率','y'],['外資','f'],['投信','t'],['均線','m'],['KD','k']].forEach(([n,k],i)=>{
    cur = cur.filter(r=>r.c[k]);
    html += (i?'<span>→</span>':'')+`<span class="box">${i?'再加':''}${n} <b>${cur.length}</b></span>`;
  });
  $('funnel').innerHTML = html;
  const show = rows.filter(r=>r.score>=p.min);
  show.sort((a,b)=>{const x=val(a,sortKey),y=val(b,sortKey); return (x<y?-1:x>y?1:(b.yield||0)-(a.yield||0))*dir;});
  $('cnt').textContent = `（${show.length} 檔）`;
  $('tbl').innerHTML = '<thead><tr>'+COLS.map(c=>`<th class="s" data-k="${c[0]}">${c[1]}${sortKey===c[0]?(dir>0?' ▲':' ▼'):''}</th>`).join('')+'</tr></thead><tbody>'+
    (show.length? show.map(s=>'<tr>'+COLS.map(c=>`<td>${c[2](s)}</td>`).join('')+'</tr>').join('')
     : `<tr><td colspan="${COLS.length}" style="text-align:center">目前條件下沒有符合的股票，可放寬條件（例如把「至少符合」改成 4 項，或把連買天數改成 2 天）。</td></tr>`)+'</tbody>';
  document.querySelectorAll('th.s').forEach(th=>th.onclick=()=>{const k=th.dataset.k; if(sortKey===k)dir*=-1; else{sortKey=k;dir=-1;} render();});
}
document.querySelectorAll('.params input,.params select').forEach(e=>e.addEventListener('input',render));
render();
</script></body></html>"""


def write_page(out_dir, payload, pool_yield):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (PAGE.replace("__CSS__", CSS).replace("__DATA__", data).replace("__POOLY__", f"{pool_yield:g}"))
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / "data.json").write_text(data, encoding="utf-8")


# ----------------------------------------------------------------- 主程式
def main():
    from pathlib import Path
    ap = argparse.ArgumentParser(description="台股選股篩選器")
    ap.add_argument("--out", default="docs/screener")
    ap.add_argument("--force", action="store_true", help="（已不需要，保留相容）")
    ap.add_argument("--pool-yield", type=float, default=3.0, help="候選池的殖利率下限（網頁條件不可低於此值）")
    ap.add_argument("--max-pool", type=int, default=150, help="候選池上限（越大跑越久）")
    args = ap.parse_args()

    today = dt.datetime.now(TZ).date()
    months = month_starts(today, 3)
    cal = safe("交易日曆", fetch_history, "2330", months)  # 以台積電的交易日當作交易日曆
    if not cal:
        print("無法取得交易日曆，結束。", file=sys.stderr)
        return 1
    dates = [r["date"] for r in cal]
    latest = dates[-1]
    if latest != today:
        print(f"最新交易日為 {latest}，不是今天（{today}）：今日休市或證交所尚未更新，使用最近交易日資料。")

    chip_days = dates[-DAYS_CHIP:][::-1]                      # 由新到舊
    chips = {}
    for d in chip_days:
        c = safe(f"法人買賣超 {d}", fetch_chips, d)
        if c is None:
            if d == latest:
                print(f"{latest} 的三大法人資料尚未公布，不更新。稍後再執行。")
                return 0
            break                                              # 較舊的缺資料就停止往前
        chips[d] = c
    used_days = [d for d in chip_days if d in chips]

    stocks = {s["code"]: s for s in (safe("個股行情", fetch_stocks) or [])}
    yields = safe("殖利率", fetch_yield) or {}
    if not stocks:
        print("無法取得個股行情，結束。", file=sys.stderr)
        return 1
    ref = stocks.get("2330")
    if ref and abs(ref["close"] - cal[-1]["c"]) > 1e-6:      # 個股行情還停在舊的一天 → 不更新，等下次排程
        print(f"個股行情尚未更新到 {latest}（台積電收盤 {ref['close']} ≠ {cal[-1]['c']}），不更新，稍後再試。")
        return 0

    cand = []
    for code, s in stocks.items():
        y, pe, pb = yields.get(code, (None, None, None))
        if y is None or y < args.pool_yield:
            continue
        fr_series = [chips[d].get(code, (None, None))[0] for d in used_days]
        tr_series = [chips[d].get(code, (None, None))[1] for d in used_days]
        fr, tr = streak(fr_series), streak(tr_series)
        if fr < 1 and tr < 1:
            continue
        cand.append({"code": code, "name": s["name"], "close": s["close"], "pct": round(s["pct"], 2),
                     "yield": y, "pe": pe, "pb": pb, "fr": fr, "tr": tr,
                     "f3": sum(v or 0 for v in fr_series[:3]), "t3": sum(v or 0 for v in tr_series[:3])})
    cand.sort(key=lambda x: (-(x["fr"] + x["tr"]), -x["yield"]))
    cand = cand[:args.max_pool]
    print(f"候選池 {len(cand)} 檔，開始抓歷史股價（約需 {len(cand) * 3 * SLEEP / 60:.0f} 分鐘）…")

    result = []
    for i, c in enumerate(cand, 1):
        try:
            ind = indicators(fetch_history(c["code"], months))
        except Exception as e:  # noqa: BLE001
            warn(f"{c['code']} 歷史股價失敗：{e}")
            continue
        if ind:
            result.append({**c, **ind})
        if i % 20 == 0:
            print(f"  進度 {i}/{len(cand)}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_page(out_dir, {"date": f"{latest:%Y-%m-%d}", "stocks": result}, args.pool_yield)
    print(f"完成：{out_dir / 'index.html'}（{len(result)} 檔）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
