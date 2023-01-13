from contextlib import contextmanager
from subprocess import check_output
from typing import List

import libvirt
from libvirt import VIR_NETWORK_SECTION_IP_DHCP_HOST as IP_DHCP_HOST
from libvirt import VIR_NETWORK_UPDATE_AFFECT_CONFIG as CONFIG
from libvirt import VIR_NETWORK_UPDATE_AFFECT_LIVE as LIVE
from libvirt import VIR_NETWORK_UPDATE_COMMAND_ADD_LAST as ADD
from libvirt import VIR_NETWORK_UPDATE_COMMAND_DELETE as DEL
from libvirt import virConnect, virNetwork

from .vm import Vm


class Net:
    conn: virConnect
    net: virNetwork

    def __init__(self):
        self.conn = libvirt.open("qemu:///system")
        self.net = self.conn.networkLookupByName("default")

    def __del__(self):
        self.conn.close()

    def xml(self, vm: Vm) -> str:
        return f"<host mac='{vm.mac()}' ip='{vm.ip()}' />"

    def create(self, vm: Vm):
        tap = f"ich{vm.id}"
        self.net.update(ADD, IP_DHCP_HOST, -1, self.xml(vm), LIVE | CONFIG)
        check_output(["sudo", "ip", "tuntap", "add", tap, "mode", "tap"])
        check_output(["sudo", "brctl", "addif", "virbr0", tap])

    def create_all(self, vms: List[Vm]):
        list(map(self.create, vms))

    def remove(self, vm: Vm):
        tap = f"ich{vm.id}"
        self.net.update(DEL, IP_DHCP_HOST, -1, self.xml(vm), LIVE | CONFIG)
        check_output(["sudo", "ip", "tuntap", "del", tap, "mode", "tap"])

    def remove_all(self, vms: List[Vm]):
        list(map(self.remove, vms))


@contextmanager
def network(vms: List[Vm]) -> Net:
    net = Net()
    try:
        net.create_all(vms)
        yield net
    finally:
        net.remove_all(vms)
