from logging import info
from rich import print

from .opt import Opt
from .utils import log
from .vm import launch_vms, insmod, numa_balancing, Vm
from .config import host_cpu_cycler, Benchmark
from .benchmark.redis import redis


def main(opt: Opt):
    # https://stackoverflow.com/a/44401529
    log(opt.log_level)
    with launch_vms(
        opt.num,
        ncpus=opt.ncpus,
        memory=opt.memory,
        dram_ratio=opt.dram_ratio,
        memory_mode=opt.memory_mode,
        vcpubind=opt.bind,
        cpu_cycler=host_cpu_cycler(),
    ) as vms:
        info("all vm started")
        insmod(vms)
        numa_balancing(vms, False)
        match opt.bench:
            case Benchmark.MANUAL:
                info("wait for manual termination via pkill cloud-hyperviso")
                list(map(Vm.wait, vms))
            case Benchmark.REDIS:
                info("benchmark to run: redis")
                redis(opt, vms)
                info("benchmark finished: redis")
            case _:
                assert False, "Unreachable"
