from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "video" / "omniimmune_product_demo.mp4"
WIDTH, HEIGHT, FPS = 1280, 720, 15
BG = (13, 17, 23)
CRIMSON = (255, 55, 92)
CYAN = (57, 220, 255)
GREEN = (71, 232, 144)
AMBER = (255, 190, 74)
MUTED = (137, 153, 171)
WHITE = (238, 244, 248)


SCENES = [
    (20, "THE PROBLEM", "Unit tests lie. Production does not.", "server"),
    (15, "THE ARCHITECTURE", "Break it. Heal it. Prove it.", "architecture"),
    (20, "BREAK IT", "Deterministic chaos. Cryptographic AttackDNA.", "chaos"),
    (30, "HEAL IT · PROVE IT", "Semantic patches. Adversarial proof.", "heal"),
    (20, "AUTO-GIT · IMMUNE MEMORY", "Every fix is isolated, auditable, reusable.", "git"),
    (15, "MISSION CONTROL", "Self-healing infrastructure, ready for production.", "summary"),
]

VOICEOVER = [
    "Unit tests lie. Code passes CI, but at 3 AM in production, unexpected traffic spikes, unhandled panics, and upstream deadlocks take down critical services. Traditional APM monitoring only tells you when your system is already dead. What if your infrastructure could fight back and heal itself?",
    "Introducing OmniImmune: an autonomous closed-loop resilience engine built for mission-critical infrastructure. Its core thesis is simple: Break it, Heal it, Prove it. It deploys competing Red and Blue AI agent swarms to continuously inoculate your services against catastrophic failures.",
    "First, the Red Swarm deploys deterministic chaos. FuzzNet floods boundary payloads triggering an unhandled order panic. StormLoad stresses concurrency locks until response times breach SLA limits. And our async latency proxy simulates upstream network failure. The target crashes, and each incident is fingerprinted into cryptographic AttackDNA.",
    "Next, the Blue Swarm triages the AST and applies a LibCST semantic patch, preserving clean code formatting. But a patch is not enough: the Arbiter replays the exact AttackDNA to verify recovery, checks golden-path regressions, and runs a three-round Adversarial Escalation with Unicode and type confusion mutations to certify generalized immunity without overfitting.",
    "Verified repairs are automatically committed to isolated Git branches with cryptographic audit trails ready for one-click human review. And when the Red Swarm strikes again? Immune Memory delivers instant O one vaccination with zero downtime and zero regressions.",
    "OmniImmune transforms fragile code into self-healing infrastructure. One hundred percent test-verified, single-command Docker reproducible, and built for real-world resilience. Explore the project on GitHub.",
]


def font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ("/System/Library/Fonts/SFNSMono.ttf" if mono else "/System/Library/Fonts/SFNS.ttf"),
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf" if mono else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


FONTS = {name: font(size, mono) for name, size, mono in (
    ("title", 46, False), ("subtitle", 26, False), ("body", 22, False),
    ("small", 17, False), ("mono", 18, True), ("mono_small", 14, True),
    ("score", 112, False), ("node", 22, False),
)}


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def base(frame: int, scene_index: int, title: str, strap: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        t = y / HEIGHT
        draw.line((0, y, WIDTH, y), fill=lerp(BG, (20, 29, 41), t * 0.9))
    grid = (24, 37, 50)
    for x in range(0, WIDTH, 48):
        draw.line((x, 0, x, HEIGHT), fill=grid, width=1)
    for y in range(0, HEIGHT, 48):
        draw.line((0, y, WIDTH, y), fill=grid, width=1)
    draw.text((54, 38), "OMNIIMMUNE", font=FONTS["small"], fill=CYAN)
    draw.text((54, 66), title, font=FONTS["title"], fill=WHITE)
    draw.text((56, 124), strap, font=FONTS["subtitle"], fill=MUTED)
    draw.text((1090, 46), f"0{scene_index + 1} / 06", font=FONTS["small"], fill=MUTED)
    draw.line((54, 162, WIDTH - 54, 162), fill=(42, 60, 74), width=1)
    return image


def panel(draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], outline: tuple[int, int, int], fill=(16, 24, 33), radius=14) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=2)


