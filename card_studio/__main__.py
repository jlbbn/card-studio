import sys

from .diagnostics import configure_logging

configure_logging()

if "--cli" in sys.argv:
    from .cli import main

    arguments = sys.argv[1:]
    arguments.remove("--cli")
    raise SystemExit(main(arguments))

from .app import main

raise SystemExit(main())
