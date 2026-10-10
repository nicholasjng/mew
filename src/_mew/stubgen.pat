# nanobind stubgen patterns applied to src/mew/_core.pyi.

# Drop the `__str__ = __repr__` alias stubgen emits for IntFlag enums, which it
# writes *before* the `def __repr__` it aliases (ty: unresolved-reference).
CounterFlags\.__str__:

# The iterator protocol is bound through type slots, which stubgen sees as
# untyped slot wrappers; restore the typed signatures.
State\.__iter__:
    def __iter__(self) -> State: ...

State\.__next__:
    def __next__(self) -> None: ...

BatchIter\.__iter__:
    def __iter__(self) -> BatchIter: ...

BatchIter\.__next__:
    def __next__(self) -> int: ...
