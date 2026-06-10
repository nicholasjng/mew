# nanobind stubgen patterns applied to src/mew/_core.pyi.

# Drop the no-op `__str__ = __repr__` alias emitted for IntFlag enums.
CounterFlags\.__str__:

# Spell the IntFlag default symbolically; stubgen otherwise renders it as `0`.
State\.set_counter:
    def set_counter(self, name: str, value: float, flags: CounterFlags = CounterFlags.kDefaults) -> None: ...
