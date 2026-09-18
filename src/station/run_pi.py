from station.runner import run_cli

def main():
    from station.prototypes.pi.prototype import PiPrototype

    return run_cli(
        prog_name="station.run_pi",
        default_type="subscription",
        prototype_class=PiPrototype,
        prototype_kind="pi",
    )

if __name__ == "__main__":
    raise SystemExit(main())
