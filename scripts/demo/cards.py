import base64, pathlib
from playwright.sync_api import sync_playwright
logo = (pathlib.Path(__file__).resolve().parents[2] / "ui" / "logo-dark.svg").read_text()
logo64 = base64.b64encode(logo.encode()).decode()
CSS = """<style>body{margin:0;background:#12100e;color:#f3efe9;font-family:-apple-system,Inter,'Segoe UI',sans-serif;
display:grid;place-items:center;height:100vh;width:100vw}.c{text-align:center;max-width:900px}
img{width:520px}h1{font-size:34px;font-weight:600;line-height:1.35;margin:36px 0 0;letter-spacing:-.01em}
p{color:#b7b0a6;font-size:22px;margin:18px auto 0;line-height:1.5;max-width:820px}b{color:#ff8c1a;font-weight:600}
.k{display:flex;gap:28px;justify-content:center;margin-top:34px;color:#b7b0a6;font-size:19px}
.k span{border:1px solid #2e2a25;border-radius:8px;padding:8px 14px}</style>"""
open_html = CSS + f"""<div class=c><img src="data:image/svg+xml;base64,{logo64}">
<h1>A news reader and protocol, with a <b>local open model</b> behind it.</h1>
<p>It files articles into your topics by meaning, summarizes and translates them when you need it,
and lets you chat with a collection of articles for analysis.</p></div>"""
end_html = CSS + f"""<div class=c><img src="data:image/svg+xml;base64,{logo64}">
<h1>Open weights, on your own hardware.</h1>
<div class=k><span>Qwen3-30B-A3B</span><span>nomic-embed-text</span><span>Ollama · llama.cpp · vLLM · NVIDIA NIM</span></div>
<p style="margin-top:34px">protocol <b>agentfeed/0.1</b> — one link tag, and agents can subscribe to a site’s feed</p>
<p style="color:#7d766d;font-size:19px">github.com/christofilojohn/AgentFeed</p></div>"""
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width":1280,"height":800}, device_scale_factor=2)
    for name, html in (("open", open_html), ("end", end_html)):
        pg.set_content(html); pg.wait_for_timeout(300); pg.screenshot(path=f"card_{name}.png")
    b.close()
print("cards ok")
