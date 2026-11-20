#!/usr/bin/env python3
"""t_0930e064: summarise pair.jsonl / res jsonl by (label, cond). usage: summ.py <file> [label-prefix]"""
import json, sys
from collections import defaultdict
rows = [json.loads(l) for l in open(sys.argv[1])]
pre = sys.argv[2] if len(sys.argv) > 2 else ""
g = defaultdict(list)
for r in rows:
    if r["label"].startswith(pre):
        g[(r["label"], r.get("cond", "-"))].append(r)
def q(v, p):
    v = sorted(v); return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]
for k, rs in g.items():
    ms = [r["ms"] for r in rs]
    sib = sum(1 for r in rs if r.get("sibling_units"))
    print(f"{k[0]:<14} {k[1]:<10} n={len(ms):>3} p50={q(ms,.5):>6} p90={q(ms,.9):>6} max={max(ms):>6} sibling_busy={sib}/{len(rs)}")
# paired top-3 agreement between conditions of the same label
by = defaultdict(dict)
for r in rows:
    if r["label"].startswith(pre):
        by[(r["label"], r["i"])][r.get("cond")] = tuple(r["top3"])
agree = defaultdict(lambda: [0, 0])
for (lab, i), d in by.items():
    cs = sorted(d)
    for a in range(len(cs)):
        for b in range(a + 1, len(cs)):
            agree[(lab, cs[a], cs[b])][1] += 1
            agree[(lab, cs[a], cs[b])][0] += d[cs[a]] == d[cs[b]]
for k, (s, n) in agree.items():
    print(f"top3 identical {k}: {s}/{n}")
