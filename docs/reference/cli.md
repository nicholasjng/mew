# CLI reference

The command sections below are generated from `mew … --help`.
For examples, see [writing and measuring benchmarks](../guide/writing-benchmarks.md#run-and-select) and [comparing and gating](../guide/comparing.md).

(exit-codes)=
## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Nothing matched: no benchmark selected, a `mew show` filter matched none, or the files in a comparison share no benchmark. |
| `2` | Usage, configuration, import, environment, or result-file error, explained on stderr. |
| `3` | `mew compare --exit-non-zero-on-regression` found a regression. |

## Shell completions

`mew completions bash`, `zsh`, or `fish` prints a completion script:

```console
$ mew completions fish > ~/.config/fish/completions/mew.fish
```

Completions cover commands, flags, paths, and fixed choices.
They never import benchmarks, so they do not complete benchmark names.

(cli-root)=
## `mew`

```{literalinclude} ../_generated/cli-help-root.txt
:language: console
```

(cli-ls)=
## `mew ls`

```{literalinclude} ../_generated/cli-help-ls.txt
:language: console
```

(cli-run)=
## `mew run`

```{literalinclude} ../_generated/cli-help-run.txt
:language: console
```

(cli-compare)=
## `mew compare`

```{literalinclude} ../_generated/cli-help-compare.txt
:language: console
```

(cli-show)=
## `mew show`

```{literalinclude} ../_generated/cli-help-show.txt
:language: console
```

(cli-log)=
## `mew log`

```{literalinclude} ../_generated/cli-help-log.txt
:language: console
```

(cli-completions)=
## `mew completions`

```{literalinclude} ../_generated/cli-help-completions.txt
:language: console
```
