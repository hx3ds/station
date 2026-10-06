from station.runner import run_cli


def main():
    from station.prototypes.cursor.prototype import CursorPrototype

    return run_cli(
        prog_name="station.run_cursor",
        default_type="subscription",
        prototype_class=CursorPrototype,
        prototype_kind="cursor",
    )


if __name__ == "__main__":
    raise SystemExit(main())
