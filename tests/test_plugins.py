import io
import json

import jiwer
import pytest
import respx
from httpx import Response

from mizan.plugins import registry as plugins
from mizan.plugins.base import AdapterError, AudioInput, MetricResult, ReportModel
from mizan.plugins.metrics.core import METRIC_PRESETS, bootstrap_ci
from mizan.plugins.textutil import duration_bucket, infer_spans, lang_mix
from tests.helpers import make_audio, utterances, wav_bytes

plugins.load_plugins("plugins.yaml")
norm = plugins.normalizers.get("arabic_v1")()
M = {k: plugins.metrics.get(k)() for k in plugins.metrics.keys()}


# ------------------------------------------------------------ normalizer (Appendix C.2, verbatim)
@pytest.mark.parametrize("raw,expected", [
    ("أنا عندي Meeting بُكرة.", "انا عندي meeting بكره"),
    ("الـــتقرير جاهز؟", "التقرير جاهز"),
    ("٣ ملفات", "3 ملفات"),
    ("We'll deploy، إن شاء الله", "we'll deploy ان شاء الله"),
])
def test_arabic_v1_fixtures(raw, expected):
    assert norm.normalize(raw) == expected


def test_arabic_v1_details():
    assert norm.normalize("۱۲۳ ٤٥٦") == "123 456"                      # Persian + Arabic-Indic digits
    assert norm.normalize("مؤتمر وئام") == "موتمر ويام"                   # rule 4 hamza folding on
    off = plugins.normalizers.get("arabic_v1")({"hamza_on_waw_yaa": False})
    assert off.normalize("مؤتمر وئام") == "مؤتمر وئام"                    # configurable
    assert norm.normalize("don't 'quoted' e-mail") == "don't quoted e mail"  # intra-token apostrophes only
    assert norm.normalize("  a \n\t b  ") == "a b"
    assert norm.normalize("") == ""
    assert norm.normalize("اللّه") == "الله"      # shadda stripped
    assert norm.normalize(norm.normalize("أنا Meeting")) == norm.normalize("أنا Meeting")  # idempotent
    assert any("tatweel" in r for r in norm.rules()) and norm.version == "1.0.0"


def test_whisper_basic_and_none():
    assert plugins.normalizers.get("whisper_basic")().normalize("Hello, [MUSIC] World!") == "hello world"
    assert plugins.normalizers.get("none")().normalize("  A  b ") == "A b"


# -------------------------------------------------------------------------- metrics vs jiwer
CASES = [
    ("the cat sat on the mat", "the cat sit on mat today"),
    ("a b c d e f", "a b c d e f"),
    ("a b c", ""),
    ("one two three four", "four three two one"),
    ("انا عندي meeting بكره", "انا عندي ميتنج بكره"),
]


@pytest.mark.parametrize("ref,hyp", CASES)
def test_wer_cer_mer_match_jiwer(ref, hyp):
    w = M["wer"].score(ref, hyp)
    assert w.value == pytest.approx(jiwer.wer(ref, hyp) if hyp else 1.0)
    assert w.details["S"] + w.details["D"] + w.details["I"] + w.details["C"] >= 0
    if hyp:
        assert M["mer"].score(ref, hyp).value == pytest.approx(jiwer.mer(ref, hyp))
        assert M["cer"].score(ref, hyp).value == pytest.approx(jiwer.cer(ref, hyp))


def test_cer_counts_spaces():
    r = M["cer"].score("a b", "ab")
    assert r.details["D"] == 1 and r.value == pytest.approx(1 / 3)


def test_empty_reference_handling():
    r = M["wer"].score("", "hello world")
    assert r.details == {"S": 0, "D": 0, "I": 2, "C": 0, "N": 0} and r.value == 1.0
    assert M["wer"].score("", "").value == 0.0


def test_corpus_level_not_mean_of_means():
    a = M["wer"].score("a b c d e f g h i j", "a b c d e f g h i x")   # 1/10
    b = M["wer"].score("a", "x")                                        # 1/1
    assert M["wer"].aggregate([a, b]) == pytest.approx(2 / 11)
    assert (a.value + b.value) / 2 != pytest.approx(2 / 11)


