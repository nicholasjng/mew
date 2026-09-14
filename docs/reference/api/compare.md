# Compare

## Reading results

Two views of a result file. {func}`~mew.compare.read_results` gives the rows as stored;
{func}`~mew.compare.read_sessions` gives comparable numbers, which is usually what a
script wants.

```{eval-rst}
.. currentmodule:: mew.compare

.. autofunction:: read_results
.. autofunction:: read_sessions

.. autoclass:: Sample
   :members:

.. autoclass:: SessionData
   :members:
```

## Comparing

```{eval-rst}
.. automodule:: mew.compare
   :members:
   :exclude-members: Sample, SessionData, read_results, read_sessions
```
