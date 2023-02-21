from .cli import Args, main

if __name__ == "__main__":
    args = Args().parse_args()
    main(args)
