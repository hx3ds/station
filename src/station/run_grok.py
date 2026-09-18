from station.runner import run_cli

def main():
    from station.prototypes.grok.prototype import GrokPrototype

    return run_cli(
        prog_name="station.run_grok",
        default_type="subscription",
        prototype_class=GrokPrototype,
        prototype_kind="grok",
    )

if __name__ == "__main__":
    raise SystemExit(main())