def terminal(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], lines: list[tuple[str, tuple[int, int, int]]], progress: float = 1.0) -> None:
    x1, y1, x2, y2 = box
    panel(draw, box, (40, 62, 78), fill=(8, 13, 19))
    draw.ellipse((x1 + 18, y1 + 16, x1 + 28, y1 + 26), fill=CRIMSON)
    draw.ellipse((x1 + 34, y1 + 16, x1 + 44, y1 + 26), fill=AMBER)
    draw.ellipse((x1 + 50, y1 + 16, x1 + 60, y1 + 26), fill=GREEN)
    y = y1 + 50
    for text, color in lines:
        draw.text((x1 + 22, y), text, font=FONTS["mono_small"], fill=color)
        y += 25
    draw.rectangle((x1 + 22, y2 - 24, x2 - 22, y2 - 18), fill=(25, 42, 54))
    draw.rectangle((x1 + 22, y2 - 24, x1 + 22 + int((x2 - x1 - 44) * progress), y2 - 18), fill=CYAN)


def subtitle(draw: ImageDraw.ImageDraw, text: str, accent_words: tuple[str, ...] = ()) -> None:
    box = (100, HEIGHT - 112, WIDTH - 100, HEIGHT - 38)
    draw.rounded_rectangle(box, radius=16, fill=(5, 9, 14, 235), outline=(48, 68, 82), width=1)
    words = text.split()
    pieces: list[tuple[str, tuple[int, int, int]]] = []
    for word in words:
        clean = word.strip(".,?!")
        pieces.append((word + " ", CYAN if clean in accent_words else WHITE))
    widths = [draw.textlength(word, font=FONTS["body"]) for word, _ in pieces]
    x = (WIDTH - sum(widths)) / 2
    for (word, color), width in zip(pieces, widths):
        draw.text((x, HEIGHT - 91), word, font=FONTS["body"], fill=color)
        x += width


