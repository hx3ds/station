from station.runner import run_cli

def main() -> int:
    from station.prototypes.prototype import Prototype

    return run_cli(
        prog_name="station.run_station",
        prototype_class=Prototype,
        prototype_kind="station",
    )

if __name__ == "__main__":
    raise SystemExit(main())
