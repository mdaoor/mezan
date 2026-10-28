"""Seed a demo campaign end-to-end against a running Mizan (no GPU, no real models: mock engines + synthetic audio).

    python contrib/seed_demo.py --url http://localhost:8080 --email admin@mizan.local --password admin
Prints the report URL and writes demo-report.md.
"""
import argparse
import io
import sys
import time
import wave

import httpx
import numpy as np

TRUTH = ["مرحبا بكم في الاجتماع", "نبدأ بمراجعة the quarterly report", "الميزانية مناسبة جدا", "please send the deck قبل الغداء",
         "هل وصلتك الرسالة", "we need more GPU memory اليوم", "شكرا جزيلا", "الخادم متوقف عن العمل", "restart the service من فضلك",
         "انتهى الاجتماع", "سنراجع الخطة غدا", "the deployment finished بنجاح"]


def synth(n: int, sr=16000) -> bytes:
    rng, parts = np.random.default_rng(1), []
    for i in range(n):
        parts.append(rng.normal(0, .002, int(1.3 * sr)))
        t = np.arange(int(4 * sr)) / sr
        parts.append((np.sin(2 * np.pi * (170 + 15 * i) * t) + .5 * np.sin(2 * np.pi * 700 * t)) * (.5 + .5 * np.sin(2 * np.pi * 3 * t)) * .3)
    parts.append(rng.normal(0, .002, int(1.3 * sr)))
    pcm = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype("<i2")
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr), w.writeframes(pcm.tobytes())
    return b.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--email", default="admin@mizan.local")
    ap.add_argument("--password", default="admin")
    a = ap.parse_args()
    h = httpx.Client(base_url=a.url.rstrip("/") + "/api/v1", timeout=120)
    tok = h.post("/auth/login", json={"email": a.email, "password": a.password}).raise_for_status().json()["access_token"]
    h.headers["Authorization"] = f"Bearer {tok}"

    def call(m, p, **kw):
        r = h.request(m, p, **kw)
        if r.status_code >= 400:
            sys.exit(f"{m} {p}: {r.status_code} {r.text[:300]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    def wait(kind, id_):
        while True:
            d = call("GET", f"/{kind}/{id_}")
            if d["status"] in ("succeeded", "failed", "cancelled", "ready"):
                return d
            time.sleep(0.5)

    p = call("POST", "/projects", json={"name": f"Demo {int(time.time())}"})
    d = call("POST", f"/projects/{p['id']}/datasets", json={"name": "demo-calls", "language_hints": ["ar", "en"]})
    up = call("POST", f"/datasets/{d['id']}/audio?segmenter=energy_vad", files=[("files", ("demo.wav", synth(len(TRUTH)), "audio/wav"))])
    wait("jobs", up["job_id"])
    segs = []
    for _ in range(60):
        segs = call("GET", f"/datasets/{d['id']}/segments")["items"]
        if len(segs) >= len(TRUTH):
            break
        time.sleep(0.5)
    segs = segs[:len(TRUTH)]
    good = {s["id"]: t for s, t in zip(segs, TRUTH)}
    weak = {s["id"]: " ".join(t.split()[:-1]) or t for s, t in zip(segs, TRUTH)}
    mk = lambda name, tr, lat, **c: call("POST", "/model-endpoints", json={"engine_kind": "mock", "model_id": name, "display_name": name,  # noqa: E731
                                                                          "config": {"transcripts": tr, "latency_ms": lat, "max_concurrency": 16, **c}})
    A, B = mk("Demo Model A", good, 40, hardware="demo GPU 0"), mk("Demo Model B", weak, 15, saturate_at=2)
    wait("runs", call("POST", f"/datasets/{d['id']}/bootstrap", json={"model_endpoint_id": B["id"]})["run_id"])
    # team
    for n, r in (("Demo Annotator", "annotator"), ("Demo Reviewer", "reviewer")):
        call("POST", "/users", json={"email": n.lower().replace(" ", ".") + "@example.com", "name": n, "role": r, "password": "demo-pass-1"})
    t = {r: {"Authorization": "Bearer " + call("POST", "/auth/login", json={"email": f"demo.{r}@example.com", "password": "demo-pass-1"})["access_token"]}
         for r in ("annotator", "reviewer")}
    call("POST", f"/datasets/{d['id']}/claims", headers=t["annotator"], json={"minutes": 30})
    for s in segs[:10]:
        call("PUT", f"/segments/{s['id']}/annotation", headers=t["annotator"], json={"text": good[s["id"]]})
        call("POST", f"/segments/{s['id']}/annotation/submit", headers=t["annotator"])
        call("POST", f"/segments/{s['id']}/annotation/review", headers=t["reviewer"], json={"action": "approve"})
    gold = call("POST", f"/datasets/{d['id']}/gold-sets", json={"name": "demo-gold", "policy": {"exclude_flags": []}})
    runs = call("POST", "/runs/batch", json={"model_endpoint_ids": [A["id"], B["id"]], "run": {
        "type": "evaluation", "model_endpoint_id": A["id"], "reference": {"type": "gold_set", "id": gold["id"]}}})["runs"]
    for r in runs:
        wait("runs", r["run_id"])
    sc = call("POST", "/load-scenarios", json={"name": f"demo-ramp-{int(time.time())}", "concurrency_steps": [1, 2, 4, 8], "step_duration_s": 3,
                                               "warmup_s": 1, "timeout_s": 10, "length_buckets": {"<5s": 0.5, "5-15s": 0.5}})
    lids = [call("POST", "/load-runs", json={"model_endpoint_id": e["id"], "scenario_id": sc["id"], "dataset_id": d["id"]})["load_run_id"] for e in (A, B)]
    for l in lids:
        wait("load-runs", l)
    cp = call("POST", "/campaigns", json={"project_id": p["id"], "name": "Demo: which model?", "question": "Which endpoint should we ship?",
                                          "run_ids": [r["run_id"] for r in runs], "load_run_ids": lids})
    rep = call("POST", f"/campaigns/{cp['id']}/reports", json={})
    wait("reports", rep["report_id"])
    md = call("GET", f"/reports/{rep['report_id']}/export", params={"format": "md"}).text
    open("demo-report.md", "w", encoding="utf-8").write(md)
    print(f"report: {a.url}/#/campaigns/{cp['id']}   (markdown written to demo-report.md)")


if __name__ == "__main__":
    main()
