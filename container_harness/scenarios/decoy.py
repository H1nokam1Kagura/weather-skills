# Harness-owned decoy OUTSIDE the traced tree. Reports every SIGUSR1/SIGTERM with sender pid.
import os, signal, sys, time
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1, signal.SIGTERM})
open("/work/decoy.pid", "w").write(str(os.getpid()))
time.sleep(0.5)
open("/srv/outside/decoy_untraced.txt", "w").write("d")   # untraced write (attribution test)
end = time.time() + 25
while time.time() < end:
    info = signal.sigtimedwait({signal.SIGUSR1, signal.SIGTERM}, 0.5)
    if info:
        print(f"DECOYGOT sig={info.si_signo} from_pid={info.si_pid}", flush=True)
print("DECOY alive_at_end", flush=True)
