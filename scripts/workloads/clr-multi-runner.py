#!/usr/bin/env python3
import os
from itertools import product
from subprocess import run
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


if __name__ == "__main__":
    isotime = datetime.now().isoformat()
    outdir = Path(f"{SCRIPT_DIR}/{isotime}")
    outdir.mkdir(parents=True, exist_ok=True)
    for (num, ratio) in product(
        range(1, 19),
        map(lambda x: x / 8.0, range(9)),
    ):
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
