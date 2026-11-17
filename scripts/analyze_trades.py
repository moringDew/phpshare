# -*- coding: utf-8 -*-
"""分析回测交易：找出亏损共性，检验候选过滤条件。"""
import csv, json, io, urllib.request, time
from collections import defaultdict

CSV = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\交易记录_回测_v1.csv"
CACHE = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\klines_cache_800.json"
OUT = r"C:\Users\kane\Documents\WeChat Files\wxid_xsdfdlvetmoa22\FileStorage\File\2025-06\website\website\quant-platform\output\交易系统v1.1\交易分析.txt"
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

def get(u):
    return urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=25).read().decode("utf-8", "ignore")

cache = json.load(open(CACHE, encoding="utf-8"))

# 行业 + 当前市值
ind_map = {}
mcap_map = {}
pn = 1
while True:
    u = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f20"
         "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f2,f12,f14,f20,f100" % pn)
    d = json.loads(get(u))["data"]
    dif = d.get("diff") or []
    if not dif: break
    for s in dif:
        code = str(s["f12"])
        ind_map[code] = s.get("f100", "")
        try:
            mcap_map[code] = float(s["f20"]) / float(s["f2"])
        except Exception:
            mcap_map[code] = 0
    if pn * 100 >= d["total"]: break
    pn += 1
    time.sleep(0.1)

rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
trades = []
for r in rows:
    code = r["代码"]
    k = cache.get(code)
    if not k: continue
    d0 = int(r["买入时间"].replace("-", ""))
    idx = next((i for i, x in enumerate(k) if x[0] == d0), None)
    if idx is None: continue
    c = [x[2] for x in k]; h = [x[3] for x in k]; l = [x[4] for x in k]
    if idx >= 15:
        trs = [max(h[j]-l[j], abs(h[j]-c[j-1]), abs(l[j]-c[j-1])) for j in range(idx-14, idx+1)]
        atr = sum(trs)/15
    else:
        atr = 0
    px = c[idx]
    trades.append(dict(code=code, name=r["名称"], ind=ind_map.get(code, ""),
                       rel=float(r["20日相对强度%"]), d20=float(r["距MA20%"]),
                       r60=float(r["60日涨幅%"]), ret=float(r["收益率%"]),
                       pnl=float(r["盈亏金额"]), days=int(r["持有天数"]),
                       price=px, atrp=atr/px*100 if px else 0,
                       mcap=mcap_map.get(code, 0)*px, date=d0,
                       reason=r["卖出原因"]))

buf = io.StringIO()
def p(*a): buf.write(" ".join(str(x) for x in a) + "\n")

p("总交易 %d 笔，总盈亏 %+.0f 元，胜率 %.1f%%" % (
    len(trades), sum(t["pnl"] for t in trades),
    sum(1 for t in trades if t["ret"] > 0)*100.0/len(trades)))

def bucket(name, key, edges):
    p("\n=== 按%s ===" % name)
    p("%-18s %5s %7s %8s %12s %10s" % ("区间", "笔数", "胜率", "平均收益%", "总盈亏(元)", "盈亏占比%"))
    total = sum(t["pnl"] for t in trades)
    for i in range(len(edges)-1):
        lo, hi = edges[i], edges[i+1]
        sub = [t for t in trades if lo <= t[key] < hi]
        if not sub: continue
        w = sum(1 for t in sub if t["ret"] > 0)
        pn = sum(t["pnl"] for t in sub)
        p("%-18s %5d %6.1f%% %8.2f %12.0f %10.1f" % (
            "%g ~ %g" % (lo, hi), len(sub), w*100.0/(len(sub)), sum(t["ret"] for t in sub)/(len(sub)), pn, pn/total*100 if total else 0))
    # 极端组
    sub = [t for t in trades if t[key] >= edges[-1]]
    if sub:
        w = sum(1 for t in sub if t["ret"] > 0)
        pn = sum(t["pnl"] for t in sub)
        p("%-18s %5d %6.1f%% %8.2f %12.0f %10.1f" % ("%g+" % edges[-1], len(sub), w*100.0/len(sub), sum(t["ret"] for t in sub)/len(sub), pn, pn/total*100 if total else 0))

