import time
from functools import reduce
from logging import info
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import List

from numa.info import node_to_cpus
from rich import print

from ..config import (GO_YCSB, PROJECT_DIR, YCSB_OPERATION_COUNT,
                      YCSB_PRELOADED, YCSB_RECORD_COUNT, YCSB_WORKLOAD_ARGS)
from ..opt import Opt
from ..utils import wait_for_exit_all
from ..vm import Vm, ssh_all


def redis(opt: Opt, vms: List[Vm]):
    tmux = [
        "tmux",
        "new",
        "-d",
    ]
    redis_server = [
        "redis-server",
        "--save",
        "",
        "--appendonly",
        "no",
        "--protected-mode",
        "no",
        "--dbfilename",
        YCSB_PRELOADED,
        "--dir",
        PROJECT_DIR,
    ]
    perf = [
        "sudo",
        "perf",
        "record",
        "--count=1",
        "--all-user",
        "--phys-data",
        "--data",
        "-z",
        "-vv",
        "-e",
    ]
    redis_server = tmux + ((perf + [opt.perf]) if opt.perf else []) + redis_server
    info(redis_server)
    # launch redis with preloaded ycsb keys in the background
    ssh_all(vms, redis_server, stderr=DEVNULL)
    info("all redis servers started")
    # redis taks at least 30s to load the data, we can try to query dbsize later
    time.sleep(15)
    # wait for loading
    while not reduce(
        bool.__and__,
        map(
            lambda vm: vm.ssh(["redis-cli", "dbsize"], stderr=DEVNULL).strip()
            == f"{YCSB_RECORD_COUNT}",
            vms,
        ),
    ):
        time.sleep(1)
    info("ycsb preload complelte")
    # run ycsb on node 1 to prevent interference with VM running on node 0

    go_ycsb = [
        "numactl",
        "--physcpubind=" + ",".join(map(str, node_to_cpus(1))),
        f"--membind={0 if opt.memory_mode else 2}",
        "--"
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
        f"threadcount={opt.ncpus}",
    ]
    go_ycsb += YCSB_WORKLOAD_ARGS[opt.workload.name]

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
    if opt.perf:
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
    info(f"redis ycsb-{opt.workload.name} workload complete")
