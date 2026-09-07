"""Drive the dashboard through the demo beats and record it.

Writes <out>/take.webm and <out>/marks.json: the wall-clock offsets (from
video start) of every model wait and punch-in, plus the start of each beat,
so the cut can time-skip the waits and the narration can find its place.
"""
import json, os, sys, time
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8770")
OUT = sys.argv[1]
ITEM = int(os.environ.get("ITEM", "31"))          # article to translate
TOPIC = int(os.environ.get("TOPIC", "9"))         # topic with a judged, no-keyword match
SUBJECT = os.environ.get("SUBJECT",
    "AI data center buildout: chips, power and infrastructure spending")
DO_DISMISS = os.environ.get("DISMISS", "1") == "1"
QUESTION = os.environ.get("QUESTION",
    "Which of these are extortion or ransomware cases, and who was affected?")
W, H = 1600, 1000
DSF = 1

CURSOR_JS = """
(() => {
  const c = document.createElement('div');
  c.id = '__cur';
  c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;'
    + 'pointer-events:none;transform:translate(-3px,-2px);transition:transform .05s;'
    + "background:url(\\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='22' height='22' viewBox='0 0 22 22'><path d='M3 2 L3 18 L7.5 13.8 L10.5 20 L13.2 18.8 L10.2 12.8 L16.5 12.5 Z' fill='white' stroke='black' stroke-width='1.4' stroke-linejoin='round'/></svg>\\") no-repeat";
  const add = () => document.body && document.body.appendChild(c);
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', add) : add();
  window.addEventListener('mousemove', (e) => {
    c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px';
  }, true);
  window.addEventListener('mousedown', () => { c.style.transform = 'translate(-3px,-2px) scale(.85)'; }, true);
  window.addEventListener('mouseup', () => { c.style.transform = 'translate(-3px,-2px)'; }, true);
})();
"""

marks, beats = [], {}
t0 = None
def now(): return round(time.time() - t0, 3)
def log(*a): print(f"[{now():7.2f}]", *a, flush=True)
def beat(name):
    beats[name] = now(); log("beat:", name)

