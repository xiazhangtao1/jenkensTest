"""Exercise logical lcore bounds without NICs, hugepages or a 256-CPU host."""
import os
import subprocess
import sys

binary, logical = sys.argv[1], int(sys.argv[2])
allowed = sorted(os.sched_getaffinity(0))
cpu = next((item for item in allowed if 128 <= item < 256), allowed[0])
result = subprocess.run([binary, str(logical), f'--lcores={logical}@{cpu}',
    f'--main-lcore={logical}', '--no-huge', '--no-pci', '--no-shconf',
    '--no-telemetry', '--iova-mode=va', '-m64'], text=True,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
print(f'Logical lcore {logical} mapped to allowed CPU {cpu}', flush=True)
print(result.stdout, end='', flush=True)
if logical < 256:
    if result.returncode != 0 or f'RTE_MAX_LCORE=256 active_lcore={logical}' not in result.stdout:
        raise SystemExit('DPDK did not accept the valid logical lcore')
elif (result.returncode != 1 or 'DPDK_EAL_INIT_FAILED' not in result.stdout
      or 'EAL: invalid parameter for --lcores' not in result.stdout):
    raise SystemExit('DPDK did not reject the out-of-range lcore with its range diagnostic')
