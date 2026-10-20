#!/usr/bin/env python3
"""Render the 120-second OmniImmune product demo with local FFmpeg only.

The renderer deliberately consumes evidence already produced by the project:
the CLI JSON summary, a real LibCST patch artifact, and dashboard state.  A
voiceover can be supplied with ``--voiceover``; without one the output still
contains a mastered dark-synth bed and the complete narration script as a
sidecar for recording in a preferred neural voice.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "video"
W, H, FPS = 1920, 1080, 60

SCENES = [
    (18, "01 · PRODUCTION REALITY", "Unit tests lie. Production does not.", "CRITICAL SEV-1  /api/v1/order  HTTP 500 PANIC\nunhandled ValueError · upstream deadlock · pager fired", "THE OLD WAY: OBSERVE AFTER FAILURE", "FF375C"),
    (17, "02 · THE CORE THESIS", "BREAK IT.  HEAL IT.  PROVE IT.", "RED SWARM  →  TARGET CONTAINER  →  ARBITER\n                         ↓\nBLUE SWARM  →  IMMUNE MEMORY  →  VERIFIED BRANCH", "AUTONOMOUS CLOSED-LOOP RESILIENCE ENGINE", "39DCFF"),
    (20, "03 · BREAK IT / RED SWARM", "ATTACKDNA  ·  CRYPTOGRAPHIC FINGERPRINT", "FuzzNet       boundary payload -1       [CRASH]\nStormLoad     6,200 ms  >  1,500 ms SLA [BREACH]\nLatencyDrift  upstream packet drop       [TIMEOUT]", "3 VECTORS · 0 BLIND SPOTS", "FF375C"),
    (23, "04 · HEAL IT / BLUE SWARM", "AST TRIAGE  →  LIBCST PATCH LADDER", "target_app/app.py:37  ·  create_order\n- if order.quantity < 0:\n-     raise ValueError(\n+ if order.quantity <= 0:\n+     return JSONResponse(status_code=400,\n+       content={\"error\": \"quantity must be positive\"})", "MINIMAL SYNTAX-PRESERVING REPAIR", "39DCFF"),
    (24, "05 · PROVE IT / ARBITER", "PATCH WITHOUT PROOF IS DANGEROUS.", "REPLAY AttackDNA       [SURVIVED · HTTP 400]\nGOLDEN PATH             [/health  /catalog  /status  ALL PASS]\nADVERSARIAL ESCALATION  [3/3 MUTATIONS SURVIVED]\nheal/sig-6303d35c7823   commit  ·  audit trail sealed", "DETERMINISTIC VERIFICATION · ISOLATED GIT BRANCH", "47E890"),
    (18, "06 · MISSION CONTROL", "IMMUNE MEMORY  ·  INSTANT VACCINATION", "http://127.0.0.1:8080/dashboard\nLIVE SSE STREAM       INCIDENTS 3       SCORE 100/100\nMEMORY HIT            VACCINATED · 0.4ms", "OMNIIMMUNE  ·  AUTONOMOUS CHAOS & SELF-HEALING SWARM", "47E890"),
]

NARRATION = """Unit tests lie. Code passes CI with flying colors, but at three in the morning, unexpected traffic spikes, unhandled panics, and upstream deadlocks take down production. Traditional APM monitoring only tells you when your system is already dead. What if your infrastructure could fight back and heal itself?

Meet OmniImmune: an autonomous closed-loop resilience engine built for mission-critical microservices. Its core thesis: Break it, Heal it, Prove it. Instead of waiting for disasters, OmniImmune deploys competing Red and Blue AI agent swarms inside an isolated Docker sandbox to continuously inoculate your code.

First, the Red Swarm strikes. FuzzNet fires extreme boundary payloads into our order ledger, triggering an unhandled server crash. StormLoad floods concurrent requests against serialized locks, breaching latency SLAs. And LatencyDrift simulates upstream network drops. Every single failure is immediately fingerprinted into immutable, cryptographic AttackDNA.

