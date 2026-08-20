# Hardware scaling benchmark (Linux kernel)

How CodeLoom's C engine (`--engine c`) performs on the Linux kernel across
CPU-resource configurations. All runs: torvalds/linux shallow clone,
64,814 code files, ~28M LOC. Same Apple M3 Pro (11 cores, 18 GB, NVMe).

## Results

| Config | Full kernel index |
|---|---|
| Full cores (default, up to 8 workers) | **89s** |
| 2 cores (`CODELOOM_CORES=2`) | **97s** |
| 1 core / serial (`CODELOOM_CORES=1`) | **113s** |
| **Throttled**: `nice -n 20` + 1 core (starved CPU) | **106s** |

For context, the pure-Python default (`--engine py`) symbol index alone is
~62s and the old pre-C-engine full index was 12+ minutes.

## Why core count matters little

The C scan is **subprocess-bound**, not Python-worker-shared. Each C-core
process runs independently reading + parsing files, so even 2 workers do most
of the work in parallel with each other. Cutting 11→2 workers cost only ~8s;
going fully serial cost ~24s. The C core's raw per-file throughput is so high
that even starved-and-serial stays under 3 minutes.

## Reproduce

```bash
# full cores
python3 codeloom.py --index --engine c --max-files 100000 .

# low-core / serial simulation (any machine)
CODELOOM_CORES=1 python3 codeloom.py --index --engine c --max-files 100000 .

# throttled (lowest scheduling priority + serial)
nice -n 20 env CODELOOM_CORES=1 python3 codeloom.py --index --engine c --max-files 100000 .
```

`CODELOOM_CORES` caps the parallel C-scan workers (1–8). It lets anyone
reproduce the low-core measurements without changing hardware.

## Honest limits

- `nice -n 20` starves scheduling (a real throughput penalty) but does NOT
  lower the CPU clock or replicate older/slower cores, RAM, or SSD.
- A literal low-end laptop would run slower per-core than the M3 Pro used
  here; the absolute kernel-build time would rise, but the design degrades
  gracefully (no collapse toward the old 12-minute baseline).
- The every-day agent workflow (`--search`, `--get-symbol`, `--loom`,
  `--pack`, `--resume`) is lazy and needs no full index, so it runs fine on
  low-end hardware regardless.
