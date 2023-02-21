# fmt: off
import time
from functools import reduce
from logging import info
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import List

from numa.info import node_to_cpus

from ..config import (CLIENT_CPU_NODE, DRAM_NODE, GO_YCSB, PMEM_NODE,
                      PROJECT_DIR, YCSB_OPERATION_COUNT, YCSB_PRELOADED,
                      YCSB_RECORD_COUNT, YCSB_WORKLOAD_ARGS, YcsbWorkload)
from ..utils import wait_for_exit_all
from ..vm import Vm, ssh_all

# fmt: on


def redis(
    vms: List[Vm],
    workload: YcsbWorkload,
    ncpus: int,
    memory_mode: bool,
    perf_event: str,
):
    args = ["tmux", "new", "-d", "redis-server"]
    args += ["--save", "", "--appendonly", "no", "--protected-mode", "no"]
    args += ["--dbfilename", YCSB_PRELOADED, "--dir", PROJECT_DIR]
    if perf_event:
        args += ["sudo", "perf", "record", "--all-user", "--phys-data", "--data"]
        args += ["-z", "-vv", "-e", perf_event]
    # info(redis_server)
    # launch redis with preloaded ycsb keys in the background
    ssh_all(vms, args, stderr=DEVNULL)
    info("all redis servers started")
    # redis taks at least 30s to load the data, we can try to query dbsize later
    time.sleep(15)
    # wait for loading
    while not reduce(
        bool.__and__,
        map(
            lambda vm: vm.ssh(
                ["redis-cli", "dbsize"], stderr=DEVNULL, check=False
            ).strip()
            == f"{YCSB_RECORD_COUNT}",
            vms,
        ),
    ):
        time.sleep(1)
    info("ycsb preload complelte")
    # run ycsb on node 1 to prevent interference with VM running on node 0

    go_ycsb = [
        "numactl",
        "--physcpubind=" + ",".join(map(str, node_to_cpus(CLIENT_CPU_NODE))),
        f"--membind={DRAM_NODE if memory_mode else PMEM_NODE}",
        "--",
    ]
    go_ycsb += [
        GO_YCSB,
        "run",
        "redis",
        "-p",
        f"recordcount={YCSB_RECORD_COUNT}",
        "-p",
        f"operationcount={YCSB_OPERATION_COUNT}",
        "-p",
        f"threadcount={ncpus}",
    ]
    go_ycsb += YCSB_WORKLOAD_ARGS[workload.name]

    ycsb_clients = [
        Popen(
            go_ycsb + ["-p", f"redis.addr={vm.ip()}:6379"],
            cwd=vm.cwd(),
            stdout=PIPE,
            stderr=PIPE,
        )
        for vm in vms
    ]
    for i, (out, err) in enumerate(wait_for_exit_all(ycsb_clients)):
        print(f"vm{i} stdout:\n{out}\nvm{i} stderr:\n{err}")
    if perf_event:
        perf_script = [
            "sudo",
            "perf",
            "--no-pager",
            "script",
            "--header",
            f"--input={Path.home()}/perf.data"
            # "-F",
            # "tid,time,ip,sym,addr,event,phys_addr",
        ]
        ssh_all(vms, ["sudo", "pkill", "redis-server"])
        for i, out in enumerate(ssh_all(vms, perf_script)):
            print(f"vm{i} perf script:\n{out}")
    info(f"redis ycsb-{workload.name} workload complete")


# class Redis:
#     def load(self, vmid: int, perf_event: Optional[str]) -> List[str]:
#         args = ["tmux", "new", "-d", "redis-server"]
#         args += ["--save", "", "--appendonly", "no", "--protected-mode", "no"]
#         args += ["--dbfilename", YCSB_PRELOADED, "--dir", PROJECT_DIR]
#         if perf_event:
#             args += ["sudo", "perf", "record", "--all-user", "--phys-data", "--data"]
#             args += ["-z", "-vv", "-e", perf_event]
#         return args

#     def loaded(self, vmid: int) -> List[str]:
#         return ["test", f"{YCSB_RECORD_COUNT}", "-eq", "'$(redis-cli dbsize)'"]

#     def run(self, vmid: int) -> List[str]:
#         pass
