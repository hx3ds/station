from station.runner import run_cli

def main():
    from station.prototypes.hermes.prototype import HermesPrototype

    return run_cli(
        prog_name="station.run_hermes",
        default_type="subscription",
        prototype_class=HermesPrototype,
        prototype_kind="hermes",
    )

if __name__ == "__main__":
    raise SystemExit(main())