bucket("距MA20%", "d20", [-1, 0, 1, 2, 3, 4, 6])
bucket("20日相对强度%", "rel", [-100, 0, 2, 5, 10, 100])
bucket("60日涨幅%", "r60", [5, 10, 15, 20, 30, 40, 60])
bucket("ATR%", "atrp", [0, 2, 2.5, 3, 4, 6, 20])
bucket("股价", "price", [0, 5, 10, 20, 50, 1000])
bucket("持有天数", "days", [0, 5, 10, 15, 25, 60])

p("\n=== 按行业（笔数≥5）===")
by = defaultdict(list)
for t in trades: by[t["ind"]].append(t)
total = sum(t["pnl"] for t in trades)
for ind, sub in sorted(by.items(), key=lambda kv: -len(kv[1])):
    if len(sub) < 5: continue
    w = sum(1 for t in sub if t["ret"] > 0)
    pn = sum(t["pnl"] for t in sub)
    p("%-12s %4d笔 胜率%5.1f%% 平均%+6.2f%% 盈亏%+10.0f" % (ind, len(sub), w*100.0/len(sub), sum(t["ret"] for t in sub)/len(sub), pn))

p("\n=== 亏损最大的15笔 ===")
for t in sorted(trades, key=lambda x: x["pnl"])[:15]:
    p("%s %-6s %-8s 买入/%s 收益率%+6.2f%% 盈亏%+9.0f 相对强度%5.1f 距MA20%5.1f 60日%5.1f ATR%4.1f 原因%s" % (
        t["date"], t["code"], t["name"], t["ind"], t["ret"], t["pnl"], t["rel"], t["d20"], t["r60"], t["atrp"], t["reason"]))

p("\n=== 盈利最大的15笔 ===")
for t in sorted(trades, key=lambda x: -x["pnl"])[:15]:
    p("%s %-6s %-8s 买入/%s 收益率%+6.2f%% 盈亏%+9.0f 相对强度%5.1f 距MA20%5.1f 60日%5.1f ATR%4.1f 原因%s" % (
        t["date"], t["code"], t["name"], t["ind"], t["ret"], t["pnl"], t["rel"], t["d20"], t["r60"], t["atrp"], t["reason"]))

# 候选过滤条件检验（全样本 + 前后半段）
p("\n" + "="*70)
p("候选过滤条件效果（前半段/后半段分开验证）")
p("="*70)
mid = sorted(t["date"] for t in trades)[len(trades)//2]
def stat(sub):
    if not sub: return (0, 0, 0, 0)
    w = sum(1 for t in sub if t["ret"] > 0)
    return (len(sub), w*100.0/len(sub), sum(t["ret"] for t in sub)/len(sub), sum(t["pnl"] for t in sub))

filters = [
    ("原始", lambda t: True),
    ("ATR% <= 6", lambda t: t["atrp"] <= 6),
    ("ATR% <= 4", lambda t: t["atrp"] <= 4),
    ("距MA20 <= 4%", lambda t: t["d20"] <= 4),
    ("60日涨幅 <= 40%", lambda t: t["r60"] <= 40),
    ("相对强度 >= 2%", lambda t: t["rel"] >= 2),
    ("股价 >= 5元", lambda t: t["price"] >= 5),
    ("组合: ATR<=6且距MA20<=4", lambda t: t["atrp"] <= 6 and t["d20"] <= 4),
    ("组合: ATR<=6且60日<=40且相对>=2", lambda t: t["atrp"] <= 6 and t["r60"] <= 40 and t["rel"] >= 2),
]
p("%-38s %-26s %-26s" % ("条件", "前半段", "后半段"))
for name, f in filters:
    a = [t for t in trades if f(t) and t["date"] < mid]
    b = [t for t in trades if f(t) and t["date"] >= mid]
    sa = stat(a); sb = stat(b)
    p("%-38s N=%3d 胜率%4.0f%% 平均%+5.2f%%   N=%3d 胜率%4.0f%% 平均%+5.2f%%" % (
        name, sa[0], sa[1], sa[2], sb[0], sb[1], sb[2]))

open(OUT, "w", encoding="utf-8").write(buf.getvalue())
print("written", OUT)
