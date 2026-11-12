import json, sys, numpy as np
sys.path.insert(0, "/home/user/mezan")
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mizan.plugins import registry as P
P.load_plugins("/home/user/mezan/plugins.yaml")
M = {k: P.metrics.get(k)() for k in P.metrics.keys()}
N = P.normalizers.get("arabic_v1")()
OUT = "/tmp/claude-0/-home-user-mezan/b24793f8-ccbc-5abe-a602-0ce5f945e08a/scratchpad/rep/"
plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 150})
res = {}

# ---------- 1. worked example (real Mizan metric code)
pairs = [
 ("أنا عندي Meeting بُكرة", "انا عندي ميتنج بكره"),
 ("we will deploy الليلة", "we will deploy الليله"),
 ("السلام عليكم", "السلام"),
 ("send the report ارسل التقرير", "send report ارسل التقرير اليوم"),
 ("ممتاز", "ممتاز ممتاز ممتاز ممتاز ممتاز ممتاز"),
]
rows = []
for r, h in pairs:
    rn, hn = N.normalize(r), N.normalize(h)
    w = M["wer"].score(rn, hn); c = M["cer"].score(rn, hn); m = M["mer"].score(rn, hn)
    raw = M["wer"].score(r, h)
    rows.append(dict(ref=r, hyp=h, ref_n=rn, hyp_n=hn, S=w.details["S"], D=w.details["D"], I=w.details["I"], N=w.details["N"],
                     wer=w.value, cer=c.value, mer=m.value, wer_raw=raw.value))
agg = lambda k: M[k].aggregate([M[k].score(N.normalize(r), N.normalize(h)) for r, h in pairs])
res["example_rows"] = rows
res["example_corpus"] = {k: agg(k) for k in ("wer", "cer", "mer", "wer_ar", "wer_en", "insertion_rate")}
res["example_mean_of_means_wer"] = float(np.mean([x["wer"] for x in rows]))
res["example_raw_corpus_wer"] = M["wer"].aggregate([M["wer"].score(r, h) for r, h in pairs])
res["example_nonrep_corpus_wer"] = M["wer"].aggregate([M["wer"].score(N.normalize(r), N.normalize(h)) for r, h in pairs[:4]])

# ---------- 2. power simulation: paired vs unpaired bootstrap
rng = np.random.default_rng(42)
def sim_trial(n, delta, base=0.15, boots=300):
    words = 1 + rng.poisson(11, n)
    # shared per-segment difficulty (heavy tailed): same audio is hard for both systems
    h = rng.beta(0.6, 0.6 * (1 - base) / base, n)
    pa = np.clip(h, 0, 1); pb = np.clip(h + delta, 0, 1)
    ea, eb = rng.binomial(words, pa), rng.binomial(words, pb)
    idx = rng.integers(0, n, (boots, n))
    wa = ea[idx].sum(1) / words[idx].sum(1); wb = eb[idx].sum(1) / words[idx].sum(1)
    d_paired = (eb[idx].sum(1) - ea[idx].sum(1)) / words[idx].sum(1)          # same resample for both systems
    idx2 = rng.integers(0, n, (boots, n))
    wb2 = eb[idx2].sum(1) / words[idx2].sum(1)
    d_unpaired = wb2 - wa                                                      # independent resamples
    return np.percentile(d_paired, 2.5) > 0, np.percentile(d_unpaired, 2.5) > 0, np.percentile(wa, [2.5, 97.5]), ea.sum() / words.sum()
sizes = [50, 100, 200, 400, 800, 1600]
power = {}
for delta in (0.01, 0.02, 0.04):
    pp, pu = [], []
    for n in sizes:
        t = [sim_trial(n, delta) for _ in range(150)]
        pp.append(float(np.mean([x[0] for x in t]))); pu.append(float(np.mean([x[1] for x in t])))
    power[delta] = (pp, pu)
res["power"] = {str(k): v for k, v in power.items()}
hw = []
for n in sizes:
    t = [sim_trial(n, 0.0) for _ in range(60)]
    hw.append(float(np.mean([(x[2][1] - x[2][0]) / 2 for x in t])))
res["ci_halfwidth"] = hw
fig, ax = plt.subplots(1, 2, figsize=(7.2, 2.9))
for d, c in zip((0.01, 0.02, 0.04), ("#4e79a7", "#f28e2b", "#59a14f")):
    ax[0].plot(sizes, power[d][0], "-o", color=c, label=f"paired, Δ={d*100:.0f} pp"); ax[0].plot(sizes, power[d][1], "--", color=c, alpha=.7, label=f"unpaired, Δ={d*100:.0f} pp")
