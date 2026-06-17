"""
Benchmark FFmpeg thread count for Ken Burns-style encoding on this machine.

Usage:
    python scratch/benchmark_ffmpeg_threads.py
    python scratch/benchmark_ffmpeg_threads.py --threads 2 4 6 8
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Allow running from repo root or storyforge-api/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw


def _make_test_image(path: Path, width: int = 1280, height: int = 720) -> None:
    img = Image.new("RGB", (width, height), color=(20, 15, 40))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        draw.line([(0, y), (width, y)], fill=(int(30 + t * 40), int(20 + t * 30), int(60 + t * 50)))
    draw.text((width // 2, height // 2), "StoryForge FFmpeg Benchmark", fill=(200, 180, 255), anchor="mm")
    img.save(path, format="PNG")


def _make_test_audio(path: Path, duration_secs: float = 8.0) -> None:
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(duration_secs), "-c:a", "libmp3lame", "-b:a", "128k", str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=60)


def _run_ken_burns_bench(
    image: Path,
    audio: Path,
    out: Path,
    threads: str,
    duration: float = 8.0,
) -> tuple[float, int]:
    frames = int(duration * 25)
    zoom_expr = "min(zoom+0.0008,1.40)"
    filter_complex = (
        f"[0:v]scale=1280:720,zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':d={frames}:s=1280x720:fps=25[v]"
    )
    cmd = [
        "ffmpeg", "-y", "-threads", threads,
        "-loop", "1", "-framerate", "25", "-i", str(image),
        "-i", str(audio),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a", "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
        str(out),
    ]
    start = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed (threads={threads}): {proc.stderr[-800:]}")
    size = out.stat().st_size if out.exists() else 0
    return elapsed, size


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark FFMPEG_THREADS for StoryForge encoding")
    parser.add_argument("--threads", nargs="+", default=["2", "4", "6"], help="Thread counts to test")
    parser.add_argument("--duration", type=float, default=8.0, help="Clip duration seconds")
    parser.add_argument("--runs", type=int, default=2, help="Runs per thread count (best of)")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not on PATH")
        sys.exit(1)

    print("StoryForge FFmpeg thread benchmark")
    print(f"  Clip duration: {args.duration}s | runs per setting: {args.runs}")
    print(f"  Thread counts: {', '.join(args.threads)}")
    print()

    with tempfile.TemporaryDirectory(prefix="sf_bench_") as tmp:
        tmp_path = Path(tmp)
        image = tmp_path / "bench.png"
        audio = tmp_path / "bench.mp3"
        _make_test_image(image)
        _make_test_audio(audio, args.duration)

        results: list[tuple[str, float, int]] = []
        for threads in args.threads:
            times: list[float] = []
            out_size = 0
            for run in range(args.runs):
                out = tmp_path / f"out_{threads}_run{run}.mp4"
                elapsed, out_size = _run_ken_burns_bench(image, audio, out, threads, args.duration)
                times.append(elapsed)
                print(f"  threads={threads} run {run + 1}/{args.runs}: {elapsed:.2f}s")
            best = min(times)
            results.append((threads, best, out_size))

    print()
    print(f"{'Threads':<10} {'Best (s)':<12} {'Output KB':<12} {'vs baseline'}")
    print("-" * 48)
    baseline = results[0][1] if results else 1.0
    for threads, best, size in results:
        delta = ((best - baseline) / baseline * 100) if baseline else 0
        marker = "  <- recommended" if threads == "4" else ""
        print(f"{threads:<10} {best:<12.2f} {size // 1024:<12} {delta:+.1f}%{marker}")

    if len(results) >= 2:
        fastest = min(results, key=lambda r: r[1])
        print()
        print(f"Fastest: {fastest[0]} threads ({fastest[1]:.2f}s)")
        if fastest[0] == "4":
            print("FFMPEG_THREADS=4 is optimal on this machine.")
        elif fastest[0] != "4":
            print(f"Consider FFMPEG_THREADS={fastest[0]} if RAM stays comfortable during full pipeline runs.")


if __name__ == "__main__":
    main()
