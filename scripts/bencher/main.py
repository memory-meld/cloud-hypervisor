from logging import info


from .benchmark.redis import redis
from .config import Benchmark, host_cpu_cycler
from .opt import Opt
from .utils import log
from .vm import Vm, insmod, launch_vms, numa_balancing


def main(opt: Opt):
    # https://stackoverflow.com/a/44401529
    log(opt.log_level, opt.pretty)
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
