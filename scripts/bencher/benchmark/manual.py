from logging import info
from subprocess import DEVNULL, run
from typing import List
from tempfile import NamedTemporaryFile

from ..config import PROJECT_DIR
from ..vm import Vm, ssh_all


def manual(vms: List[Vm], cmd: str, wait: bool):
    # run for ~3mins i.e. 5 trials
    with NamedTemporaryFile(mode="w+", prefix=f"{PROJECT_DIR}/") as script:
        script.write(cmd)
        script.flush()
        run(["chmod", "a+rw", script.name])
        for i, out in enumerate(ssh_all(vms, ["bash", script.name], stderr=DEVNULL, check=False)):
            print(f"vm{i} cmd:\n{out}")
        if wait:
            info("wait for manual termination via pkill cloud-hyperviso")
            list(map(Vm.wait, vms))
