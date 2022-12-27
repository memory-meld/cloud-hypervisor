#!/usr/bin/env python3
import argparse
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from subprocess import Popen, run, check_output
from typing import List

from numa.info import node_to_cpus

# pip3 install --upgrade libvirt-python py-libnuma humanfriendly


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

PROJECT_DIR = "/home/jlhu/Projects"
DEFAULT_TEST_DIR = PROJECT_DIR + "/ch-test"
CLOUD_HYPERVISOR = DEFAULT_TEST_DIR + "/base/cloud-hypervisor"
VIRTIOFSD = DEFAULT_TEST_DIR + "/base/virtiofsd"
CH_REMOTE = DEFAULT_TEST_DIR + "/base/ch-remote"
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
    run(["sudo", "daxctl", "reconfigure-device", "--human", "--mode=system-ram", "all"])
    try:
        yield None
    finally:
        # TODO: put PMEM back into devdax mode
        # print("pmem cleaned up")
        pass


@contextmanager
def create_vm(id: int, ncpus: int = 4, mem: int = 8 << 30):
    r"""
    Start a vm.

    Expected directory structure: `TEST_DIR/{vm\d+,base}`.
    Will lanch `virtiofsd` then `cloud-hypervisor` and create
    corresponding socket file `{virtiofsd,cloud-hypervisor}.socket`.
    To use this on a list of VMs, see
    [`contextlib.ExitStack()`](https://stackoverflow.com/a/45681273).
    """
    cwd = DEFAULT_TEST_DIR + f"/vm{id}"
    host_cpus = ",".join(map(str, node_to_cpus(0)))
    affinity = ",".join([f"{i}@[{host_cpus}]" for i in range(ncpus)])
    mac = get_mac(id)
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
        f"size=[{mem//2},{mem//2}],statistics=on,heterogeneous_memory=on",
        "--memory",
        "size=0,shared=on",
        "--memory-zone",
        f"size={mem},shared=on,host_numa_node=0,id=fast",
        f"size={mem},shared=on,host_numa_node=2,id=slow",
        "--numa",
        f"guest_numa_id=0,cpus=0-{ncpus-1},memory_zones=fast",
        "guest_numa_id=1,memory_zones=slow",
    ]
    virtiofsd = Popen(fs_args, cwd=cwd)
    time.sleep(1)
    ch = Popen(ch_args, cwd=cwd)

    try:
        yield ch
    finally:
        ch.terminate()
        ch.wait()
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
    return check_output(["ssh", get_ip(id)] + args, **kwargs).decode("utf-8")


def main(args):
    with pmem(), network(args.num), ExitStack() as stack:
        vms = [
            stack.enter_context(create_vm(i, args.ncpus, args.mem))
            for i in range(args.num)
        ]
        match args.subcmd:
            case "manual":
                [vm.communicate() for vm in vms]
            case _:
                print(f"not implemented subcmd: {args.subcmd}")


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
    # https://stackoverflow.com/a/4575792
    subcmd = parser.add_subparsers(
        dest="subcmd", title="workloads", description="valid workloads"
    )
    redis = subcmd.add_parser("redis")
    mix = subcmd.add_parser("mix")
    manual = subcmd.add_parser("manual")
    main(parser.parse_args())
