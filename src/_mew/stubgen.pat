# nanobind stubgen patterns applied to src/mew/_core.pyi.

# Drop the `__str__ = __repr__` alias stubgen emits for IntFlag enums, which it
# writes *before* the `def __repr__` it aliases (ty: unresolved-reference).
CounterFlags\.__str__:
