import atexit
from enum import Enum
from itertools import cycle
from pathlib import Path

import libvirt
from numa.info import node_to_cpus

DEFAULT_IMAGE = "clr.img"
DEFAULT_KERNEL = "vmlinux.bin"
DEFAULT_CMDLINE = " ".join(
    [
        "psi=1",
        "root=/dev/vda2",
        "rw",
        "rootfstype=ext4,btrfs,xfs,f2fs",
        "console=hvc0",
        "console=ttyS0,115200n8",
        "console=tty0",
        "module.sig_enforce=0",
        "mitigations=off",
        "cryptomgr.notests",
        "quiet",
        "init=/usr/lib/systemd/systemd-bootchart",
        "initcall_debug",
        "no_timer_check",
        "tsc=reliable",
        "noreplace-smp",
        "page_alloc.shuffle=1",
    ]
)

# directory structure
# this dir is mapped to the guest for easy access to supplementary data
PROJECT_DIR = Path.home() / "Projects/ch-test"
SHARED_DIR = PROJECT_DIR / ".."
CLOUD_HYPERVISOR = PROJECT_DIR / "base/cloud-hypervisor"
VIRTIOFSD = PROJECT_DIR / "base/virtiofsd"
CH_REMOTE = PROJECT_DIR / "base/ch-remote"
GO_YCSB = PROJECT_DIR / "base/go-ycsb"
MODULES_DIR = PROJECT_DIR / "base"
VM_WORKING_DIR = PROJECT_DIR

DEFAULT_SSH_ARGS = [
    "ssh",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=1",
    "-o",
    "LogLevel=ERROR",
]
DEFAULT_NETWORK_CONFIG = """
    <network>
      <name>default</name>
      <bridge name='virbr0'/>
      <forward/>
      <ip address='192.168.122.1' netmask='255.255.255.0'>
        <dhcp>
          <range start='192.168.122.2' end='192.168.122.254'/>
        </dhcp>
      </ip>
    </network>
"""
YCSB_RECORD_COUNT = 3000000
YCSB_OPERATION_COUNT = 5000000
YCSB_PRELOADED = f"ycsb-{YCSB_RECORD_COUNT}.rdb"
YCSB_WORKLOAD_ARGS = dict(
    A=[
        "-p",
        "workload=core",
        "-p",
        "readallfields=true",
        "-p",
        "readproportion=0.5",
        "-p",
        "updateproportion=0.5",
        "-p",
        "scanproportion=0",
        "-p",
        "insertproportion=0",
        "-p",
        "requestdistribution=uniform",
    ]
)


def host_cpu_cycler():
    return cycle(node_to_cpus(0))


ENV_SETUP_CMDS = dict(
    pmem="sudo daxctl reconfigure-device --human --mode=system-ram all",
    network="sudo systemctl --no-pager --full start libvirtd",
    cpu="echo 3000000 | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq",
    numa="sudo sysctl -w kernel.numa_balancing=0",
)

VCPUBind = Enum("VCPUBind", ["CORE", "NODE"])
Benchmark = Enum("Benchmark", ["MANUAL", "REDIS"])
YcsbWorkload = Enum("YcsbWorkload", ["A", "B", "C", "D", "E", "F"])


class LogLevel(Enum):
    CRITICAL = 50
    ERROR = 40
    WARNING = 30
    INFO = 20
    DEBUG = 10
    NOTSET = 0
