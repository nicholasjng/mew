"""Record the revision under test and the JAX build in every run."""

import hashlib
from pathlib import Path

import jax
import jaxlib

import mew

harness = Path(__file__).resolve().parent
repo = (harness / "../jax-history").resolve()


def context() -> dict:
    return {
        **mew.vcs_context(repo),
        "jax": {
            "version": jax.__version__,
            "jaxlib_version": jaxlib.__version__,
            "backend": jax.default_backend(),
            "devices": [str(device) for device in jax.devices()],
        },
        "suite": {"sha256": hashlib.sha256((harness / "bench_jax.py").read_bytes()).hexdigest()},
    }
