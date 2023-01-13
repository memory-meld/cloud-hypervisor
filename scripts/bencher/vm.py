from contextlib import ExitStack, contextmanager
from itertools import cycle
from logging import info
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen, check_output
from time import sleep
from typing import Iterable, List

from numa.info import node_to_cpus
# from rich import print

from .config import (CLOUD_HYPERVISOR, DEFAULT_CMDLINE, DEFAULT_IMAGE,
                     DEFAULT_KERNEL, DEFAULT_SSH_ARGS, MODULES_DIR, SHARED_DIR,
                     VIRTIOFSD, VM_WORKING_DIR, VCPUBind)
from .utils import take, wait_for_exit


class Vm:
    id: int
    ncpus: int = 4
    memory: int = 8 << 30
    dram_ratio: float = 0.2
    memory_mode: bool = False
    vcpubind: VCPUBind = VCPUBind.CORE
    cpu_cycler: Iterable[int]
    virtiofsd: Popen
    cloud_hypervisor: Popen
    ch_args: List[str] = None

    def __init__(
        self,
        id: int,
        ncpus: int = 4,
        memory: int = 8 << 30,
        dram_ratio: float = 0.2,
        memory_mode: bool = False,
        vcpubind: VCPUBind = VCPUBind.CORE,
        cpu_cycler: Iterable[int] = cycle(node_to_cpus(0)),
    ):
        self.id = id
        self.ncpus = ncpus
        self.memory = memory
        self.dram_ratio = dram_ratio
        self.memory_mode = memory_mode
        self.vcpubind = vcpubind
        self.cpu_cycler = cpu_cycler

    def cwd(self) -> Path:
        return VM_WORKING_DIR / f"vm{self.id}"

    def ip(self) -> str:
        return f"192.168.122.{self.id+166}"

    def mac(self) -> str:
        return f"2e:89:a8:e4:b9:{self.id+0x48:02x}"

    def virtiofsd_args(self) -> List[str]:
        return [
            VIRTIOFSD,
            "--cache=never",
            "--socket-path=virtiofsd.socket",
            f"--shared-dir={SHARED_DIR}",
        ]

    def affinity(self, to_string=False) -> List[List[int]] | str:
        match self.vcpubind:
            case VCPUBind.CORE:
                ret = list(map(lambda x: [x], take(self.ncpus, self.cpu_cycler)))
            case VCPUBind.NODE:
                ret = list(map(lambda _: node_to_cpus(0), range(self.ncpus)))
            case _:
                assert False, "Unreachable code"
        return (
            ",".join([f"{g}@{h}" for g, h in enumerate(ret)]).replace(" ", "")
            if to_string
            else ret
        )

    def dram(self) -> int:
        return int(self.memory * self.dram_ratio)

    def pmem(self) -> int:
        return self.memory - self.dram()

    def pmem_node(self) -> int:
        return 0 if self.memory_mode else 2

    def cloud_hypervisor_args(self) -> List[str]:
        if self.ch_args is None:
            self.ch_args = list(
                map(
                    str,
                    [
                        CLOUD_HYPERVISOR,
                        "--api-socket",
                        "path=cloud-hypervisor.socket",
                        "--kernel",
                        DEFAULT_KERNEL,
                        "--cmdline",
                        DEFAULT_CMDLINE,
                        "--fs",
                        "tag=Projects,socket=virtiofsd.socket",
                        "--disk",
                        f"path={DEFAULT_IMAGE}",
                        "--cpus",
                        f"boot={self.ncpus},affinity=[{self.affinity(True)}]",
                        "--net",
                        f"tap=ich{self.id},mac={self.mac()}",
                        "--balloon",
                        f"size=[{self.pmem()},{self.dram()}],statistics=on,heterogeneous_memory=on",
                        "--memory",
                        "size=0,shared=on",
                        "--memory-zone",
                        f"size={self.memory},shared=on,host_numa_node=0,id=fast",
                        f"size={self.memory},shared=on,host_numa_node={self.pmem_node()},id=slow",
                        "--numa",
                        f"guest_numa_id=0,cpus=0-{self.ncpus-1},memory_zones=fast",
                        "guest_numa_id=1,memory_zones=slow",
                    ],
                )
            )
        return self.ch_args

    def launch(self):
        self.virtiofsd = Popen(
            self.virtiofsd_args(), cwd=self.cwd(), stdout=PIPE, stderr=PIPE
        )
        sleep(1)
        self.cloud_hypervisor = Popen(
            self.cloud_hypervisor_args(), cwd=self.cwd(), stdout=PIPE, stderr=PIPE
        )

    def kill(self):
        self.cloud_hypervisor.terminate()
        out, err = wait_for_exit(self.cloud_hypervisor)
        print(f"vm{self.id} cloud-hypervisor args:\n{self.cloud_hypervisor_args()}")
        print(
            f"vm{self.id} cloud-hypervisor stdout:\n{out}\nvm{self.id} cloud-hypervisor stderr:\n{err}"
        )
        self.virtiofsd.terminate()
        self.virtiofsd.wait()
        try:
            [
                (self.cwd() / f).unlink()
                for f in ["virtiofsd.socket", "cloud-hypervisor.socket"]
            ]
        except FileNotFoundError:
            pass
        # print("vm cleaned up")

    def ssh(self, args: List[str], **kwargs) -> str:
        """Run commands via SSH in the `id`-th VM."""
        return check_output(
            DEFAULT_SSH_ARGS + [self.ip()] + args,
            **kwargs,
        ).decode("utf-8")

    def wait_for_boot(self):
        ssh = " ".join(DEFAULT_SSH_ARGS + [self.ip(), "uname", "-a"])
        check_output(
            f"until {ssh}; do sleep 1; done",
            shell=True,
            stderr=DEVNULL,
        )

    def wait(self):
        wait_for_exit(self.cloud_hypervisor)

    def __enter__(self):
        self.launch()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.kill()
        return True


def insmod(vms: List[Vm]):
    modules = ["balloon_events.ko", "virtio_balloon.ko"]
    module_args = [[], ["pebs_enabled=false"]]
    for vm in vms:
        for mod, args in zip(modules, module_args):
            vm.ssh(["sudo", "insmod", f"{MODULES_DIR}/{mod}"] + args)
    info("balloon module installed")


def numa_balancing(vms: List[Vm], on: bool = False):
    for vm in vms:
        vm.ssh(["sudo", "sysctl", "-w", f"kernel.numa_balancing={int(on)}"])
    info(f"guest kernel.numa_balancing enabled: {on}")


def ssh_all(vms: List[Vm], args, **kwargs) -> List[str]:
    return [vm.ssh(args, **kwargs) for vm in vms]


@contextmanager
def launch_vms(nguests: int, **kwargs) -> List[Vm]:
    from .net import network

    vms = [Vm(id, **kwargs) for id in range(nguests)]
    with network(vms), ExitStack() as stack:
        readyvms = list(map(stack.enter_context, vms))
        list(map(Vm.wait_for_boot, vms))
        try:
            yield readyvms
        finally:
            pass
