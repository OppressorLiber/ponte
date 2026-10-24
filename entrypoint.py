import sys

if __name__ == "__main__":
    if "--apply-update" in sys.argv:
        from pontelan.update_helper import main
        raise SystemExit(main(sys.argv[sys.argv.index("--apply-update") + 1]))
    from pontelan.__main__ import main
    raise SystemExit(main())
