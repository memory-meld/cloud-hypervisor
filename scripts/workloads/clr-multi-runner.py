#!/usr/bin/env python3
import os
from itertools import product
from subprocess import run, CalledProcessError
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


if __name__ == "__main__":
    isotime = datetime.now().isoformat()
    outdir = Path(f"{SCRIPT_DIR}/{isotime}")
    outdir.mkdir(parents=True, exist_ok=True)
    for num in range(1, 25):
        for dram in range(0, 9):
            ratio = dram / 8.0
            log = outdir / f"redis-ycsb-a-{num}-4-8-{ratio:.3f}.log"
            try:
                with log.open("w") as f:
                    run(
                        [
                            "python3",
                            f"{SCRIPT_DIR}/clr-multi.py",
                            "--num",
                            f"{num}",
                            "--dram-ratio",
                            f"{ratio}",
                            "redis",
                            "--workload",
                            "a",
                        ],
                        stdout=f,
                        stderr=f,
                        check=True,
                    )
            except CalledProcessError:
                print(f"running out of pmem: config num {num} dram {dram}")
                log.unlink()
                break