ax[0].set_xscale("log"); ax[0].set_xlabel("segments in test set"); ax[0].set_ylabel("power (95% CI excludes 0)"); ax[0].set_ylim(0, 1.02); ax[0].legend(fontsize=6.5, frameon=False)
ax[0].set_title("Detecting a real WER difference (synthetic model)", fontsize=8.5)
ax[1].plot(sizes, [h * 100 for h in hw], "-o", color="#e15759"); ax[1].set_xscale("log"); ax[1].set_xlabel("segments in test set"); ax[1].set_ylabel("95% CI half-width of one WER (pp)")
ax[1].set_title("Precision of a single WER (base ≈ 15%)", fontsize=8.5)
plt.tight_layout(); plt.savefig(OUT + "fig/power.png"); plt.close()

# ---------- 3. coordinated omission
def co_demo():
    svc, dur, stall_at, stall, rate = 0.02, 30.0, 10.0, 2.0, 20.0     # 20 ms service, 20 req/s offered (40% utilisation), one 2 s stall at t=10
    arr = np.arange(0, dur, 1 / rate); free = 0.0; lat_open = []; stalled = False
    for t in arr:                                                       # open loop: requests are *scheduled*; latency counted from the intended send time
        start = max(t, free); s = svc
        if not stalled and t >= stall_at:
            s += stall; stalled = True
        free = start + s; lat_open.append(free - t)
    t = 0.0; lat_closed = []; stalled = False                           # closed loop: next request is sent only after the previous one returns
    while t < dur:
        s = svc
        if not stalled and t >= stall_at:
            s += stall; stalled = True
        lat_closed.append(s); t += s
    return np.array(lat_open) * 1000, np.array(lat_closed) * 1000
lo, lc = co_demo()
pc = lambda a: {q: float(np.percentile(a, q)) for q in (50, 99, 99.9)} | {"n": int(len(a)), "max": float(a.max())}
res["co"] = {"open": pc(lo), "closed": pc(lc)}
fig, ax = plt.subplots(figsize=(6.2, 2.5))
ax.plot(np.arange(len(lo)) / 20, lo, color="#e15759", label=f"open-loop (intended send times), n={len(lo)}")
ax.plot(np.cumsum(lc) / 1000 - lc / 1000, lc, "o", ms=2.0, color="#4e79a7", label=f"closed-loop (1 client), n={len(lc)}")
ax.set_xlabel("test time (s)"); ax.set_ylabel("recorded latency (ms)"); ax.legend(frameon=False, fontsize=7.5); ax.set_title("One 2 s stall, two ways of measuring it (simulation)", fontsize=8.5)
plt.tight_layout(); plt.savefig(OUT + "fig/co.png"); plt.close()

# ---------- 4. Little's law / knee (idealised)
c = np.arange(1, 129); S0 = 0.40; cap = 24.0       # 0.40 s unloaded service time, 24 req/s capacity
thr = np.minimum(c / S0, cap) ; lat = c / thr
fig, ax = plt.subplots(1, 2, figsize=(7.2, 2.6))
ax[0].plot(c, thr, color="#4e79a7"); ax[0].set_xlabel("concurrency (in flight)"); ax[0].set_ylabel("throughput (req/s)"); ax[0].axvline(cap * S0, ls=":", color="gray"); ax[0].text(cap * S0 + 2, 3, "knee ≈ C·S₀", fontsize=7)
ax[1].plot(c, lat * 1000, color="#e15759"); ax[1].set_xlabel("concurrency (in flight)"); ax[1].set_ylabel("mean latency (ms)  = N / X"); ax[1].axvline(cap * S0, ls=":", color="gray")
fig.suptitle("Closed-loop saturation (idealised): below the knee latency is flat; beyond it, extra concurrency only adds queueing", fontsize=7.5)
plt.tight_layout(); plt.savefig(OUT + "fig/knee.png"); plt.close()
json.dump(res, open(OUT + "numbers.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps({k: res[k] for k in ("example_corpus", "example_mean_of_means_wer", "example_raw_corpus_wer", "example_nonrep_corpus_wer", "co", "ci_halfwidth")}, ensure_ascii=False, indent=1))
print(json.dumps(res["power"]))
for r in rows: print(r["ref_n"], "|", r["hyp_n"], r["S"], r["D"], r["I"], r["N"], round(r["wer"],3), round(r["cer"],3), round(r["mer"],3), round(r["wer_raw"],3))
