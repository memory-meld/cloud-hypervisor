from contextlib import contextmanager, redirect_stdout, redirect_stderr
from logging import info
from pathlib import Path
from datetime import datetime
from itertools import product
from subprocess import check_output

from rich import inspect, print

from bencher import main, Args, Bench, ENV_SETUP_SCRIPTS


@contextmanager
def outdir(name: str = str(datetime.now().isoformat())):
    script_dir = Path(__file__).resolve().parent
    d = script_dir / name
    try:
        d.mkdir(parents=True, exist_ok=True)
        yield d
    finally:
        # cleanup if nothing has been generated
        try:
            dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    skip = []  # ["freq"]
    for name, script in ENV_SETUP_SCRIPTS.items():
        if name in skip:
            continue
        check_output(script, shell=True)

    with outdir() as dir:
        # for num, ratio in product([1, 2, 8, 9, 10, 17, 18, 19], [0.2, 0.625]):
        for num, ratio in product([], [0.2, 0.625]):
            opt = Args().from_dict(
                dict(
                    bench=Bench.REDIS,
                    num=num,
                    dram_ratio=ratio,
                    # perf_event="r80D1:P",
                    # perf_event="MEM_TRANS_RETIRED.LOAD_LATENCY_GT_256:P",
                    pretty=True,
                )
            )
            print(opt)
            log = dir / f"redis-ycsb-a-{num}-4-8-{ratio:.3f}.log"
            try:
                # logging will cache fd which will cause redirect_stderr(f) to misbehave
                with log.open("w") as f, redirect_stdout(f):
                    main(opt)
            except Exception:
                print(f"failed case: config num {num} dram ratio {ratio}")
                log.unlink()
