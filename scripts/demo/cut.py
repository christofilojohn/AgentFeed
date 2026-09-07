"""Cut the take: time-skip the model waits, punch in on the small text, add cards."""
import json, os, subprocess, sys
take = sys.argv[1]; outdir = sys.argv[2]
m = json.load(open(os.path.join(take, "marks.json")))
SRC = os.path.join(take, "take.webm")
DSF = m["dsf"]; FW, FH = m["w"] * DSF, m["h"] * DSF
OW, OH, FPS = FW, FH, 30
LEAD, KEEP = 1.4, 1.3            # trim before first frame; seconds of real waiting shown
FAST_TO = 1.0                     # the skipped stretch is compressed to this long
CAPTION = {"building the topic": "local model building the topic — terms, search, judgement",
           "German abstract": "local model writing the abstract in German",
           "Greek abstract": "local model writing the abstract in Greek",
           "collection answer": "local model answering from 10 abstracts and your notes"}
FONT = "/System/Library/Fonts/Helvetica.ttc"
os.makedirs(outdir, exist_ok=True)
parts = []

def enc(name, start, end, vf):
    out = os.path.join(outdir, f"{name}.mp4")
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", SRC,
           "-vf", vf, "-r", str(FPS), "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", out]
    subprocess.run(cmd, check=True); parts.append(out)

def card(name, png, secs):
    out = os.path.join(outdir, f"{name}.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-t", str(secs), "-i", png,
                    "-vf", f"scale={OW}:{OH},fade=t=in:st=0:d=0.4,fade=t=out:st={secs-0.4}:d=0.4",
                    "-r", str(FPS), "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", out],
                   check=True); parts.append(out)

NORMAL = f"scale={OW}:{OH}"
def zoom_vf(box, z=2.0):
    cx = (box["x"] + box["width"] / 2) * DSF; cy = (box["y"] + box["height"] / 2) * DSF
    cw, ch = FW / z, FH / z
    x = min(max(cx - cw / 2, 0), FW - cw); y = min(max(cy - ch / 2, 0), FH - ch)
    return f"crop={cw:.0f}:{ch:.0f}:{x:.0f}:{y:.0f},scale={OW}:{OH}"

events = sorted(m["marks"], key=lambda e: e["start"])
CARD = 2.6
def cut_time(t):
    c = CARD + (t - LEAD)
    for ev in events:
        if "box" in ev: continue
        s, e = ev["start"], ev["end"]
        if t >= e: c -= (e - s) - (KEEP + FAST_TO)
        elif t > s + KEEP: c -= (t - s - KEEP) * (1 - FAST_TO / max(e - s - KEEP, 0.01))
    return round(c, 2)
json.dump({b: cut_time(t) for b, t in m.get("beats", {}).items()},
          open(os.path.join(outdir, "timeline.json"), "w"), indent=1)
card("card_open", "card_open.png", CARD)
t = LEAD; n = 0
for ev in events:
    s, e = ev["start"], ev["end"]
    if s > t + 0.05: enc(f"p{n:02d}", t, s, NORMAL); n += 1
    if "box" in ev:
        enc(f"p{n:02d}_zoom", s, e, zoom_vf(ev["box"])); n += 1
    else:
        skipped = e - (s + KEEP)
        enc(f"p{n:02d}", s, s + KEEP, NORMAL); n += 1
        if skipped > 0.3:
            factor = skipped / FAST_TO
            text = f"»»  {skipped:.0f} s skipped  ·  {CAPTION.get(ev['label'], 'local model working')}"
            vf = (f"setpts=PTS/{factor:.3f},{NORMAL},drawtext=fontfile={FONT}:text='{text}':"
                  f"fontsize=34:fontcolor=white:box=1:boxcolor=black@0.62:boxborderw=18:"
                  f"x=(w-text_w)/2:y=70")
            enc(f"p{n:02d}_fast", s + KEEP, e, vf); n += 1
    t = e
enc(f"p{n:02d}", t, m["end"] + 0.6, NORMAL)
card("card_end", "card_end.png", 4.2)
lst = os.path.join(outdir, "list.txt")
open(lst, "w").write("".join(f"file '{os.path.abspath(p)}'\n" for p in parts))
final = os.path.join(outdir, "agentfeed-demo.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                "-c", "copy", "-movflags", "+faststart", final], check=True)
d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", final],
                   capture_output=True, text=True).stdout.strip()
print("final", final, "duration", d)