def server_scene(frame: int, total: int, index: int) -> Image.Image:
    image = base(frame, index, SCENES[index][1], SCENES[index][2])
    draw = ImageDraw.Draw(image)
    pulse = (math.sin(frame / 4) + 1) / 2
    for rack in range(5):
        x = 72 + rack * 150
        draw.rounded_rectangle((x, 205, x + 112, 570), radius=8, fill=(24, 30, 38), outline=(67, 79, 91), width=2)
        for row in range(8):
            y = 228 + row * 40
            draw.rectangle((x + 14, y, x + 98, y + 23), fill=(12, 19, 26), outline=(44, 61, 73))
            color = CRIMSON if (row + rack + frame // 6) % 3 == 0 else (60, 100, 112)
            draw.ellipse((x + 22, y + 8, x + 30, y + 16), fill=color)
            draw.line((x + 42, y + 11, x + 88, y + 11), fill=(52, 76, 89), width=2)
    draw.rectangle((0, 580, WIDTH, 650), fill=(18, 12, 19))
    for x in range(10, WIDTH, 70):
        draw.ellipse((x, 598 + int(pulse * 4), x + 7, 605 + int(pulse * 4)), fill=CRIMSON)
    terminal(draw, (810, 212, 1208, 540), [
        ("$ curl /api/v1/order", MUTED),
        ("HTTP/1.1 500 INTERNAL", CRIMSON),
        ("ValueError: CRITICAL_ORDER_PANIC", CRIMSON),
        ("pagerduty: SEV-1", AMBER),
        ("upstream: timeout", CRIMSON),
        ("system status: degraded", CRIMSON),
    ], progress=min(1, frame / max(1, total * 0.65)))
    draw.text((820, 560), "3:00 AM  ·  PRODUCTION  ·  INCIDENT #OMNI-042", font=FONTS["small"], fill=CRIMSON)
    subtitle(draw, "What if your infrastructure could fight back and heal itself?", ("fight", "heal"))
    return image


def architecture_scene(frame: int, total: int, index: int) -> Image.Image:
    image = base(frame, index, SCENES[index][1], SCENES[index][2])
    draw = ImageDraw.Draw(image)
    nodes = [(160, 320, "RED SWARM", CRIMSON), (540, 320, "ARBITER", WHITE), (920, 320, "BLUE SWARM", CYAN)]
    for x, y, label, color in nodes:
        glow = 8 + int(4 * math.sin(frame / 5))
        draw.ellipse((x - 92 - glow, y - 92 - glow, x + 92 + glow, y + 92 + glow), outline=(*color, 80), width=2)
        draw.ellipse((x - 78, y - 78, x + 78, y + 78), fill=(18, 27, 37), outline=color, width=4)
        tw = draw.textlength(label, font=FONTS["node"])
        draw.text((x - tw / 2, y - 12), label, font=FONTS["node"], fill=color)
    for x1, x2, color in ((252, 462, CRIMSON), (618, 828, CYAN)):
        draw.line((x1, 320, x2, 320), fill=color, width=4)
        pos = x1 + int((x2 - x1) * ((frame % 30) / 30))
        draw.polygon([(pos, 320), (pos - 16, 310), (pos - 16, 330)], fill=color)
    draw.text((70, 530), "DETERMINISTIC CHAOS", font=FONTS["small"], fill=CRIMSON)
    draw.text((510, 530), "CLOSED-LOOP CONTROL", font=FONTS["small"], fill=WHITE)
    draw.text((900, 530), "SEMANTIC REPAIR", font=FONTS["small"], fill=CYAN)
    subtitle(draw, "Break it. Heal it. Prove it.", ("Break", "Heal", "Prove"))
    return image


def chaos_scene(frame: int, total: int, index: int) -> Image.Image:
    image = base(frame, index, SCENES[index][1], SCENES[index][2])
    draw = ImageDraw.Draw(image)
    terminal(draw, (54, 205, 735, 600), [
        ("$ docker compose up --build", CYAN),
        ("[RED SWARM] attack wave 01", CRIMSON),
        ("[FUZZNET] boundary payload → 500", CRIMSON),
        ("[STORMLOAD] p99 2,482ms > SLA", AMBER),
        ("[LATENCY PROXY] upstream dropped", CRIMSON),
        ("[ARBITER] fingerprinting incident", WHITE),
        ("AttackDNA sha256: 5491fe80...", CYAN),
    ], progress=(frame % 45) / 45)
    panel(draw, (790, 205, 1208, 600), CRIMSON, fill=(28, 16, 25))
    draw.text((824, 235), "LIVE ATTACK RADAR", font=FONTS["subtitle"], fill=CRIMSON)
    for i, (label, value) in enumerate((("FuzzNet", 0.88), ("StormLoad", 0.74), ("LatencyDrift", 0.96))):
        y = 315 + i * 78
        draw.text((824, y), label, font=FONTS["small"], fill=WHITE)
        draw.rectangle((824, y + 30, 1150, y + 43), fill=(53, 37, 48))
        draw.rectangle((824, y + 30, 824 + int(326 * min(1, value + 0.08 * math.sin(frame / 5))), y + 43), fill=CRIMSON)
        draw.text((1160, y + 22), f"{int(value * 100)}%", font=FONTS["small"], fill=CRIMSON)
    draw.text((824, 555), "STATUS  ·  INCIDENTS FINGERPRINTED", font=FONTS["small"], fill=AMBER)
    subtitle(draw, "The target crashes, and each incident becomes cryptographic AttackDNA.", ("crashes", "AttackDNA"))
    return image


def heal_scene(frame: int, total: int, index: int) -> Image.Image:
    image = base(frame, index, SCENES[index][1], SCENES[index][2])
    draw = ImageDraw.Draw(image)
    panel(draw, (54, 205, 600, 595), CYAN, fill=(10, 19, 27))
    draw.text((80, 230), "LIBCST · AST PATCH LADDER", font=FONTS["small"], fill=CYAN)
    code = [
        ("- if order.quantity < 0:", CRIMSON),
        ("-     raise ValueError(...)", CRIMSON),
        ("+ if order.quantity <= 0:", GREEN),
        ("+     return JSONResponse(", GREEN),
        ('+       status_code=400,', GREEN),
        ('+       content={"status":"rejected"})', GREEN),
        ("", WHITE),
        ("✓ syntax tree valid", CYAN),
        ("✓ container auto-reloaded", CYAN),
    ]
    y = 280
    for line, color in code:
        draw.text((82, y), line, font=FONTS["mono"], fill=color)
        y += 32
    panel(draw, (650, 205, 1208, 595), GREEN, fill=(10, 25, 24))
    draw.text((680, 230), "ADVERSARIAL ESCALATION", font=FONTS["small"], fill=GREEN)
    mutations = [("01", "numeric boundary"), ("02", "unicode injection"), ("03", "nested type confusion")]
    for i, (num, label) in enumerate(mutations):
        y = 300 + i * 78
        draw.ellipse((684, y, 724, y + 40), fill=(22, 62, 49), outline=GREEN, width=2)
        draw.text((696, y + 9), num, font=FONTS["small"], fill=GREEN)
        draw.text((750, y + 2), label, font=FONTS["body"], fill=WHITE)
        draw.text((750, y + 34), "survived · target healthy", font=FONTS["small"], fill=GREEN)
    draw.text((680, 555), "GENERALIZED IMMUNITY  ·  CERTIFIED", font=FONTS["small"], fill=GREEN)
    subtitle(draw, "A patch is not enough. We mutate the attack to prove immunity generalizes.", ("mutate", "immunity"))
    return image


def git_scene(frame: int, total: int, index: int) -> Image.Image:
    image = base(frame, index, SCENES[index][1], SCENES[index][2])
    draw = ImageDraw.Draw(image)
    draw.text((70, 218), "GIT AUDIT GRAPH", font=FONTS["small"], fill=MUTED)
    main_x, main_y = 160, 420
    draw.line((main_x, main_y, 440, main_y), fill=WHITE, width=4)
    draw.ellipse((main_x - 12, main_y - 12, main_x + 12, main_y + 12), fill=WHITE)
    draw.text((90, 465), "main", font=FONTS["body"], fill=WHITE)
    branches = [(440, 420, 670, 310, "heal/sig-order-panic", CYAN), (440, 420, 670, 500, "heal/sig-upstream", GREEN)]
    for x1, y1, x2, y2, label, color in branches:
        draw.line((x1, y1, x2, y2), fill=color, width=4)
        draw.ellipse((x2 - 13, y2 - 13, x2 + 13, y2 + 13), fill=color)
        draw.text((x2 + 25, y2 - 14), label, font=FONTS["small"], fill=color)
    panel(draw, (810, 220, 1208, 570), GREEN, fill=(11, 27, 25))
    draw.text((845, 248), "VERIFIED PATCH", font=FONTS["subtitle"], fill=GREEN)
    draw.text((845, 305), "heal/sig-order-panic", font=FONTS["mono"], fill=CYAN)
    draw.text((845, 365), "commit  7f3a91c", font=FONTS["mono"], fill=WHITE)
    draw.text((845, 410), "DNA SHA-256  84074d...", font=FONTS["mono_small"], fill=MUTED)
    draw.text((845, 470), "VACCINATED  ✓", font=FONTS["title"], fill=GREEN)
    draw.text((845, 530), "Immune Memory · O(1) lookup", font=FONTS["small"], fill=GREEN)
    subtitle(draw, "Every verified repair becomes an isolated, auditable branch.", ("verified", "auditable"))
    return image


def summary_scene(frame: int, total: int, index: int) -> Image.Image:
    image = base(frame, index, SCENES[index][1], SCENES[index][2])
    draw = ImageDraw.Draw(image)
    draw.text((94, 245), "IMMUNITY SCORE", font=FONTS["subtitle"], fill=MUTED)
    draw.text((78, 275), "100", font=FONTS["score"], fill=GREEN)
    draw.text((350, 320), "/ 100", font=FONTS["title"], fill=MUTED)
    cards = [(560, 250, "3/3", "VERIFIED", GREEN), (850, 250, "0", "REGRESSIONS", CYAN), (560, 420, "3", "MUTATIONS / FIX", AMBER), (850, 420, "O(1)", "IMMUNE MEMORY", CYAN)]
    for x, y, value, label, color in cards:
        panel(draw, (x, y, x + 235, y + 125), color, fill=(17, 26, 35))
        draw.text((x + 22, y + 18), value, font=FONTS["title"], fill=color)
        draw.text((x + 22, y + 82), label, font=FONTS["small"], fill=WHITE)
    draw.text((82, 560), "github.com/omnirush/omniimmune", font=FONTS["body"], fill=CYAN)
    draw.text((82, 600), "SYSTEMS · INFRASTRUCTURE · CYBERSECURITY", font=FONTS["small"], fill=MUTED)
    subtitle(draw, "Explore OmniImmune: infrastructure that fights back and heals itself.", ("fights", "heals"))
    return image


RENDERERS = {"server": server_scene, "architecture": architecture_scene, "chaos": chaos_scene, "heal": heal_scene, "git": git_scene, "summary": summary_scene}


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=ROOT)


