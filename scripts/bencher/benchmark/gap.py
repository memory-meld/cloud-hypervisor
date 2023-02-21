import time
from functools import reduce
from logging import info
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import List

from numa.info import node_to_cpus

# from rich import print

from ..config import (
    GO_YCSB,
    SHARED_DIR,
    YCSB_OPERATION_COUNT,
    YCSB_PRELOADED,
    YCSB_RECORD_COUNT,
    YCSB_WORKLOAD_ARGS,
    CLIENT_CPU_NODE,
    DRAM_NODE,
    PMEM_NODE,
)
from ..utils import wait_for_exit_all
from ..vm import Vm, ssh_all


def gap_bc(vms: List[Vm], ntrials: int, niter: int):
    dir = SHARED_DIR / "gapbs"
    args = ["tmux", "new", "-d"]
    # 40s for 1 iteration x 1 trial per iteration
    # run for ~3mins i.e. 5 trials
    args += [
        f"'/bin/time -v -- {dir}/bc -n {ntrials} -i {niter} -f {dir}/kronecker-s25d24.sg'"
    ]
    args += ["';'", "pipe-pane", "'cat > /tmp/gap_bc'"]
    ssh_all(vms, args, stderr=DEVNULL)
    ssh_all(vms, ["tmux", "wait", "time"], check=False, stderr=DEVNULL)
    for i, out in enumerate(ssh_all(vms, ["cat", "/tmp/gap_bc"])):
        print(f"vm{i} gap_bc:\n{out}")
    ssh_all(vms, ["sudo", "rmmod", "manual_events"], check=False, stderr=DEVNULL)
    for i, out in enumerate(ssh_all(vms, ["sudo", "dmesg"])):
        print(f"vm{i} dmesg:\n{out}")
    info(f"redis gap_bc workload complete")