def test_script_split_attribution_adds_up():
    ref, hyp = "انا عندي meeting مهم بكره", "انا عند meting مهم بكره جدا"
    tot = M["wer"].score(ref, hyp)
    ar, en = M["wer_ar"].score(ref, hyp), M["wer_en"].score(ref, hyp)
    errs = lambda r: r.details["S"] + r.details["D"] + r.details["I"]  # noqa: E731
    assert errs(ar) + errs(en) == errs(tot)
    assert ar.details["N"] == 4 and en.details["N"] == 1
    assert en.value == 1.0  # meeting -> meting
    assert ar.details["I"] == 1  # inserted Arabic token attributed by hypothesis script


def test_script_metric_not_applicable_when_script_absent():
    r = M["wer_en"].score("انا هنا", "انا هنا")
    assert not r.applicable
    agg = M["wer_en"].aggregate([r, M["wer_en"].score("a b", "a c")])
    assert agg == 0.5


def test_en_preservation_fuzzy():
    r = M["en_preservation"].score("deploy the build now", "deploy the bild nau")
    assert r.details == {"kept": 3, "total": 4}                        # "bild"~"build" ok (>=0.8); "nau"!~"now"
    assert M["en_preservation"].score("deploy", "نشر").details["kept"] == 0
    assert not M["en_preservation"].score("انا هنا", "انا هنا").applicable


def test_hallucination_rate():
    h = M["hallucination_rate"]
    assert h.score(None, "thanks for watching", None, None, ["no_speech"]).details == {"bad": 1, "n": 1}
    assert h.score(None, "ok", None, None, ["music"]).details == {"bad": 0, "n": 1}
    assert not h.score(None, "lots of words here", None, None, []).applicable
    assert h.aggregate([h.score(None, "a b c", None, None, ["no_speech"]), h.score(None, "", None, None, ["no_speech"])]) == 0.5


def test_insertion_rate_catches_loops():
    r = M["insertion_rate"].score("a b", "a b b b b b b")
    assert r.value == pytest.approx(5 / 2)


def test_latency_and_rtfx():
    L = M["latency_ms"]
    rs = [L.score(None, "", None, None, None, {"latency_ms": v}) for v in (100, 200, 300, 400, 1000)]
    agg = L.aggregate_all(rs)
    assert agg["latency_ms"] == 400 and agg["latency_ms_p50"] == 300 and agg["latency_ms_p95"] == pytest.approx(880)
    X = M["rtfx"]
    rs = [X.score(None, "", None, None, None, {"latency_ms": 500, "audio_s": 10}), X.score(None, "", None, None, None, {"latency_ms": 1500, "audio_s": 10})]
    assert X.aggregate(rs) == pytest.approx(10.0)
    assert not L.score(None, "", None, None, None, {}).applicable


def test_bootstrap_ci_deterministic_and_brackets():
    rs = [M["wer"].score("a b c d", "a b c x" if i % 3 else "a b c d") for i in range(60)]
    a, b = bootstrap_ci(M["wer"], rs, 300, seed=7), bootstrap_ci(M["wer"], rs, 300, seed=7)
    assert a == b and a[0] <= M["wer"].aggregate(rs) <= a[1]
    assert bootstrap_ci(M["wer"], rs[:1]) is None


def test_metric_preset():
    assert METRIC_PRESETS["arabic_codeswitch_default"] == ["wer", "cer", "mer", "wer_ar", "wer_en", "en_preservation", "hallucination_rate", "latency_ms"]


# ------------------------------------------------------------------------------- text util
def test_spans_and_lang_mix():
    sp = infer_spans("انا عندي meeting مهم")
    assert [s["lang"] for s in sp] == ["ar", "en", "ar"]
    assert lang_mix("انا عندي meeting") == "mixed" and lang_mix("hello") == "en" and lang_mix("مرحبا") == "ar"
    assert [duration_bucket(x) for x in (2, 5, 14.9, 15, 30, 31)] == ["<5s", "5-15s", "5-15s", "15-30s", "15-30s", ">30s"]


# ----------------------------------------------------------------------------- segmenters
def test_energy_vad_finds_utterances():
    wav = utterances(4, speech=4.0, gap=1.5)
    segs = plugins.segmenters.get("energy_vad")().segment(wav, len(wav) / 32000, {})
    assert len(segs) == 4
    for (s, e), i in zip(segs, range(4)):
        true_start = 1.5 + i * 5.5
        assert abs(s - (true_start - 0.25)) < 0.5 and abs(e - (true_start + 4.0 + 0.25)) < 0.5


