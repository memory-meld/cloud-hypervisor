import logging
from itertools import islice
from subprocess import Popen
from typing import Iterable, List, Tuple

from rich.logging import RichHandler

from .config import LogLevel


def log(level: LogLevel = LogLevel.WARNING, pretty: bool = False):
    # https://stackoverflow.com/a/44401529
    logging.basicConfig(
        format="%(asctime)s,%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d:%H:%M:%S",
        level=level.name,
        handlers=[RichHandler()] if pretty else None,
    )


# return (stdout, stderr) of all subprocesses
def wait_for_exit_all(subprocesses: List[Popen]) -> List[Tuple[str, str]]:
    """Call communicate() and return str (stdout, stderr) for each process."""
    return [
        (
            "" if out is None else out.decode("utf-8"),
            "" if err is None else err.decode("utf-8"),
        )
        for (out, err) in map(Popen.communicate, subprocesses)
    ]


def wait_for_exit(subprocess: Popen) -> Tuple[str, str]:
    """Call communicate() and return str (stdout, stderr) for each process."""
    (out, err) = subprocess.communicate()
    return (
        "" if out is None else out.decode("utf-8"),
        "" if err is None else err.decode("utf-8"),
    )


def take(n: int, iterable: Iterable):
    "Return first n items of the iterable as a list"
    return list(islice(iterable, n))
