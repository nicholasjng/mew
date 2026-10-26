"""Measure warm JIT execution, including dispatch and synchronization."""

from pathlib import Path

import jax
import jax.numpy as jnp

import mew

repo = (Path(__file__).resolve().parent / "../jax-history").resolve()
if not Path(jax.__file__).resolve().is_relative_to(repo):
    raise RuntimeError(f"Expected JAX from {repo}, got {jax.__file__}")


@mew.benchmark(name="jax_matmul", use_real_time=True)
def bench_matmul(state: mew.State) -> None:
    x = jnp.arange(512 * 512, dtype=jnp.float32).reshape(512, 512) / (512 * 512)
    x.block_until_ready()
    matmul = jax.jit(lambda a: a @ a)
    matmul(x).block_until_ready()
    for _ in state:
        matmul(x).block_until_ready()
