#!/usr/bin/env python3
import os
from itertools import product
from subprocess import run
from datetime import now
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


if __name__ == "__main__":
    isotime = now().isoformat()
    outdir = Path(f"{SCRIPT_DIR}/{isotime}")
    outdir.mkdir(parents=True, exist_ok=True)
    for (num, ratio) in product(range(1, 19), range(0.0, 1.125, 0.125)):
        with open(f"{outdir}/redis-ycsb-a-{num}-4-8-{ratio:.3f}.log", "w") as log:
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
                stdout=log,
                stderr=log,
                check=True,
            )