def make_voiceover(work: Path) -> Path:
    chunks: list[Path] = []
    for i, text in enumerate(VOICEOVER):
        raw = work / f"voice_{i}.aiff"
        run(["say", "-v", "Samantha", "-r", "182", "-o", str(raw), text])
        chunks.append(raw)
    concat = work / "voice_concat.txt"
    concat.write_text("\n".join(f"file '{path}'" for path in chunks), encoding="utf-8")
    voice = work / "voice.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-ar", "44100", "-ac", "2", str(voice)])
    return voice


def make_synth(work: Path, duration: int = 120) -> Path:
    synth = work / "synth.wav"
    graph = (
        "[0:a]volume=0.045[a0];[1:a]volume=0.028[a1];[2:a]volume=0.018[a2];"
        "[a0][a1][a2]amix=inputs=3:duration=longest,afade=t=in:st=0:d=3,afade=t=out:st=114:d=6"
    )
    run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=110:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={duration}",
        "-filter_complex", graph, "-ar", "44100", "-ac", "2", str(synth),
    ])
    return synth


def render_video(work: Path) -> Path:
    frames = work / "frames"
    frames.mkdir()
    frame_number = 0
    for scene_index, (seconds, _, _, key) in enumerate(SCENES):
        scene_frames = seconds * FPS
        for local in range(scene_frames):
            frame = RENDERERS[key](local, scene_frames, scene_index)
            frame.save(frames / f"frame_{frame_number:05d}.png", optimize=True)
            frame_number += 1
    silent = work / "silent.mp4"
    run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(frames / "frame_%05d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", str(silent)])
    voice = make_voiceover(work)
    synth = make_synth(work)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-i", str(silent), "-i", str(voice), "-i", str(synth),
        "-filter_complex", "[1:a]apad=pad_dur=120,atrim=duration=120[voice];[voice][2:a]amix=inputs=2:duration=first:dropout_transition=2[a]",
        "-map", "0:v", "-map", "[a]", "-t", "120", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(OUTPUT),
    ])
    return OUTPUT


if __name__ == "__main__":
    if shutil.which("ffmpeg") is None or shutil.which("say") is None:
        raise SystemExit("This renderer requires ffmpeg and macOS say")
    with tempfile.TemporaryDirectory(prefix="omniimmune-demo-") as directory:
        result = render_video(Path(directory))
        print(result)
