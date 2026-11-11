"""Standard-library-only probe intended to run inside a selected Python interpreter."""
from importlib import metadata
import json
from pathlib import Path
import platform
from re import sub
from struct import calcsize
import sys


def collect_python_observation():
    distributions = []
    for distribution in metadata.distributions():
        name, version = distribution.metadata.get("Name"), distribution.version
        if not name or not version:
            raise ValueError("Installed distribution metadata is incomplete")
        distributions.append({"name": sub(r"[-_.]+", "-", name.lower()), "version": version})
    distributions.sort(key=lambda item: item["name"])
    return {
        "executable": str(Path(sys.executable).absolute()),
        "python_version": sys.version,
        "python_bits": calcsize("P") * 8,
        "dont_write_bytecode": sys.dont_write_bytecode,
        "implementation": platform.python_implementation(),
        "os_system": platform.system(),
        "os_release": platform.release(),
        "distributions": distributions,
    }


if __name__ == "__main__":
    print(json.dumps(collect_python_observation(), ensure_ascii=True, separators=(",", ":")))