def main():
    global t0
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H},
                                  device_scale_factor=DSF, color_scheme="dark",
                                  record_video_dir=OUT,
                                  record_video_size={"width": W * DSF, "height": H * DSF})
        ctx.add_init_script(CURSOR_JS)
        # "new" = fetched since the last visit; make that the tail of the last fetch
        ctx.add_init_script("try{localStorage.setItem('agentfeed.lastSeen','2026-09-03T21:31:53')}catch(e){}")
        page = ctx.new_page()
        t0 = time.time()
        page.on("dialog", lambda d: d.accept("Not my field") if d.type == "prompt" else d.accept())

        def glide(sel, dx=0, dy=0, steps=28):
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=15000)
            b = el.bounding_box()
            x, y = b["x"] + b["width"] / 2 + dx, b["y"] + b["height"] / 2 + dy
            page.mouse.move(x, y, steps=steps)
            return x, y

        def click(sel, dx=0, dy=0, settle=0.3):
            glide(sel, dx, dy)
            time.sleep(settle)
            page.mouse.down(); time.sleep(0.08); page.mouse.up()

        def hold(s): time.sleep(s)

        def wait_skip(label, fn, timeout=300):
            m = {"label": label, "start": now()}
            log("WAIT", label)
            fn(timeout)
            m["end"] = now(); marks.append(m)
            log("DONE", label, f"{m['end']-m['start']:.1f}s")

        def zoom(label, sel, dur, dx=0, dy=0):
            el = page.locator(sel).first
            b = el.bounding_box()
            glide(sel, dx, dy)
            m = {"label": label, "start": now(), "box": b}
            time.sleep(dur)
            m["end"] = now(); marks.append(m)
            log("ZOOM", label)

        def wait_abstract(timeout):
            # skeleton appears after 180 ms, then the paragraph replaces it
            page.locator(".abstract-skeleton").wait_for(state="visible", timeout=5000)
            page.locator("#abstract-slot p.abstract").wait_for(state="visible",
                                                                timeout=timeout * 1000)

        # ── 1. the feed ─────────────────────────────────────────────
        page.goto(BASE + "/", wait_until="networkidle")
        page.locator(".row").first.wait_for()
        beat("feed")
        page.mouse.move(700, 450, steps=10)
        hold(0.8)
        glide("#list", dy=-120)
        for _ in range(3):
            page.mouse.wheel(0, 260); hold(0.4)
        hold(0.3)
        for _ in range(3):
            page.mouse.wheel(0, -260); hold(0.3)
        hold(0.3)

        # ── 2. the stack ────────────────────────────────────────────
        beat("status")
        click("#btn-status")
        page.locator("#reader .kv").first.wait_for()
        hold(0.5)
        glide("#reader dt:has-text('Chat model')", dx=220)
        hold(1.8)

        # ── 3. follow a subject: the topic pipeline, live ───────────
        beat("topic_new")
        click("#btn-new-topic")
        page.locator("#tf-name").wait_for()
        hold(0.3)
        click("#tf-name")
        page.keyboard.type(SUBJECT, delay=22)
        hold(0.4)
        click("#tf-follow")
        wait_skip("building the topic",
                  lambda t: page.locator(".row .why").first.wait_for(timeout=t * 1000))
        hold(0.4)
        glide("#list-meta"); hold(1.6)
        glide(".topic-row:last-child .nav-count"); hold(1.4)

        # ── 4. a topic that is not a search box ─────────────────────
        beat("topic")
        click(f".topic-row[data-topic='{TOPIC}'] .nav-label")
        page.locator(f".row[data-id='{ITEM}'] .why").wait_for()
        hold(0.5)
        zoom("matched line", f".row[data-id='{ITEM}'] .why", 2.8, dx=-40)

        # ── 5/6/7. abstract, translated, kept ───────────────────────
        beat("article")
        click(f".row[data-id='{ITEM}'] .row-title")
        page.locator("#abstract-slot p.abstract").wait_for(timeout=120000)
        page.mouse.move(1000, 520, steps=15)
        hold(2.2)

        beat("german")
        glide("#reader-lang"); hold(0.3)
        page.select_option("#reader-lang", "de")
        wait_skip("German abstract", wait_abstract)
        page.mouse.move(1000, 560, steps=12)
        hold(2.2)

        beat("cached")
        glide("#reader-lang"); hold(0.3)
        page.select_option("#reader-lang", "en")
        page.locator("#abstract-slot .meta:has-text('cached')").wait_for(timeout=5000)
        hold(1.1)
        glide("#reader-lang"); hold(0.3)
        page.select_option("#reader-lang", "de")
        page.locator("#abstract-slot .meta:has-text('cached')").wait_for(timeout=5000)
        hold(0.4)
        zoom("cached", "#abstract-slot .abstract-head .meta", 2.6)

        # ── 8. the analysis that argues against itself ──────────────
        beat("analysis")
        click("#btn-signals")
        page.locator(".linkish[data-analysis]").first.wait_for()
        hold(0.6)
        click(".linkish[data-analysis]")
        page.locator(".stance").wait_for(timeout=10000)
        hold(1.0)
        zoom("call", ".stance-head", 2.2)
        glide(".stance-against", dx=-120); hold(2.0)
        page.mouse.wheel(0, 380); hold(0.3)
        page.mouse.wheel(0, 380); hold(1.2)

        # ── 9. keep one, drop one ───────────────────────────────────
        beat("keep")
        click(".nav-item[data-view='latest']")
        page.locator(".row").first.wait_for(); hold(0.5)
        free = page.locator("#list .row:has(.row-btn.star:not(.on))")
        star_id = free.nth(1).get_attribute("data-id")
        drop_id = free.nth(3).get_attribute("data-id")
        click(f".row[data-id='{star_id}'] [data-star]")
        hold(1.1)
        if DO_DISMISS:
            click(f".row[data-id='{drop_id}'] [data-dismiss]")
            page.locator("#toast:not(.hidden)").wait_for(timeout=10000)
            hold(1.6)

        # ── 10. ask a collection ────────────────────────────────────
        beat("ask")
        click(".coll-row[data-collection='1'] .nav-label")
        page.locator("#ask-q").wait_for(timeout=20000)
        hold(0.5)
        click("#ask-q")
        page.keyboard.type(QUESTION, delay=24)
        hold(0.4)
        click("[data-ask-go]")
        wait_skip("collection answer",
                  lambda t: page.locator(".answer-body").wait_for(timeout=t * 1000))
        beat("answer")
        page.mouse.move(1000, 500, steps=12)
        hold(1.8)
        page.mouse.wheel(0, 300); hold(1.8)

        # ── 11. the button ──────────────────────────────────────────
        beat("agents")
        click("#btn-agents")
        page.locator(".embed-snippet").wait_for()
        hold(0.7)
        glide(".embed-row img"); hold(1.4)
        glide(".embed-snippet", dy=-8); hold(2.0)

        end = now()
        hold(0.6)
        page.close()
        vid = page.video.path()
        ctx.close(); browser.close()
        os.replace(vid, os.path.join(OUT, "take.webm"))
        json.dump({"marks": marks, "beats": beats, "end": end, "star": star_id,
                   "drop": drop_id, "w": W, "h": H, "dsf": DSF},
                  open(os.path.join(OUT, "marks.json"), "w"), indent=1)
        print("saved", os.path.join(OUT, "take.webm"))

main()