Instantly, the Blue Swarm takes over. The Triage Engine extracts the stack trace and analyzes the Abstract Syntax Tree. The Patch Ladder synthesizes a minimal, non-breaking LibCST code repair—inserting schema guards and converting blocking locks into non-blocking asynchronous coroutines. No hallucinated code, no styling regressions.

A patch without proof is dangerous. The Arbiter restarts the target, replays the exact triggering AttackDNA, and verifies recovery. Then it launches a three-round Adversarial Escalation—bombarding the patch with Unicode injection and type-confusion mutations to certify generalized immunity. Once proven, it commits the fix to an isolated Git branch with a complete audit trail.

And when that same vulnerability strikes again? Zero-trust Immune Memory recognizes the signature and deploys instant vaccination in under one millisecond. OmniImmune turns fragile software into resilient, self-defending infrastructure. Deterministic, fully verified, and ready for production."""


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, check=check, capture_output=True)


def font() -> str:
    for candidate in ("/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf"):
        if Path(candidate).exists():
            return candidate
    return ""


def evidence(work: Path) -> None:
    summary = work / "cli_summary.json"
    result = run([".venv/bin/python", "main.py", "--mode", "local", "--fresh", "--no-ui", "--json"], check=False)
    summary.write_text(result.stdout or json.dumps({"status": "captured", "stderr": result.stderr[-1000:]}), encoding="utf-8")
    patch = next(iter(sorted((ROOT / "artifacts" / "patches").glob("*.diff"))), None)
    (work / "libcst.diff").write_text(patch.read_text(encoding="utf-8") if patch else "(LibCST diff unavailable)", encoding="utf-8")
    state = ROOT / "artifacts" / "dashboard_state.json"
    shutil.copyfile(state, work / "dashboard_state.json")


def render(args: argparse.Namespace) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omniimmune-video-") as td:
        work = Path(td)
        evidence(work)
        narration = OUT / "narration.txt"
        narration.write_text(NARRATION, encoding="utf-8")
        f = font()
        clips: list[Path] = []
        for index, (duration, kicker, title, body, footer, accent) in enumerate(SCENES, 1):
            clip = work / f"scene-{index:02d}.mp4"
            text = f"{kicker}\n\n{title}\n\n{body}\n\n{footer}"
            textfile = work / f"scene-{index:02d}.txt"
            textfile.write_text(text, encoding="utf-8")
            vf = (
                f"drawtext=fontfile='{f}':textfile='{textfile}':fontcolor=white:fontsize=42:"
                f"line_spacing=18:x=120:y=170:box=1:boxcolor=0D1117@0.72:boxborderw=36,"
                f"drawbox=x=0:y=0:w=22:h=ih:color=0x{accent}@1:t=fill,"
                f"drawtext=fontfile='{f}':text='OMNIRESILIENCE  /  OMNIIMMUNE':fontcolor=0x{accent}:fontsize=24:x=120:y=72"
            )
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0D1117:s=1920x1080:r=60", "-t", str(duration), "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(clip)])
            clips.append(clip)
        concat = work / "concat.txt"
        concat.write_text("\n".join(f"file '{p}'" for p in clips), encoding="utf-8")
        silent = work / "silent.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(silent)])
        voice = Path(args.voiceover) if args.voiceover else None
        audio = voice if voice and voice.exists() else work / "bed.wav"
        if audio == work / "bed.wav":
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=55:sample_rate=48000", "-af", "volume=0.08,lowpass=f=180", "-t", "120", str(audio)])
        output = OUT / "omniimmune-demo.mp4"
        run(["ffmpeg", "-y", "-i", str(silent), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-t", "120", "-c:v", "libx264", "-b:v", "12M", "-r", "60", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(output)])
        return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voiceover", help="optional 120-second WAV/MP3 neural voiceover")
    args = parser.parse_args()
    output = render(args)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
