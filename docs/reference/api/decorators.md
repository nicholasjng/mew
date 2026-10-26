# Decorators

Each decorator registers a function at import time and returns it unchanged.
Apply one registration decorator per function.
Options are checked at registration, where a wrong type raises `TypeError` and an invalid or contradictory value raises `ValueError`.

```{eval-rst}
.. currentmodule:: mew

.. autofunction:: benchmark
.. autofunction:: parametrize
.. autofunction:: grid
```

## Options

```{eval-rst}
.. autoclass:: mew.api.BenchmarkOptions
   :members:
```
