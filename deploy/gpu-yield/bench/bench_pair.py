#!/usr/bin/env python3
"""t_0930e064: paired replay of the 50 saved rerank requests across several zerank instances.
usage: bench_pair.py <label> <out.jsonl> <port>:<cond> [<port>:<cond> ...]
For each request, every instance gets the same request back to back (order rotated per request), so
background load (live traffic, the sibling card's bench) lands on every condition alike.
Per call: wall ms, top-3, GPU0 util before, sibling-bench-active flag. Prints p50/p90 per condition."""
import http.client, json, os, subprocess, sys, time

label, out = sys.argv[1], sys.argv[2]
conds = [(int(p), c) for p, c in (a.split(":", 1) for a in sys.argv[3:])]  # port is an int: loopback only
reqs = json.load(open("/home/ace/t_0930e064/requests50.json"))
GPU0 = "GPU-31cbe110-66f1-da0c-dd4b-5cc60bcde854"


def gpu0_util():
    try:
        o = subprocess.run(["nvidia-smi", "-i", GPU0, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5).stdout
        return int(o.strip())
    except Exception:
        return None


def sibling_active():
    o = subprocess.run(["pgrep", "-u", "ace", "-f", "^[^ ]*python[^ ]* (bench_inproc|replay)[.]py"],
                       capture_output=True, text=True).stdout
    return sum(1 for l in o.splitlines() if l.strip())


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


GAP_S = float(os.environ.get("GAP_S", "0"))  # idle seconds between requests (duty-cycle runs)
res = {c: [] for _, c in conds}
with open(out, "a") as f:
    for i, r in enumerate(reqs):
        body = json.dumps({"query": r["query"], "documents": r["documents"]}).encode()
        if GAP_S and i:
            time.sleep(GAP_S)
        order = conds[i % len(conds):] + conds[:i % len(conds)]
        for port, cond in order:
            util, sib = gpu0_util(), sibling_active()
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)  # fixed host and scheme
            t = time.time()
            conn.request("POST", "/v1/rerank", body=body, headers={"content-type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                sys.exit(f"{cond} request {i}: HTTP {resp.status}")
            rr = json.loads(resp.read())
            conn.close()
            ms = (time.time() - t) * 1000
            res[cond].append(ms)
            f.write(json.dumps({"label": label, "cond": cond, "i": i, "id": r["id"], "t0": t, "ms": round(ms),
                                "top3": [x["index"] for x in rr["results"][:3]],
                                "s3": [round(x["relevance_score"], 4) for x in rr["results"][:3]],
                                "util_before": util, "sibling_units": sib}) + "\n")
            f.flush()
for _, c in conds:
    v = res[c]
    print(f"{label} {c}: n={len(v)} p50={q(v, .5):.0f} p90={q(v, .9):.0f} max={max(v):.0f}", flush=True)
