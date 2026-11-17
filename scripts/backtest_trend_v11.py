# -*- coding: utf-8 -*-
"""趋势跟随系统 v1.1 的 2-3 年回测（纯标准库）。"""
import json, urllib.request, time, io, os, datetime, csv
from concurrent.futures import ThreadPoolExecutor
from collections import Counter

OUT = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\回测_趋势系统v11.txt"
CACHE = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\klines_cache_800.json"
UNI_CACHE = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\universe_v11.json"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
     "Referer": "https://quote.eastmoney.com/"}
START_CAP = 1_000_000.0
COST = 0.001
MAX_POS = 8
MAX_W = 0.125

def get(u, timeout=25):
    return urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=timeout).read().decode("utf-8", "ignore")

def num(x, d=None):
    try: return float(x)
    except Exception: return d

FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"

def load_universe():
    out = []; pn = 1
    while True:
        u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f20&fs=%s"
             "&fields=f2,f9,f12,f14,f20,f100" % (pn, FS))
        d = json.loads(get(u))["data"]
        dif = d.get("diff") or []
        if not dif: break
        out.extend(dif)
        if len(out) >= d["total"]: break
        pn += 1; time.sleep(0.1)
    res = []
    for s in out:
        nm = s.get("f14", "")
        if "ST" in nm or "退" in nm: continue
        mc = num(s.get("f20"), 0) or 0
        px = num(s.get("f2"), 0) or 0
        if mc < 150e8 or px <= 0: continue
        res.append(dict(code=str(s["f12"]), name=nm, px=px, mcap=mc, shares=mc/px, ind=s.get("f100", "")))
    return res

def fetch(sec, lmt=800, fqt=1):
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=%d&end=20500101&lmt=%d" % (sec, fqt, lmt))
    for _ in range(3):
        try:
            d = json.loads(get(u))["data"]["klines"]
            return [[int(a[0].replace("-", "")), float(a[1]), float(a[2]), float(a[3]), float(a[4]), float(a[5])]
                    for a in (s.split(",") for s in d)]
        except Exception:
            time.sleep(0.4)
    return None

def secid(code):
    if code[0] == "6": return "1." + code
    if code[0] == "0" or code[0] == "3": return "0." + code
    return "0." + code

if os.path.exists(UNI_CACHE):
    uni = json.load(open(UNI_CACHE, encoding="utf-8"))
