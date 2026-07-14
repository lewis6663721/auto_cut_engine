from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = ROOT / "static" / "demos" / "sample.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(target),
        ],
        check=True,
    )
    print(target)


if __name__ == "__main__":
    main()