def test_vad_force_splits_long_speech_with_overlap_and_max_len():
    import numpy as np
    t = np.arange(40 * 16000) / 16000
    steady = (0.3 * np.sin(2 * np.pi * 180 * t) + np.random.default_rng(1).normal(0, 0.003, len(t))).astype("float32")  # no pauses
    segs = plugins.segmenters.get("energy_vad")().segment(wav_bytes(steady), 40.0, {"max_seg_s": 15})
    assert all(e - s <= 15.001 for s, e in segs) and len(segs) >= 3
    assert all(segs[i + 1][0] < segs[i][1] for i in range(len(segs) - 1))      # hard cuts inside speech overlap by 1 s
    assert segs[0][0] < 1 and segs[-1][1] > 39
    # with natural pauses (envelope dips) it prefers cutting at quiet points: no overlap needed
    natural = plugins.segmenters.get("energy_vad")().segment(wav_bytes(make_audio([("speech", 40.0)])), 40.0, {"max_seg_s": 15})
    assert all(natural[i + 1][0] >= natural[i][1] for i in range(len(natural) - 1))


def test_fixed_window_and_manual():
    fw = plugins.segmenters.get("fixed_window")().segment(b"", 25.0, {"window_s": 10, "overlap_s": 1})
    assert fw == [(0.0, 10.0), (9.0, 19.0), (18.0, 25.0)]
    with pytest.raises(ValueError):
        plugins.segmenters.get("fixed_window")().segment(b"", 25.0, {"window_s": 5, "overlap_s": 5})
    assert plugins.segmenters.get("manual_import")().segment(b"", 10, {"segments": [[5, 99], [0, 2]]}) == [(0.0, 2.0), (5.0, 10.0)]


# ------------------------------------------------------------------------------ adapters
@respx.mock
async def test_vllm_adapter_success_and_errors():
    cls = plugins.adapters.get("vllm_openai")
    a = cls({"base_url": "http://v:8000", "model": "IFM/Jais-ASR", "max_concurrency": 48})
    route = respx.post("http://v:8000/v1/audio/transcriptions").mock(return_value=Response(200, json={"text": "مرحبا hello"}))
    r = await a.transcribe(AudioInput(wav_bytes(make_audio([("speech", 1)])), 1.0, "s1", "ar"), {})
    assert r.text == "مرحبا hello" and r.latency_ms >= 0 and not r.no_speech
    assert b'name="language"' in route.calls[0].request.content and b"IFM/Jais-ASR" in route.calls[0].request.content
    route.mock(return_value=Response(200, json={"text": "[IFM-NoTranscript]"}))
    assert (await a.transcribe(AudioInput(b"x", 1, "s"), {})).no_speech
    for code, retry in ((503, True), (429, True), (400, False), (404, False)):
        route.mock(return_value=Response(code, text="x"))
        with pytest.raises(AdapterError) as ei:
            await a.transcribe(AudioInput(b"x", 1, "s"), {})
        assert ei.value.retryable is retry and ei.value.error_class == f"http_{code}"
    import httpx
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AdapterError) as ei:
        await a.transcribe(AudioInput(b"x", 1, "s"), {})
    assert ei.value.error_class == "timeout" and ei.value.retryable
    caps = a.capabilities()
    assert caps.concurrency_hint == 48 and not caps.external
    respx.get("http://v:8000/v1/models").mock(return_value=Response(200, json={"data": [{"id": "IFM/Jais-ASR"}]}))
    assert (await a.health())["ok"] and await a.list_models() == ["IFM/Jais-ASR"]
    await a.aclose()


@respx.mock
async def test_adapter_health_reports_failure_instead_of_raising():
    a = plugins.adapters.get("http_openai_compat")({"base_url": "http://down:1", "model": "m"})
    respx.get("http://down:1/v1/models").mock(side_effect=__import__("httpx").ConnectError("refused"))
    h = await a.health()
    assert h["ok"] is False and "ConnectError" in h["detail"]


def test_external_adapters_flagged():
    for kind in ("fanar_api", "openai_api", "aws_transcribe"):
        cfg = {"base_url": "https://x", "model": "m", "api_key": "k", "bucket": "b", "region": "r"}
        assert plugins.adapters.get(kind)(cfg).capabilities().external is True
    for kind in ("vllm_openai", "http_openai_compat", "faster_whisper_http", "mock"):
        assert plugins.adapters.get(kind)({"base_url": "http://x", "model": "m"}).capabilities().external is False


