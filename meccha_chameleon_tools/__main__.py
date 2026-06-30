import importlib.util
import os
import sys


def _load_updater():
    path = os.path.join(os.path.dirname(__file__), "updater.py")
    spec = importlib.util.spec_from_file_location("peterhack_updater", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    updater = _load_updater()
    if updater is not None:
        result = updater.run_startup_check(sys.argv[1:])
        if result == "restart":
            sys.exit(0)

    from meccha_chameleon_tools import main

    main()
