"""Narration and subtitles for the cut.

One line per beat, synthesised offline (Kokoro-82M by default, or a system voice), placed at the
beat's start as the cut's timeline.json reports it. A line that would run
into the next one is hurried a little (never more than 15%), then delayed.
Writes out/voiceover.wav, out/agentfeed-demo.srt, out/agentfeed-demo-voiced.mp4
and out/script.md.
"""
import json, os, subprocess, sys
CUT, OUT = sys.argv[1], sys.argv[2]           # cut dir (video + timeline), output dir
VOICE = os.environ.get("VOICE", "am_michael")   # a Kokoro voice (has "_"), or a macOS `say` voice
RATE = os.environ.get("RATE", "170")            # `say` words per minute
SPEED = float(os.environ.get("SPEED", "1.0"))   # Kokoro pace
KOKORO_DIR = os.path.expanduser(os.environ.get("KOKORO_DIR", "~/Library/Caches/kokoro-onnx"))

# Kokoro-82M (Apache 2.0) through kokoro-onnx: open weights, runs on the CPU,
# far more natural than the system voices. Its phonemizer is espeak-ng; the
# wheel's bundled copy has a build-machine path baked in, so point it at a
# system espeak-ng when there is one (brew install espeak-ng).
_kokoro = None
def kokoro():
    global _kokoro
    if _kokoro is None:
        import espeakng_loader
        for lib, data in (("/opt/homebrew/lib/libespeak-ng.dylib", "/opt/homebrew/share/espeak-ng-data"),
                          ("/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1", "/usr/lib/x86_64-linux-gnu/espeak-ng-data")):
            if os.path.exists(lib) and os.path.exists(data):
                espeakng_loader.get_library_path = lambda lib=lib: lib
                espeakng_loader.get_data_path = lambda data=data: data
                os.environ["ESPEAK_DATA_PATH"] = data
                break
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(os.path.join(KOKORO_DIR, "kokoro-v1.0.onnx"),
                         os.path.join(KOKORO_DIR, "voices-v1.0.bin"))
    return _kokoro

def synth(text, wav):
    """Write `text` to `wav` (48 kHz mono) with whichever voice is configured."""
    if "_" in VOICE:
        import soundfile as sf
        lang = "en-gb" if VOICE.startswith("b") else "en-us"
        samples, sr = kokoro().create(text, voice=VOICE, speed=SPEED, lang=lang)
        raw = wav + ".raw.wav"; sf.write(raw, samples, sr)
    else:
        raw = wav + ".aiff"
        subprocess.run(["say", "-v", VOICE, "-r", RATE, "-o", raw, text], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, "-ar", "48000", "-ac", "1", wav], check=True)

LINES = [  # (beat, offset seconds after the beat starts, text)
  ("card",      0.5, "AgentFeed: a news reader and protocol, with a local open model behind it."),
  ("status",    0.4, "Open weights, on your hardware: Qwen 3, on Ollama."),
  ("topic_new", 0.3, "It collects relevant articles on the topics that interest you. One sentence, and the model builds the vocabulary, searches, and judges every match."),
  ("topic",     0.3, "No keyword match here. Found by meaning, and judged relevant."),
  ("article",   0.4, "Every article gets a local abstract."),
  ("german",    0.3, "Switch to German, and the model rewrites it."),
  ("cached",    0.3, "Back and forth: instant. Once written, a translation is kept."),
  ("analysis",  0.4, "Analyze the market through the news. The analysis is built from your articles, and every claim cites one."),
  ("keep",      0.5, "Keep what matters. Dismiss what doesn't, and it never comes back."),
  ("ask",       0.3, "Ask a collection a question. It reads the abstracts, and your notes."),
  ("answer",    0.2, "Every finding cites the article it came from."),
  ("agents",    0.3, "And it's a protocol. A site adds one link tag; agents discover its feed and subscribe."),
  ("end",       0.4, "Nothing leaves your machine. AgentFeed."),
]

video = os.path.join(CUT, "agentfeed-demo.mp4")
tl = json.load(open(os.path.join(CUT, "timeline.json")))
os.makedirs(OUT, exist_ok=True)

def dur(f):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", f], capture_output=True, text=True).stdout)
total = dur(video)
tl["card"] = 0.0
tl["end"] = total - 4.2                        # the end card is the last three seconds

def srt_t(s):
    ms = int(round(s * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

cues, inputs, filters = [], [], []
prev_end = 0.0
for i, (b, off, text) in enumerate(LINES):
    start = max(tl[b] + off, prev_end + 0.35)
    nxt = tl[LINES[i + 1][0]] + LINES[i + 1][1] if i + 1 < len(LINES) else total
    wav = os.path.join(OUT, f"l{i:02d}.wav")
    synth(text, wav)
    d = dur(wav); tempo = 1.0
    room = nxt - 0.3 - start
    if d > room > 0:
        tempo = min(1.15, d / room)
        fast = os.path.join(OUT, f"l{i:02d}f.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-af", f"atempo={tempo:.3f}", fast], check=True)
        wav = fast; d = dur(wav)
    print(f"{start:6.1f}-{start+d:5.1f}  tempo {tempo:.2f}  room {room:5.1f}  {b:10s} {text[:58]}")
    cues.append((start, start + d, b, text))
    inputs += ["-i", wav]
    filters.append(f"[{i}]adelay={int(start*1000)}|{int(start*1000)}[a{i}]")
    prev_end = start + d

mix = ("".join(f"[a{i}]" for i in range(len(LINES)))
       + f"amix=inputs={len(LINES)}:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[out]")
voice = os.path.join(OUT, "voiceover.wav")
subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(filters) + ";" + mix,
                "-map", "[out]", "-ar", "48000", voice], check=True)

srt = os.path.join(OUT, "agentfeed-demo.srt")
with open(srt, "w") as f:
    for n, (a, e, _, t) in enumerate(cues, 1):
        f.write(f"{n}\n{srt_t(a)} --> {srt_t(e + 0.3)}\n{t}\n\n")
with open(os.path.join(OUT, "script.md"), "w") as f:
    f.write("# Narration\n\n| at | beat | line |\n|---|---|---|\n")
    for a, e, b, t in cues:
        f.write(f"| {int(a)//60}:{int(a)%60:02d} | {b} | {t} |\n")

# Burned-in subtitles: an .ass with the video's own PlayRes, so the font size
# and margins mean pixels. (force_style on an .srt scales against a 384x288
# default and comes out enormous.)
vw, vh = [int(x) for x in subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
          "-show_entries", "stream=width,height", "-of", "csv=p=0", video],
          capture_output=True, text=True).stdout.strip().split(",")]
def ass_t(x):
    cs = int(round(x * 100)); h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"
ass = os.path.join(OUT, "agentfeed-demo.ass")
with open(ass, "w") as f:
    f.write(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {vw}
PlayResY: {vh}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Helvetica Neue,{int(vh*0.034)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H8C000000,0,0,0,0,100,100,0,0,4,0,{int(vh*0.010)},2,{int(vw*0.12)},{int(vw*0.12)},{int(vh*0.045)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""")
    for a, e, _, t in cues:
        f.write(f"Dialogue: 0,{ass_t(a)},{ass_t(e + 0.3)},Sub,,0,0,0,,{t}\n")
final = os.path.join(OUT, "agentfeed-demo-voiced.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-i", voice,
                "-vf", f"ass={ass}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}", "-movflags", "+faststart", final], check=True)
print("final", final, f"{dur(final):.1f}s")
