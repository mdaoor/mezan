import json, base64, html, pathlib
d = pathlib.Path(__file__).parent
n = json.load(open(d / "numbers.json"))
sizes = [50, 100, 200, 400, 800, 1600]
rows = "".join(
    f"<tr><td>{dl} pp</td><td>paired</td>" + "".join(f"<td>{v*100:.0f}%</td>" for v in n['power'][k][0]) + "</tr>"
    f"<tr><td></td><td>independent</td>" + "".join(f"<td>{v*100:.0f}%</td>" for v in n['power'][k][1]) + "</tr>"
    for k, dl in (("0.01", 1), ("0.02", 2), ("0.04", 4)))
power = ("<table><tr><th>True WER gap</th><th>Comparison</th>" + "".join(f"<th>{s} seg.</th>" for s in sizes) + "</tr>" + rows +
         "<caption>Table 1. Simulated power (150 trials per cell; baseline WER ≈ 15%; B = 300 bootstrap resamples). Synthetic data: treat as illustrative, not as a sizing guarantee.</caption></table>")
ex = n["example_rows"]
et = ("<table><tr><th>Reference (normalized)</th><th>Hypothesis (normalized)</th><th>S</th><th>D</th><th>I</th><th>N</th><th>WER</th><th>CER</th><th>MER</th><th>WER raw</th></tr>" +
      "".join(f"<tr><td><span class='ar'>{html.escape(r['ref_n'])}</span></td><td><span class='ar'>{html.escape(r['hyp_n'])}</span></td><td>{r['S']}</td><td>{r['D']}</td><td>{r['I']}</td><td>{r['N']}</td>"
              f"<td>{r['wer']*100:.0f}%</td><td>{r['cer']*100:.0f}%</td><td>{r['mer']*100:.0f}%</td><td>{r['wer_raw']*100:.0f}%</td></tr>" for r in ex) +
      "<tr><th colspan='2'>Corpus level (Σ errors / Σ N)</th><th>1</th><th>2</th><th>6</th><th>16</th><th>56.25%</th><th>60.9%</th><th>40.9%</th><th>75.0%</th></tr>"
      "<caption>Table 2. Five segment pairs scored with Mizan's normalizer (arabic_v1) and metric code. Corpus-level WER is 9/16; the arithmetic mean of the five segment WERs is 123%.</caption></table>")
body = "".join((d / f).read_text() for f in ("part1.html", "part2.html", "part3.html", "part4.html"))
body = body.replace("@@POWER_TABLE@@", power).replace("@@EXAMPLE_TABLE@@", et)
doc = f"<!doctype html><html lang='en'><meta charset='utf-8'><title>Swapping ASR Models and Inference Engines with Confidence</title><style>{(d/'style.css').read_text()}</style><body>{body}</body></html>"
(d / "report.html").write_text(doc, encoding="utf-8")
print("html", len(doc))