else:
    uni = load_universe()
    json.dump(uni, open(UNI_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
print("universe", len(uni))
cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
todo = [s for s in uni if s["code"] not in cache]
print("to fetch", len(todo))
if todo:
    def work(s): return s["code"], fetch(secid(s["code"]))
    with ThreadPoolExecutor(max_workers=12) as ex:
        for code, k in ex.map(work, todo):
            if k: cache[code] = k
    json.dump(cache, open(CACHE, "w", encoding="utf-8"))
print("cached", len(cache))

hs = fetch("1.000300", 800, 0)
hs_dates = [x[0] for x in hs]
hs_close = [x[2] for x in hs]
hs_idx = {d: i for i, d in enumerate(hs_dates)}
hs_r20 = {}
hs_ma20 = {}
for d, i in hs_idx.items():
    hs_r20[d] = (hs_close[i]/hs_close[i-20] - 1) if i >= 20 else 0.0
    hs_ma20[d] = (sum(hs_close[i-19:i+1]) / 20) if i >= 19 else None

def wd(ymd):
    return datetime.date(ymd // 10000, ymd // 100 % 100, ymd % 100).weekday()

states = {}
cur = "defense"
for j, d in enumerate(hs_dates):
    states[d] = cur
    if j >= 64 and wd(d) == 4:
        ma60 = sum(hs_close[j-59:j+1])/60
        ma60_5 = sum(hs_close[j-64:j-4])/60
        rising = ma60 > ma60_5
        cur = "attack" if (hs_close[j] > ma60 and rising) else ("cash" if (hs_close[j] < ma60 and not rising) else "defense")

IND = {}
for s in uni:
    k = cache.get(s["code"])
    if not k or len(k) < 200: continue
    dates = [x[0] for x in k]
    o = [x[1] for x in k]; c = [x[2] for x in k]; h = [x[3] for x in k]; l = [x[4] for x in k]; v = [x[5] for x in k]
    n = len(k)
    ma20 = [None]*n; ma60 = [None]*n; ma120 = [None]*n
    vma5 = [None]*n; vma20 = [None]*n; atr = [None]*n; amt20 = [None]*n
    s20 = s60 = s120 = sv5 = sv20 = sa = 0.0
    for i in range(n):
        s20 += c[i]; s60 += c[i]; s120 += c[i]; sv5 += v[i]; sv20 += v[i]; sa += c[i]*v[i]*100
        if i >= 20: s20 -= c[i-20]; sv20 -= v[i-20]; sa -= c[i-20]*v[i-20]*100
        if i >= 60: s60 -= c[i-60]
        if i >= 120: s120 -= c[i-120]
        if i >= 5: sv5 -= v[i-5]
        if i >= 19: ma20[i] = s20/20; vma20[i] = sv20/20; amt20[i] = sa/20
        if i >= 59: ma60[i] = s60/60
        if i >= 119: ma120[i] = s120/120
        if i >= 4: vma5[i] = sv5/5
        if i >= 14:
            trs = [max(h[j]-l[j], abs(h[j]-c[j-1]), abs(l[j]-c[j-1])) for j in range(i-13, i+1)]
            atr[i] = sum(trs)/14
    IND[s["code"]] = dict(dates=dates, pmap={d: i for i, d in enumerate(dates)}, o=o, c=c, h=h, l=l, v=v,
                          ma20=ma20, ma60=ma60, ma120=ma120, vma5=vma5, vma20=vma20, atr=atr, amt20=amt20)

code_ind = {s["code"]: s["ind"] for s in uni}
code_name = {s["code"]: s["name"] for s in uni}

START_I = 140
positions = {}
pending = []
cash = START_CAP
peak_eq = START_CAP
pause_until = -1
pause_reset_day = -1
risk_state = "normal"
equity_curve = []
trade_log = []
daily_dates = []
exposure_hist = []
daily_cash = []
daily_pos = []
daily_holdings = []
daily_trades = []

for t in range(START_I, len(hs_dates)):
    date = hs_dates[t]
    daily_dates.append(date)
    state = states.get(date, "defense")
    trades_today = []

    # 执行挂单
    still = []
    for item in pending:
        act = item["act"]; code = item["code"]
        ind = IND.get(code)
        if not ind: continue
        i = ind["pmap"].get(date)
        if i is None or i < 1:
            still.append(item); continue
        op = ind["o"][i]; prev = ind["c"][i-1]
        if act == "sell":
            if code not in positions: continue
            if op <= prev * 0.905:
                still.append(item); continue
            p = positions.pop(code)
            proceeds = round(p["shares"] * op * (1 - COST), 2)
            cash = round(cash + proceeds, 2)
            eq_after_sell = cash + sum(q["shares"]*q["last"] for q in positions.values())
            pnl = round(proceeds - p["entry_cost"], 2)
            trade_log.append(dict(
                code=code, name=code_name.get(code, ""),
                entry_date=p["entry_date"], exit_date=date,
                entry=p["entry"], exit=op, shares=p["shares"],
                entry_reason=p["entry_reason"], entry_state=p["entry_state"],
                entry_rel=p["entry_rel"], entry_d20=p["entry_d20"], entry_r60=p["entry_r60"],
                entry_cost=p["entry_cost"], proceeds=proceeds,
                cash_after_buy=p["cash_after_buy"], equity_after_buy=p["equity_after_buy"],
                cash_after_sell=cash, equity_after_sell=eq_after_sell, pnl=pnl,
                ret=pnl/p["entry_cost"]*100,
                days=t-p["entry_t"], reason=p.get("reason", "")))
            trades_today.append(dict(act="卖出", code=code, name=code_name.get(code, ""),
                                     shares=p["shares"], price=op, amount=proceeds,
                                     cash_after=cash, reason=p.get("reason", ""), state=state,
                                     entry_date=p["entry_date"], pnl=pnl))
        else:
            if state != "attack": continue
            if op >= prev * 1.095: continue
            if code in positions: continue
            eq = cash + sum(q["shares"]*q["last"] for q in positions.values())
            a = (ind["atr"][i-1] if i >= 1 else None) or (op * 0.03)
            # 1% 风险对应的股数 = 总资金×1% ÷ (2×ATR)；再受 12.5% 权重和现金约束
            risk_shares = (eq * 0.01 / (2 * a)) if a > 0 else 0
            weight_shares = (eq * item["w"]) / (op * (1 + COST))
            cash_shares = cash / (op * (1 + COST))
            shares = int(min(risk_shares, weight_shares, cash_shares) // 100) * 100
            if shares < 100: continue
            cost = round(shares * op * (1 + COST), 2)
            if cost > cash: continue
            cash = round(cash - cost, 2)
            positions[code] = dict(shares=shares, entry=op, entry_t=t, high=op, last=op,
                                   atr=a, below=0, reason="", entry_date=date,
                                   entry_reason=item["trig"], entry_state=state,
                                   entry_rel=item["rel"], entry_d20=item["d20"], entry_r60=item["r60"],
                                   entry_cost=cost)
            eq_after_buy = cash + sum(q["shares"]*q["last"] for q in positions.values())
            positions[code]["cash_after_buy"] = cash
            positions[code]["equity_after_buy"] = eq_after_buy
            trades_today.append(dict(act="买入", code=code, name=code_name.get(code, ""),
                                     shares=shares, price=op, amount=cost,
                                     cash_after=cash, reason=item["trig"], state=state,
                                     entry_date=date, pnl=None))
    pending = still

    # 更新 & 卖出信号
    sells = []
    for code, p in list(positions.items()):
        ind = IND[code]
        i = ind["pmap"].get(date)
        if i is None: continue
        cl = ind["c"][i]; p["last"] = cl
        p["high"] = max(p["high"], cl)
        held = t - p["entry_t"]
        if cl < (ind["ma60"][i] or 0):
            sells.append((code, "跌破MA60")); continue
        if p["high"] - cl > 2.5 * p["atr"]:
            sells.append((code, "移动止损")); continue
        if held >= 5:
            if ind["ma20"][i] and cl < ind["ma20"][i]:
                p["below"] += 1
            else:
                p["below"] = 0
            if p["below"] >= 3:
                sells.append((code, "跌破MA20三日")); continue
            if held >= 20 and (cl/p["entry"]-1) < 0.05 and ind["ma20"][i] and cl < ind["ma20"][i]:
                sells.append((code, "时间止损")); continue

    # 组合回撤控制（-10% 减半并暂停 5 日；-15% 清仓并暂停 10 日；暂停期满重置回撤基准）
    pos_val_now = sum(p["shares"] * p["last"] for p in positions.values())
    eq_now = cash + pos_val_now
    if pause_reset_day >= 0 and t >= pause_reset_day:
        peak_eq = eq_now
        pause_reset_day = -1
        risk_state = "normal"
    if eq_now > peak_eq: peak_eq = eq_now
    dd = (eq_now / peak_eq - 1) if peak_eq else 0.0
    if risk_state == "normal":
        if dd <= -0.15 and positions:
            risk_state = "liquidate"
            for code in positions:
                if all(c != code for c, _ in sells):
                    sells.append((code, "回撤清仓"))
            pause_until = max(pause_until, t + 10)
            pause_reset_day = t + 10
        elif dd <= -0.10 and positions:
            risk_state = "halve"
            cur_exp = pos_val_now
            target = eq_now * 0.5
            if cur_exp > target:
                for code, p in sorted(positions.items(), key=lambda kv: kv[1]["last"] / kv[1]["entry"] - 1):
                    if cur_exp <= target: break
                    if all(c != code for c, _ in sells):
                        sells.append((code, "回撤减半"))
                        cur_exp -= p["shares"] * p["last"]
            pause_until = max(pause_until, t + 5)
            pause_reset_day = t + 5
    risk_halve = (risk_state == "halve")
    risk_liquidate = (risk_state == "liquidate")

    if state != "attack" and positions:
        eq = cash + sum(q["shares"]*q["last"] for q in positions.values())
        cap = 0.30 if state == "defense" else 0.10
        cur_exp = sum(q["shares"]*q["last"] for q in positions.values())
        if eq > 0 and cur_exp/eq > cap:
            for code, p in sorted(positions.items(), key=lambda kv: kv[1]["last"]/kv[1]["entry"]-1):
                if cur_exp/eq <= cap: break
                if all(c != code for c, _ in sells):
                    sells.append((code, "市场降仓"))
                    cur_exp -= p["shares"]*p["last"]

    for code, reason in sells:
        if code in positions and all(a["code"] != code for a in pending):
            positions[code]["reason"] = reason
            pending.append({"act": "sell", "code": code})

    # 买入（新增：信号当日沪深300必须在MA20上方）
    hsma20 = hs_ma20.get(date)
    idx_ok = hsma20 is not None and hs_close[hs_idx[date]] > hsma20
    if (state == "attack" and idx_ok and not risk_halve and not risk_liquidate
            and t > pause_until and len(positions) < MAX_POS):
        slots = MAX_POS - len(positions)
        hs20 = hs_r20.get(date, 0.0)
        cand = []
        for s in uni:
            code = s["code"]
            if code in positions: continue
            ind = IND.get(code)
            if not ind: continue
            i = ind["pmap"].get(date)
            if i is None or i < 130: continue
            ma20 = ind["ma20"][i]; ma60 = ind["ma60"][i]; ma120 = ind["ma120"][i]
            if not (ma20 and ma60 and ma120): continue
            if not (ma20 > ma60 > ma120): continue
            if ma60 <= ind["ma60"][i-5]: continue
            cl = ind["c"][i]
            d20 = cl/ma20 - 1
            if not (-0.01 <= d20 <= 0.06): continue
            r60 = cl/ind["c"][i-60] - 1
            if not (0.05 <= r60 <= 0.60): continue
            r20 = cl/ind["c"][i-20] - 1
            if r20 - hs20 <= 0: continue
            if (ind["amt20"][i] or 0) < 2e8: continue
            vma5 = ind["vma5"][i]; vma20 = ind["vma20"][i]
            trig = None
            if vma5 and ind["v"][i] <= 0.8*vma5 and cl > ma20:
                trig = "缩量回踩"
            elif vma20 and cl >= max(ind["h"][i-19:i]) and ind["v"][i] >= 1.5*vma20:
                trig = "放量突破"
            if not trig: continue
            cand.append((r20 - hs20, code, trig, d20, r60))
        cand.sort(key=lambda x: -x[0])
        ind_count = Counter(code_ind.get(c, "") for c in positions)
        taken = 0
        for rel, code, trig, d20, r60 in cand:
            if taken >= slots: break
            indn = code_ind.get(code, "")
            if ind_count[indn] >= 2: continue
            if any(a["act"] == "buy" and a["code"] == code for a in pending): continue
            pending.append({"act": "buy", "code": code, "w": MAX_W, "trig": trig,
                            "rel": rel, "d20": d20, "r60": r60, "state": state})
            ind_count[indn] += 1
            taken += 1

    pos_val = sum(p["shares"]*p["last"] for p in positions.values())
    eq = cash + pos_val
    equity_curve.append(eq)
    daily_cash.append(cash)
    daily_pos.append(pos_val)
    exposure_hist.append(pos_val/eq if eq > 0 else 0)
    snap = []
    for code, p in positions.items():
        snap.append(dict(code=code, name=code_name.get(code, ""), shares=p["shares"],
                         entry=p["entry"], close=p["last"], value=p["shares"]*p["last"],
                         pnl=p["shares"]*(p["last"]-p["entry"]),
                         ret=(p["last"]/p["entry"]-1)*100,
                         entry_date=p["entry_date"], entry_reason=p["entry_reason"],
                         days=t-p["entry_t"]))
    daily_holdings.append(snap)
    daily_trades.append(trades_today)

buf = io.StringIO()
def p(*a): buf.write(" ".join(str(x) for x in a) + "\n")

eq0, eq1 = equity_curve[0], equity_curve[-1]
days = len(equity_curve); years = days/250.0
cagr = (eq1/eq0)**(1/years) - 1
mx = eq0; mdd = 0
for x in equity_curve:
    mx = max(mx, x); mdd = min(mdd, x/mx-1)

year_eq = {}
for d, e in zip(daily_dates, equity_curve):
    year_eq[d//10000] = e
annual = {}; prev = eq0
for y in sorted(year_eq):
    annual[y] = year_eq[y]/prev - 1; prev = year_eq[y]

b0 = hs_close[hs_idx[daily_dates[0]]]; b1 = hs_close[hs_idx[daily_dates[-1]]]
bret = b1/b0 - 1

n = len(trade_log)
wins = [t for t in trade_log if t["ret"] > 0]
loss = [t for t in trade_log if t["ret"] <= 0]
pf = (sum(t["ret"] for t in wins)/abs(sum(t["ret"] for t in loss))) if loss and sum(t["ret"] for t in loss) else float("inf")

p("="*72)
p("趋势跟随系统 v1.1 回测（修正版）")
p("="*72)
p("区间 %s ~ %s（%d 交易日，约 %.1f 年）" % (daily_dates[0], daily_dates[-1], days, years))
p("标的池：当前市值≥150亿，共 %d 只（存在幸存者偏差）" % len(uni))
p("初始 %.0f → 期末 %.0f" % (eq0, eq1))
p("总收益 %+.2f%%   年化 %+.2f%%   最大回撤 %.2f%%" % ((eq1/eq0-1)*100, cagr*100, mdd*100))
p("沪深300 同期 %+.2f%%   年化 %+.2f%%" % (bret*100, ((1+bret)**(1/years)-1)*100))
p("平均仓位 %.1f%%   期末现金 %.0f   期末持仓市值 %.0f   期末持仓 %d 只"
  % (sum(exposure_hist)/len(exposure_hist)*100, cash, daily_pos[-1], len(positions)))
p("累计变动金额 %+.0f 元" % (eq1-eq0))
p("")
p("年度收益：")
for y in sorted(year_eq):
    p("  %d  %+7.2f%%" % (y, annual[y]*100))
p("")
p("交易统计：")
p("  笔数 %d   胜率 %.1f%%   平均每笔 %+.2f%%" % (n, len(wins)*100.0/n if n else 0, sum(t["ret"] for t in trade_log)/n if n else 0))
if wins and loss:
    p("  平均盈利 %+.2f%%   平均亏损 %+.2f%%   盈亏比 %.2f   利润因子 %.2f" % (
        sum(t["ret"] for t in wins)/len(wins), sum(t["ret"] for t in loss)/len(loss),
        (sum(t["ret"] for t in wins)/len(wins))/abs(sum(t["ret"] for t in loss)/len(loss)), pf))
p("  平均持有 %d 天   最大单笔 %+.1f%%   最小单笔 %+.1f%%" % (
    sum(t["days"] for t in trade_log)/n if n else 0,
    max((t["ret"] for t in trade_log), default=0), min((t["ret"] for t in trade_log), default=0)))
rc = Counter(t["reason"] for t in trade_log)
p("  卖出原因：" + ", ".join("%s×%d" % (k, v) for k, v in rc.most_common()))
p("")
p("按买入信号分组：")
for trig in ("缩量回踩", "放量突破"):
    sub = [t for t in trade_log if t["entry_reason"] == trig]
    if not sub: continue
    w_ = sum(1 for t in sub if t["ret"] > 0)
    p("  %s  笔数%3d  胜率%5.1f%%  平均%+6.2f%%" % (trig, len(sub), w_*100.0/len(sub), sum(t["ret"] for t in sub)/len(sub)))
p("")
p("按卖出原因分组：")
for reason, cnt in rc.most_common():
    sub = [t for t in trade_log if t["reason"] == reason]
    w_ = sum(1 for t in sub if t["ret"] > 0)
    p("  %-8s 笔数%3d  胜率%5.1f%%  平均%+6.2f%%" % (reason, cnt, w_*100.0/len(sub), sum(t["ret"] for t in sub)/len(sub)))

# 逐笔 CSV
def ymd(d):
    return "%d-%02d-%02d" % (d//10000, d//100 % 100, d % 100)
STATE_CN = {"attack": "进攻", "defense": "防守", "cash": "空仓"}
CSV = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\交易记录_回测_v11.csv"
with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["序号", "代码", "名称", "买入时间", "买入价", "买入原因", "买入时市场",
                 "20日相对强度%", "距MA20%", "60日涨幅%", "数量",
                 "买入金额", "买入后剩余现金", "买入后总资产",
                 "卖出时间", "卖出价", "卖出原因", "持有天数",
                 "卖出金额", "卖出后剩余现金", "卖出后总资产",
                 "盈亏金额", "累计盈亏", "收益率%", "状态", "盈亏方向"])
    cum = 0.0
    for i, t in enumerate(trade_log, 1):
        cum += t["pnl"]
        wr.writerow([i, t["code"], t["name"], ymd(t["entry_date"]), "%.2f" % t["entry"],
                     t["entry_reason"], STATE_CN.get(t["entry_state"], t["entry_state"]),
                     "%.2f" % (t["entry_rel"]*100), "%.2f" % (t["entry_d20"]*100),
                     "%.2f" % (t["entry_r60"]*100), t["shares"],
                     "%.2f" % t["entry_cost"], "%.2f" % t["cash_after_buy"], "%.2f" % t["equity_after_buy"],
                     ymd(t["exit_date"]), "%.2f" % t["exit"], t["reason"], t["days"],
                     "%.2f" % t["proceeds"], "%.2f" % t["cash_after_sell"], "%.2f" % t["equity_after_sell"],
                     "%.2f" % t["pnl"], "%.2f" % cum, "%.2f" % t["ret"], "已平仓",
                     "盈" if t["pnl"] > 0 else "亏"])

# 每日资金曲线 CSV（含当日买卖与持仓明细）
DAILY = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\资金曲线_回测_v11.csv"
with open(DAILY, "w", encoding="utf-8-sig", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["日期", "现金", "持仓市值", "总资产", "当日盈亏", "当日盈亏%", "累计收益率%", "仓位%",
                 "持仓只数", "当日买入", "当日卖出", "持仓明细"])
    prev_eq = eq0
    for idx, d in enumerate(daily_dates):
        eq = equity_curve[idx]; cc = daily_cash[idx]; pv = daily_pos[idx]; ex = exposure_hist[idx]
        chg = eq - prev_eq
        buys = "; ".join("%s(%s) %d股@%.2f" % (x["name"], x["code"], x["shares"], x["price"])
                         for x in daily_trades[idx] if x["act"] == "买入")
        sells = "; ".join("%s(%s) %d股@%.2f" % (x["name"], x["code"], x["shares"], x["price"])
                          for x in daily_trades[idx] if x["act"] == "卖出")
        detail = "; ".join("%s(%s) %d股 市值%.2f (%.1f%%)" % (h["name"], h["code"], h["shares"], h["value"], h["ret"])
                           for h in daily_holdings[idx])
        wr.writerow([ymd(d), "%.2f" % cc, "%.2f" % pv, "%.2f" % eq, "%.2f" % chg,
                     "%.2f" % (chg/prev_eq*100 if prev_eq else 0),
                     "%.2f" % ((eq/eq0-1)*100), "%.1f" % (ex*100),
                     len(daily_holdings[idx]), buys, sells, detail])
        prev_eq = eq

# 每日持仓明细 CSV（一行一只）
HOLD = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\每日持仓_回测_v11.csv"
with open(HOLD, "w", encoding="utf-8-sig", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["日期", "代码", "名称", "数量", "成本价", "收盘价", "市值", "盈亏金额", "盈亏%",
                 "买入时间", "卖出时间", "状态", "买入原因", "持有天数"])
    for d, snap in zip(daily_dates, daily_holdings):
        for h in snap:
            wr.writerow([ymd(d), h["code"], h["name"], h["shares"], "%.2f" % h["entry"],
                         "%.2f" % h["close"], "%.2f" % h["value"], "%.2f" % h["pnl"],
                         "%.2f" % h["ret"], ymd(h["entry_date"]), "", "持仓中",
                         h["entry_reason"], h["days"]])

# 每日交易流水 CSV（按日期排列的全部买卖，标记买入/卖出时间与盈亏）
FLOW = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\每日交易流水_回测_v11.csv"
with open(FLOW, "w", encoding="utf-8-sig", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["成交日期", "操作", "代码", "名称", "数量", "成交价", "成交金额", "成交后现金",
                 "买入时间", "卖出时间", "盈亏", "市场状态", "原因"])
    for d, lst in zip(daily_dates, daily_trades):
        for x in lst:
            buy_t = ymd(d) if x["act"] == "买入" else ymd(x["entry_date"])
            sell_t = ymd(d) if x["act"] == "卖出" else ""
            pnl = "" if x["pnl"] is None else "%.2f" % x["pnl"]
            wr.writerow([ymd(d), x["act"], x["code"], x["name"], x["shares"], "%.2f" % x["price"],
                         "%.2f" % x["amount"], "%.2f" % x["cash_after"], buy_t, sell_t, pnl,
                         STATE_CN.get(x["state"], x["state"]), x["reason"]])

open(OUT, "w", encoding="utf-8").write(buf.getvalue())
print("written", OUT)
print("written", CSV)
print("written", DAILY)
print("written", HOLD)
print("written", FLOW)
