"""Harness RAM sampler (root, untraced, started by entry.sh just before the traced run).

usage: python -I -B ram_sampler.py OUT_FILE INTERVAL_SECONDS

Appends ``<unix_ts> <MemAvailable kiB>`` lines from /proc/meminfo (the container VM figure:
owner reading UC-07_memory) every INTERVAL seconds until SIGTERM, then one last reading and
``#end``. A read failure writes ``#error ...`` and exits 1, so the analyzer reports the field
MISSING instead of a partial minimum. One process, no children: nothing here forks, so a
group or -1 signal from the traced tree has exactly one harness pid to resolve.
"""

import signal
import sys
import time


def mem_available_kib():
    with open("/proc/meminfo", encoding="ascii") as stream:
        for line in stream:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    raise RuntimeError("MemAvailable not in /proc/meminfo")


def main(argv):
    out, interval = argv[1], float(argv[2])
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(True))
    with open(out, "w", encoding="ascii", buffering=1) as stream:
        stream.write(f"#start interval={interval}\n")
        try:
            while True:
                stream.write(f"{time.time():.6f} {mem_available_kib()}\n")
                if stop:
                    break
                time.sleep(interval)
        except Exception as exc:  # noqa: BLE001 - recorded; the analyzer makes the field MISSING
            stream.write(f"#error {type(exc).__name__}: {exc}\n")
            return 1
        stream.write("#end\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
