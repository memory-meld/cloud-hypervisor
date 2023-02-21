import re
from pathlib import Path

from rich import print
from tap import Tap


class Args(Tap):
    file: Path  # file to be processed


def parse_gap_bc(log: str) -> list[list[float]]:
    vm = re.compile(r"(?P<tt>^vm(?P<id>\d+) gap_bc.*^Average Time:.+$)", re.S | re.M)
    trial = re.compile(r"Trial Time: \s+([\d\.]+)")
    return [[float(time) for time in trial.findall(txt)] for txt, _ in vm.findall(log)]


def parse_dmesg(log: str) -> list[list[float]]:
    vm = re.compile(r"(?P<tt>^vm(?P<id>\d+) dmesg.*?^\[\s*[\.\d]+\].*$)", re.S | re.M)
    trial = re.compile(r"Trial Time: \s+([\d\.]+)")
    return [[float(time) for time in trial.findall(txt)] for txt, _ in vm.findall(log)]


def main(args: Args):
    print(f"processing {args.file}")

    with open(args.file, "r") as f:
        content = f.read()
        trial_time = parse_gap_bc(content)
        print(trial_time)


if __name__ == "__main__":
    args = Args().parse_args()
    main(args)
