from logging import info

from rich import inspect, print

from bencher import main

if __name__ == "__main__":
    opt = Opt.from_args()
    inspect(opt)
    main(opt)
