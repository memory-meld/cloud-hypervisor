from contextlib import redirect_stdout, redirect_stderr
from logging import info
from pathlib import Path
from datetime import datetime
from itertools import product

from rich import inspect, print

from bencher import main, Opt, Benchmark

SCRIPT_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    isotime = datetime.now().isoformat()
    outdir = SCRIPT_DIR / str(isotime)
    outdir.mkdir(parents=True, exist_ok=True)
    for num, ratio in product(range(1, 28), [0.2, 0.625]):
        opt = Opt.from_dict(
            dict(bench=Benchmark.MANUAL, num=num, dram_ratio=ratio, pretty=True)
        )
        inspect(opt)
        log = outdir / f"redis-ycsb-a-{num}-4-8-{ratio:.3f}.log"
        try:
            # , redirect_stderr(f)
            with log.open("w") as f, redirect_stdout(f):
                main(opt)
        except Exception:
            print(f"failed case: config num {num} dram ratio {ratio}")
            log.unlink()
