import { createRequire } from 'node:module'
const require = createRequire(import.meta.url)
const { chromium } = require('playwright')
const dir = process.argv[2]
const b = await chromium.launch()
const p = await b.newPage()
await p.goto('file://' + dir + '/report.html')
await p.pdf({ path: dir + '/ASR_Evaluation_and_Engine_Swap_Report.pdf', format: 'A4', printBackground: true, outline: true, tagged: true,
  displayHeaderFooter: true, margin: { top: '20mm', bottom: '20mm', left: '17mm', right: '17mm' },
  headerTemplate: '<div style="font-size:7px;color:#888;width:100%;padding:0 17mm;font-family:sans-serif">Swapping ASR Models and Inference Engines with Confidence</div>',
  footerTemplate: '<div style="font-size:7px;color:#888;width:100%;padding:0 17mm;font-family:sans-serif;display:flex;justify-content:space-between"><span>Technical report v1.0 · 12 Nov 2026</span><span>Page <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>' })
await b.close()
