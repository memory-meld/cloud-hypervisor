#!/usr/bin/env python3
import os
from itertools import product
from subprocess import run
from datetime import datetime
from pathlib import Path
import argparse
from parse import parse
import re

# pip3 install --upgrade parse

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def main(args):
    print(
        "subcmd,datasest,workload,num,ncpus,mem,ratio,read_ops,read_lat,read_p99,update_ops,update_lat,update_p99"
    )
    for log in args.dir.rglob("*.log"):
        subcmd, dataset, workload, num, ncpus, mem, ratio = parse(
            "{}-{}-{}-{:d}-{:d}-{:d}-{:f}.log", log.name
        )
        r = re.compile(
            r"^Run finished, takes.*\n(?P<read>READ.*)\n(?P<update>UPDATE.*)\n",
            re.MULTILINE,
        )
        trops, trlat, trp99 = 0, 0, 0
        tuops, tulat, tup99 = 0, 0, 0
        for (read, update) in r.findall(log.read_text()):
            rtime, rcnt, rops, ravg, rmin, rmax, rp99, rp39, rp49 = parse(
                "READ   - Takes(s): {:f}, Count: {:d}, OPS: {:f}, Avg(us): {:d}, Min(us): {:d}, Max(us): {:d}, 99th(us): {:d}, 99.9th(us): {:d}, 99.99th(us): {:d}",
                read,
            )
            utime, ucnt, uops, uavg, umin, umax, up99, up39, up49 = parse(
                "UPDATE - Takes(s): {:f}, Count: {:d}, OPS: {:f}, Avg(us): {:d}, Min(us): {:d}, Max(us): {:d}, 99th(us): {:d}, 99.9th(us): {:d}, 99.99th(us): {:d}",
                update,
            )
            trops += rops
            trlat += ravg
            trp99 += rp99
            tuops += uops
            tulat += uavg
            tup99 += up99
        trops, trlat, trp99, tuops, tulat, tup99 = map(
            lambda x: x / num, [trops, trlat, trp99, tuops, tulat, tup99]
        )
        print(
            f"{subcmd},{dataset},{workload},{num},{ncpus},{mem},{ratio},{trops},{trlat},{trp99},{tuops},{tulat},{tup99}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dir", "-d", type=Path, help="Directory to where the runner output its output"
    )
    main(parser.parse_args())
