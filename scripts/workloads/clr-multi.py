#!/usr/bin/env python3
import argparse
import time
from contextlib import ExitStack, contextmanager
from functools import reduce
import logging
from logging import info
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen, check_output, run
from typing import List, Tuple

from numa.info import node_to_cpus

# pip3 install --upgrade libvirt-python py-libnuma


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

# this dir is mapped to the guest for easy access to supplementary data
PROJECT_DIR = "/home/jlhu/Projects"
DEFAULT_TEST_DIR = PROJECT_DIR + "/ch-test"
CLOUD_HYPERVISOR = DEFAULT_TEST_DIR + "/base/cloud-hypervisor"
VIRTIOFSD = DEFAULT_TEST_DIR + "/base/virtiofsd"
CH_REMOTE = DEFAULT_TEST_DIR + "/base/ch-remote"
GO_YCSB = DEFAULT_TEST_DIR + "/base/go-ycsb"
DEFAULT_SSH_ARGS = [
    "ssh",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=1",
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
YCSB_A_ARGS = [
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


def get_cwd(id: int) -> str:
    return DEFAULT_TEST_DIR + f"/vm{id}"


def get_ip(id: int) -> str:
    return f"192.168.122.{id+166}"


def get_mac(id: int) -> str:
    return f"2e:89:a8:e4:b9:{id+0x48:02x}"


@contextmanager
def network(num: int = 40):
    """Create dhcp static ip assignment and tap device for vm0 up to vm89."""
    import libvirt
    from libvirt import VIR_NETWORK_SECTION_IP_DHCP_HOST as IP_DHCP_HOST
    from libvirt import VIR_NETWORK_UPDATE_AFFECT_CONFIG as CONFIG
    from libvirt import VIR_NETWORK_UPDATE_AFFECT_LIVE as LIVE
    from libvirt import VIR_NETWORK_UPDATE_COMMAND_ADD_LAST as ADD
    from libvirt import VIR_NETWORK_UPDATE_COMMAND_DELETE as DEL
    from libvirt import libvirtError

    # from xml.dom import minidom
    # user should be in the libvirt group
    conn = libvirt.open("qemu:///system")
    # we make use of the libvirt default nat network 192.168.122.1/24
    net = conn.networkLookupByName("default")
    assert num < 90, f"The IP range is not large enough for {num} VMs"
    for i in range(0, num):
        mac = get_mac(i)
        ip = get_ip(i)
        xml = f"<host mac='{mac}' ip='{ip}' />"
        try:
            net.update(ADD, IP_DHCP_HOST, -1, xml, LIVE | CONFIG)
        except libvirtError:
            pass
        tap = f"ich{i}"
        run(["sudo", "ip", "tuntap", "add", tap, "mode", "tap"])
        run(["sudo", "brctl", "addif", "virbr0", tap])
    try:
        yield conn
    finally:
        for i in range(0, num):
            mac = get_mac(i)
            ip = get_ip(i)
            xml = f"<host mac='{mac}' ip='{ip}' />"
            try:
                net.update(DEL, IP_DHCP_HOST, -1, xml, LIVE | CONFIG)
            except libvirtError:
                pass
            tap = f"ich{i}"
            run(["sudo", "ip", "tuntap", "del", tap, "mode", "tap"])
        conn.close()
        # print("network cleaned up")


@contextmanager
def pmem():
    """Convert all devdax PMEM into system ram."""
    run(
        "echo 3 | sudo tee /proc/sys/vm/drop_caches",
        shell=True,
        capture_output=True,
        check=True,
    )
    run(
        ["sudo", "daxctl", "reconfigure-device", "--human", "--mode=system-ram", "all"],
        capture_output=True,
        check=True,
    )
    try:
        yield None
    finally:
        # TODO: put PMEM back into devdax mode
        # print("pmem cleaned up")
        pass


@contextmanager
def create_vm(id: int, ncpus: int = 4, mem: int = 8 << 30, dram_ratio=0.5):
    r"""
    Start a vm.

    Expected directory structure: `TEST_DIR/{vm\d+,base}`.
    Will lanch `virtiofsd` then `cloud-hypervisor` and create
    corresponding socket file `{virtiofsd,cloud-hypervisor}.socket`.
    To use this on a list of VMs, see
    [`contextlib.ExitStack()`](https://stackoverflow.com/a/45681273).
    """
    cwd = get_cwd(id)
    host_cpus = ",".join(map(str, node_to_cpus(0)))
    affinity = ",".join([f"{i}@[{host_cpus}]" for i in range(ncpus)])
    mac = get_mac(id)
    assert 0.0 <= dram_ratio <= 1.0, f"Unexpected dram_ratio: {dram_ratio}"
    dram = int(mem * dram_ratio)
    pmem = mem - dram
    fs_args = [
        VIRTIOFSD,
        "--cache=never",
        "--socket-path=virtiofsd.socket",
        f"--shared-dir={PROJECT_DIR}",
    ]
    ch_args = [
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
        f"boot={ncpus},affinity=[{affinity}]",
        "--net",
        f"tap=ich{id},mac={mac}",
        "--balloon",
        f"size=[{mem-dram},{mem-pmem}],statistics=on,heterogeneous_memory=on",
        "--memory",
        "size=0,shared=on",
        "--memory-zone",
        f"size={mem},shared=on,host_numa_node=0,id=fast",
        f"size={mem},shared=on,host_numa_node=2,id=slow",
        "--numa",
        f"guest_numa_id=0,cpus=0-{ncpus-1},memory_zones=fast",
        "guest_numa_id=1,memory_zones=slow",
    ]
    virtiofsd = Popen(fs_args, cwd=cwd, stdout=PIPE, stderr=PIPE)
    time.sleep(1)
    ch = Popen(ch_args, cwd=cwd, stdout=PIPE, stderr=PIPE)

    try:
        yield ch
    finally:
        ch.terminate()
        out, err = wait_for_exit([ch])[0]
        print(
            f"vm{id} cloud-hypervisor stdout:\n{out}\nvm{id} cloud-hypervisor stderr:\n{err}"
        )
        virtiofsd.terminate()
        virtiofsd.wait()

        try:
            [
                Path(cwd + "/" + f).unlink()
                for f in ["virtiofsd.socket", "cloud-hypervisor.socket"]
            ]
        except FileNotFoundError:
            pass
        # print("vm cleaned up")


def ssh_cmd(id: int, args: List[str], **kwargs) -> str:
    """Run commands via SSH in the `id`-th VM."""
    return check_output(
        DEFAULT_SSH_ARGS + [get_ip(id)] + args,
        **kwargs,
    ).decode("utf-8")


def subcmd_redis(args, vms: List[Popen]):
    server_args = [
        "tmux",
        "new",
        "-d",
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
        DEFAULT_TEST_DIR,
    ]
    for id in range(args.num):
        # launch redis with preloaded ycsb keys in the background
        ssh_cmd(id, server_args, stderr=DEVNULL)
    info("all redis servers started")
    # redis taks at least 30s to load the data, we can try to query dbsize later
    time.sleep(15)
    # wait for loading
    while not reduce(
        bool.__and__,
        map(
            lambda i: ssh_cmd(i, ["redis-cli", "dbsize"], stderr=DEVNULL).strip()
            == f"{YCSB_RECORD_COUNT}",
            range(args.num),
        ),
    ):
        time.sleep(1)
    info("ycsb preload complelte")
    # run ycsb on node 1 to prevent interference with VM running on node 0

    ycsb_args = [
        "numactl",
        "--cpunodebind=1",
        "--membind=1",
        "--",
        GO_YCSB,
        "run",
        "redis",
        "-p",
        f"recordcount={YCSB_RECORD_COUNT}",
        "-p",
        f"operationcount={YCSB_OPERATION_COUNT}",
        "-p",
        f"threadcount={args.ncpus}",
    ]
    match args.workload:
        case "a":
            ycsb_args += YCSB_A_ARGS
        case _:
            assert False, f"Workload not implemented yet: {args.workload}"

    jobs = [
        Popen(
            ycsb_args + ["-p", f"redis.addr={get_ip(id)}:6379"],
            cwd=get_cwd(id),
            stdout=PIPE,
            stderr=PIPE,
        )
        for id in range(args.num)
    ]
    for i, (out, err) in enumerate(wait_for_exit(jobs)):
        print(f"vm{i} stdout:\n{out}\nvm{i} stderr:\n{err}")
    info(f"redis ycsb-{args.workload} workload complelte")


# return (stdout, stderr) of all subprocesses
def wait_for_exit(subprocesses: List[Popen]) -> List[Tuple[str, str]]:
    """Call communicate() and return str (stdout, stderr) for each process."""
    return [
        (
            "" if out is None else out.decode("utf-8"),
            "" if err is None else err.decode("utf-8"),
        )
        for (out, err) in map(Popen.communicate, subprocesses)
    ]


def wait_for_boot(num: int):
    for i in range(num):
        check_output(
            f"until {' '.join(DEFAULT_SSH_ARGS)} {get_ip(i)} uname -a; do sleep 1; done",
            shell=True,
            stderr=DEVNULL,
        )
    info("all VM booted")


def main(args):
    # https://stackoverflow.com/a/44401529
    logging.basicConfig(
        format="%(asctime)s,%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d:%H:%M:%S",
        level=logging.getLevelName(args.log_level),
    )
    with pmem(), network(args.num), ExitStack() as stack:
        vms = [
            stack.enter_context(create_vm(i, args.ncpus, args.mem, args.dram_ratio))
            for i in range(args.num)
        ]
        wait_for_boot(args.num)
        match args.subcmd:
            case "manual":
                wait_for_exit(vms)
            case "redis":
                subcmd_redis(args, vms)
            case _:
                assert False, f"Subcmd not implemented: {args.subcmd}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--num", "-n", type=int, required=True, help="How many VMs to launch"
    )
    parser.add_argument(
        "--ncpus",
        "-c",
        type=int,
        default=4,
        help="How many vCPUs for each VM, defaults to 4",
    )
    parser.add_argument(
        "--mem",
        "-m",
        type=int,
        default=8 << 30,
        help="How memory in byte for each VM, defaults to 8G",
    )
    parser.add_argument(
        "--dram-ratio",
        "-d",
        type=float,
        default=0.5,
        help="Initial DRAM ratio out of all system-ram, defaults to 0.5",
    )
    parser.add_argument(
        "--log-level",
        "-l",
        default="INFO",
        type=str.upper,
        choices=["FATAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "NOTSET"],
        help="Logging level, defaults to INFO",
    )
    # https://stackoverflow.com/a/4575792
    subcmd = parser.add_subparsers(
        dest="subcmd", title="workloads", description="valid workloads"
    )
    redis_parser = subcmd.add_parser("redis")
    redis_parser.add_argument(
        "--workload",
        "-w",
        default="a",
        choices=["a", "b", "c", "d", "e", "f"],
        help="Which ycsb workload to run on redis, defaults to a",
    )
    mix_parser = subcmd.add_parser("mix")
    manual_parser = subcmd.add_parser("manual")
    main(parser.parse_args())