async def test_aws_transcribe_adapter_with_fake_clients(monkeypatch):
    import mizan.plugins.adapters.aws_transcribe as aws

    class S3:
        def put_object(self, **k): self.k = k
        def delete_object(self, **k): self.deleted = True

    class TR:
        n = 0
        def start_transcription_job(self, **k): self.job = k
        def get_transcription_job(self, TranscriptionJobName):
            self.n += 1
            return {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED" if self.n > 1 else "IN_PROGRESS",
                                         "Transcript": {"TranscriptFileUri": "http://t/out.json"}}}
        def list_transcription_jobs(self, **k): return {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(aws.urllib.request, "urlopen", lambda uri: Resp(json.dumps({"results": {"transcripts": [{"transcript": "مرحبا"}]}}).encode()))
    s3, tr = S3(), TR()
    a = aws.AWSTranscribeAdapter({"bucket": "b", "region": "r", "model": "aws", "poll_s": 0.01}, client_factory=lambda: (s3, tr))
    res = await a.transcribe(AudioInput(b"wav", 1, "s", "ar"), {})
    assert res.text == "مرحبا" and tr.job["LanguageCode"] == "ar-AE" and s3.deleted
    assert (await a.health())["ok"]


async def test_mock_adapter_scripted_and_errors():
    a = plugins.adapters.get("mock")({"model": "m", "transcripts": {"s1": "hello"}, "latency_ms": 1, "error_rate": 0})
    assert (await a.transcribe(AudioInput(b"", 1, "s1"), {})).text == "hello"
    b = plugins.adapters.get("mock")({"model": "m", "latency_ms": 1, "error_rate": 1})
    with pytest.raises(AdapterError):
        await b.transcribe(AudioInput(b"", 1, "x"), {})


# -------------------------------------------------------------------------------- exporters
def _model():
    return ReportModel(title="T", question="Q?", sections=[
        {"id": "a", "title": "Accuracy", "blocks": [
            {"type": "paragraph", "text": "hello مرحبا"},
            {"type": "table", "title": "t", "columns": ["Endpoint", "WER"], "rows": [["A|B", 0.12345], ["B", None]]},
            {"type": "chart", "kind": "bar", "title": "c", "x_label": "e", "y_label": "w", "categories": ["A", "B"],
             "series": [{"name": "WER", "values": [0.1, 0.2]}]},
            {"type": "chart", "kind": "line", "title": "l", "x_label": "c", "y_label": "ms", "series": [{"name": "A", "points": [[1, 10], [2, 30]]}]},
            {"type": "code", "text": "mizan run"}, {"type": "bullets", "items": ["x"]}, {"type": "heading", "text": "H"}]}],
        meta={"header": ["**Gold set:** g v1"]})


def test_exporters():
    for kind, needle in (("markdown", "| Endpoint | WER |"), ("html", "<svg"), ("json", '"sections"'), ("csv", "# 1. Accuracy / t")):
        buf = io.BytesIO()
        mime = plugins.exporters.get(kind)().export(_model(), buf)
        text = buf.getvalue().decode("utf-8-sig")
        assert needle in text and mime
    md = io.BytesIO()
    plugins.exporters.get("markdown")().export(_model(), md)
    assert "## 1. Accuracy" in md.getvalue().decode() and "A\\|B" in md.getvalue().decode()
    j = io.BytesIO()
    plugins.exporters.get("jsonl_dataset")().export(ReportModel(title="x", meta={"records": [{"b": 1, "a": "ع"}]}), j)
    assert j.getvalue().decode() == '{"a": "ع", "b": 1}\n'


def test_plugin_registry_load_from_yaml(tmp_path):
    (tmp_path / "myplug.py").write_text(
        "from mizan.plugins.base import Normalizer\n"
        "class Upper(Normalizer):\n    kind='upper'\n    version='9'\n"
        "    def normalize(self,t): return t.upper()\n    def rules(self): return ['upper']\n")
    import sys
    sys.path.insert(0, str(tmp_path))
    (tmp_path / "plugins.yaml").write_text("normalizers:\n  - module: myplug:Upper\n")
    plugins.load_plugins(tmp_path / "plugins.yaml", force=True)
    assert plugins.normalizers.get("upper")().normalize("a") == "A"
    with pytest.raises(KeyError):
        plugins.normalizers.get("nope")
