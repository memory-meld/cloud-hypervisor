from contextlib import redirect_stdout, redirect_stderr
from logging import info
from pathlib import Path
from datetime import datetime
from itertools import product
from subprocess import check_output

from rich import inspect, print

from bencher import main, Opt, Benchmark, ENV_SETUP_SCRIPTS


def outdir(name: str = str(datetime.now().isoformat())) -> Path:
    script_dir = Path(__file__).resolve().parent
    d = script_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == "__main__":
    skip = []  # ["freq"]
    for name, script in ENV_SETUP_SCRIPTS:
        if name in skip:
            continue
        check_output(script, shell=True)

    dir = outdir()
    for num, ratio in product(range(1, 28), [0.2, 0.625]):
        opt = Opt.from_dict(
            dict(
                bench=Benchmark.REDIS,
                num=num,
                dram_ratio=ratio,
                perf="r80D1:P",
                pretty=True,
            )
        )
        inspect(opt)
        log = dir / f"redis-ycsb-a-{num}-4-8-{ratio:.3f}.log"
        try:
            # , redirect_stderr(f)
            with log.open("w") as f, redirect_stdout(f):
                main(opt)
        except Exception:
            print(f"failed case: config num {num} dram ratio {ratio}")
            log.unlink()
