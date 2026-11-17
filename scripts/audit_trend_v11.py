# -*- coding: utf-8 -*-
"""趋势系统 v1.1 回测结果审计：现金流、规则合规、仓位风险、持仓约束。"""
import csv, json, io, os, urllib.request

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "交易系统v1.1")
H = {"User-Agent": "Mozilla/5.0"}

def load(name):
    return list(csv.DictReader(open(os.path.join(BASE, name), encoding="utf-8-sig")))

flow = load("每日交易流水_回测_v11.csv")
trades = load("交易记录_回测_v11.csv")
curve = load("资金曲线_回测_v11.csv")
cache = json.load(open(os.path.join(BASE, "klines_cache_800.json"), encoding="utf-8"))

out = io.StringIO()
def p(*a): out.write(" ".join(str(x) for x in a) + "\n")
problems = 0

# 1 现金流
cash = 1000000.0; bad = 0; maxdiff = 0.0
for r in flow:
    amt = float(r["成交金额"])
    cash = round(cash + (-amt if r["操作"] == "买入" else amt), 2)
    d = abs(cash - float(r["成交后现金"])); maxdiff = max(maxdiff, d)
    if d > 0.001: bad += 1
p("现金流：不一致 %d 笔，最大偏差 %.6f，期末 %.2f" % (bad, maxdiff, cash))
problems += bad

# 2 买卖配对
nb = sum(1 for r in flow if r["操作"] == "买入")
ns = sum(1 for r in flow if r["操作"] == "卖出")
p("流水：买入 %d，卖出 %d，台账 %d" % (nb, ns, len(trades)))
if not (nb == ns == len(trades)): problems += 1

# 3 仓位约束
maxpos = max(int(r["持仓只数"]) for r in curve)
negcash = sum(1 for r in curve if float(r["现金"]) < -0.01)
p("最大持仓 %d 只，现金为负 %d 天" % (maxpos, negcash))
if maxpos > 8 or negcash: problems += 1

# 4 权益核对
pnl = sum(float(r["盈亏金额"]) for r in trades)
p("台账盈亏 %.2f，期初+盈亏 %.2f，曲线期末 %s" % (pnl, 1000000 + pnl, curve[-1]["总资产"]))
if abs(1000000 + pnl - float(curve[-1]["总资产"])) > 0.01: problems += 1

# 5 指数
u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000300"
     "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=0&end=20500101&lmt=800")
d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=H)).read().decode())["data"]["klines"]
hsd = [int(a[0].replace("-", "")) for a in (s.split(",") for s in d)]
hsc = [float(a[2]) for a in (s.split(",") for s in d)]
hp = {x: i for i, x in enumerate(hsd)}

viol = []; risk_bad = []; weight_bad = []
for r in trades:
    kk = cache.get(r["代码"]); ed = int(r["买入时间"].replace("-", ""))
    i = next((j for j, x in enumerate(kk) if x[0] == ed), None)
    if i is None or i < 130: continue
    c = [x[2] for x in kk[:i]]; v = [x[5] for x in kk[:i]]; h = [x[3] for x in kk[:i]]; l = [x[4] for x in kk[:i]]
    ma = lambda n: sum(c[-n:]) / n
    ma20, ma60, ma120 = ma(20), ma(60), ma(120)
    d20 = (c[-1]/ma20-1)*100; r60 = (c[-1]/c[-61]-1)*100
    hi = hp.get(kk[i-1][0]); hs20 = (hsc[hi]/hsc[hi-20]-1) if hi and hi >= 20 else 0
    r20 = (c[-1]/c[-21]-1)*100
    errs = []
    if not (ma20 > ma60 > ma120): errs.append("均线")
    if not (-1 <= d20 <= 6): errs.append("d20")
    if not (5 <= r60 <= 60): errs.append("r60")
    if r20 - hs20 <= 0: errs.append("相对强度")
    if hi is not None and hsc[hi] <= sum(hsc[hi-19:hi+1])/20: errs.append("指数<MA20")
    if errs: viol.append((r["买入时间"], r["名称"], ",".join(errs)))
    trs = [max(h[j]-l[j], abs(h[j]-c[j-1]), abs(l[j]-c[j-1])) for j in range(len(c)-14, len(c))]
    atr = sum(trs)/14
    shares = int(r["数量"]); eq = float(r["买入后总资产"])
    risk = shares*2*atr/eq*100; w = shares*float(r["买入价"])/eq*100
    if risk > 1.05: risk_bad.append((r["买入时间"], r["名称"], round(risk, 2)))
    if w > 13.0: weight_bad.append((r["买入时间"], r["名称"], round(w, 2)))

p("入场规则违规 %d 笔；单笔风险>1.05%% %d 笔；单笔权重>13%% %d 笔" % (len(viol), len(risk_bad), len(weight_bad)))
for x in (viol + risk_bad + weight_bad)[:8]: p("  ", x)
problems += len(viol) + len(risk_bad) + len(weight_bad)

# 6 持仓重复
dup = 0
for r in curve:
    codes = [x.split("(")[1].split(")")[0] for x in r["持仓明细"].split("; ") if "(" in x]
    if len(codes) != len(set(codes)): dup += 1
p("同日重复持仓 %d 天" % dup)
problems += dup

p("")
p("审计结论：%s（问题数 %d）" % ("通过" if problems == 0 else "存在问题", problems))
print(out.getvalue())
open(os.path.join(BASE, "审计报告_v11.txt"), "w", encoding="utf-8").write(out.getvalue())
