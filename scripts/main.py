from logging import info

from bencher import main
from rich import inspect, print


if __name__ == "__main__":
    opt = Opt.from_args()
    inspect(opt)
    main(opt)
