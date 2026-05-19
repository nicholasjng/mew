# Registry

The registry is the process-global collection of registered benchmarks.
Decorators add to it; the runner reads from it.

```{eval-rst}
.. currentmodule:: mew

.. autoclass:: Registry
   :members:

.. autoclass:: Entry
   :members:

.. data:: REGISTRY

   The shared :class:`Registry` instance populated by decorators.
```
