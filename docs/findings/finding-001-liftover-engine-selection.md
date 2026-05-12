# Lift-over engine selection: the `liftover` Python package over bcftools

## Context

During Phase 2, the original lift-over implementation used pure-Python
`pyliftover`, which took ~20 minutes on a 631K-variant 23andMe file.
Optimization was needed before Phase 4 (which produces ~30M imputed variants).

## Observation

Three engines were evaluated:

- **bcftools `+liftover`** would be fastest, but the `+liftover` plugin is a
  third-party Cloudflare/freeseek plugin not bundled with bcftools. On
  Ubuntu 25.x, the apt package excludes it. A source build of bcftools also
  excludes it. Building the third-party plugin from `freeseek/score` requires
  linking against bcftools' bundled htslib, which on the user's setup was built
  without `-fPIC` and refused to link into a shared object. The build chain
  failed after meaningful time investment.
- **`liftover` Python package** (CFFI-backed, drop-in replacement for
  `pyliftover`) is roughly 50–100× faster than `pyliftover` on real data, with
  zero system dependencies beyond what `uv sync` installs. ~4 seconds for 631K
  variants on macOS.
- **`pyliftover`** remains available as an explicit fallback.

## Implication

The default lift-over engine is `liftover`. The `BcftoolsLiftover` class was
removed from the codebase (preserved in closed PR #8 if ever needed). The
`Liftover` Protocol still supports alternative engines if a working bcftools
`+liftover` setup ever exists.

## Follow-up

None. Phase 4's TopMed output is GRCh38-native, so lift-over doesn't apply
there. If a future phase needs to lift millions of variants, `liftover` should
scale acceptably (linear extrapolation suggests ~3 minutes for 30M variants).
