from station.runner import run_cli

def main():
    from station.prototypes.opencode.prototype import OpenCodePrototype

    return run_cli(
        prog_name="station.run_opencode",
        default_type="subscription",
        prototype_class=OpenCodePrototype,
        prototype_kind="opencode",
    )

if __name__ == "__main__":
    raise SystemExit(main())
