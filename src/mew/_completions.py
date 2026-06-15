"""Generate static shell-completion scripts from the argparse parser.

Rolled by hand (no dependency): introspect the parser — subcommands, their
options, and each value's completion kind (file / fixed choices / none) — and
emit a completion script per shell. ``mew completions <shell>`` prints it for
``eval`` or install. Static: command and option names, file completion for path
args, and fixed choices (``--format``, ``--profiler``, the shell list); no live
benchmark-name completion.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

SHELLS = ("bash", "zsh", "fish", "powershell")


@dataclass
class _Opt:
    flags: list[str]
    help: str
    takes_value: bool
    # "file", a list of choices, or None (freeform / no value).
    value: str | list[str] | None


@dataclass
class _Cmd:
    names: list[str]  # primary + aliases (e.g. ["list", "ls"])
    help: str
    opts: list[_Opt] = field(default_factory=list)
    positional: str | list[str] | None = None  # value kind of the positional arg


def _value_kind(action: argparse.Action) -> str | list[str] | None:
    """Completion kind for an action's value: a choices list, ``"file"``, or None."""
    if action.choices:
        return [str(c) for c in action.choices]
    dest = action.dest
    if dest == "format":
        from mew.cli import _STDOUT_FORMATS  # source of truth, avoids drift

        return sorted(_STDOUT_FORMATS)
    if dest == "profiler":
        from mew import profilers

        return ["auto", *sorted(profilers._BACKENDS)]
    if getattr(action, "type", None) is Path:
        return "file"
    if not action.option_strings:  # positional → all of mew's are paths
        return "file"
    if dest == "output":  # run's `-o` takes `-`/`stdout` and file paths
        return "file"
    return None


def _commands(parser: argparse.ArgumentParser) -> list[_Cmd]:
    sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if sub is None:
        return []
    help_by_name = {ca.dest: (ca.help or "") for ca in sub._choices_actions}
    # Group names that map to the same subparser (argparse stores aliases as
    # extra keys pointing at the same parser object).
    grouped: dict[int, _Cmd] = {}
    order: list[int] = []
    for name, subp in sub.choices.items():
        key = id(subp)
        if key not in grouped:
            grouped[key] = _Cmd(names=[], help=help_by_name.get(name, ""))
            order.append(key)
            for a in subp._actions:
                if a.option_strings:
                    grouped[key].opts.append(
                        _Opt(a.option_strings, a.help or "", a.nargs != 0, _value_kind(a))
                    )
                else:
                    grouped[key].positional = _value_kind(a)
        grouped[key].names.append(name)
    return [grouped[k] for k in order]


def _global_flags(parser: argparse.ArgumentParser) -> list[str]:
    return [f for a in parser._actions if a.option_strings for f in a.option_strings]


# --- bash ---------------------------------------------------------------------


def _bash(parser: argparse.ArgumentParser) -> str:
    cmds = _commands(parser)
    top = [n for c in cmds for n in c.names] + _global_flags(parser)
    out = [
        "_mew() {",
        "    local cur prev cmd i w",
        '    cur="${COMP_WORDS[COMP_CWORD]}"',
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        '    cmd=""',
        "    for ((i=1; i<COMP_CWORD; i++)); do",
        '        w="${COMP_WORDS[i]}"',
        '        case "$w" in -*) ;; *) cmd="$w"; break;; esac',
        "    done",
        '    if [ -z "$cmd" ]; then',
        f'        COMPREPLY=( $(compgen -W "{" ".join(top)}" -- "$cur") )',
        "        return",
        "    fi",
        '    case "$cmd" in',
    ]
    for c in cmds:
        out.append(f"    {'|'.join(c.names)})")
        out.append('        case "$prev" in')
        file_flags = [f for o in c.opts if o.value == "file" for f in o.flags]
        if file_flags:
            out.append(
                f'        {"|".join(file_flags)}) COMPREPLY=( $(compgen -f -- "$cur") ); return;;'
            )
        for o in c.opts:
            if isinstance(o.value, list):
                out.append(
                    f"        {'|'.join(o.flags)}) "
                    f'COMPREPLY=( $(compgen -W "{" ".join(o.value)}" -- "$cur") ); return;;'
                )
        out.append("        esac")
        flags = [f for o in c.opts for f in o.flags]
        out.append('        if [ "${cur:0:1}" = "-" ]; then')
        out.append(f'            COMPREPLY=( $(compgen -W "{" ".join(flags)}" -- "$cur") )')
        out.append("            return")
        out.append("        fi")
        if c.positional == "file":
            out.append('        COMPREPLY=( $(compgen -f -- "$cur") )')
        elif isinstance(c.positional, list):
            out.append(f'        COMPREPLY=( $(compgen -W "{" ".join(c.positional)}" -- "$cur") )')
        out.append("        ;;")
    out += ["    esac", "}", "complete -F _mew mew", ""]
    return "\n".join(out)


# --- zsh ----------------------------------------------------------------------


def _zdesc(help_text: str) -> str:
    """First clause of a help string, sanitized for a zsh `_arguments` description."""
    s = (help_text or "").replace("\n", " ").split(". ")[0][:64]
    return s.translate(str.maketrans({"'": "", "[": "", "]": "", "`": "", ":": ";"}))


def _zsh_spec(o: _Opt) -> str:
    flagpart = "{" + ",".join(o.flags) + "}" if len(o.flags) > 1 else o.flags[0]
    spec = f"'({' '.join(o.flags)}){flagpart}[{_zdesc(o.help)}]"
    if o.takes_value:
        if o.value == "file":
            spec += ":path:_files"
        elif isinstance(o.value, list):
            spec += f":value:({' '.join(o.value)})"
        else:
            spec += ":value:"
    return spec + "'"


def _zsh(parser: argparse.ArgumentParser) -> str:
    cmds = _commands(parser)
    out = ["_mew() {", '  local curcontext="$curcontext" state line', "  _arguments -C \\"]
    out.append("    '1: :->command' \\")
    out.append("    '*:: :->args'")
    out.append("  case $state in")
    out.append("    command)")
    descr = " ".join(f"'{c.names[0]}:{_zdesc(c.help)}'" for c in cmds)
    out.append(f"      local -a cmds=({descr})")
    out.append("      _describe 'mew command' cmds")
    out.append("      ;;")
    out.append("    args)")
    out.append("      case $line[1] in")
    for c in cmds:
        out.append(f"        {'|'.join(c.names)})")
        specs = [_zsh_spec(o) for o in c.opts]
        if c.positional == "file":
            specs.append("'*:path:_files'")
        elif isinstance(c.positional, list):
            specs.append(f"'*:value:({' '.join(c.positional)})'")
        out.append("          _arguments \\")
        out.append("            " + " \\\n            ".join(specs))
        out.append("          ;;")
    out += ["      esac", "      ;;", "  esac", "}", "", "compdef _mew mew", ""]
    return "\n".join(out)


# --- fish ---------------------------------------------------------------------


def _fquote(s: str) -> str:
    s = (s or "").replace("\n", " ").split(". ")[0][:80]
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fish(parser: argparse.ArgumentParser) -> str:
    cmds = _commands(parser)
    out = []
    for c in cmds:
        for name in c.names:
            out.append(f"complete -c mew -n __fish_use_subcommand -a {name} -d {_fquote(c.help)}")
    for c in cmds:
        cond = "__fish_seen_subcommand_from " + " ".join(c.names)
        for o in c.opts:
            parts = [f"complete -c mew -n '{cond}'"]
            for f in o.flags:
                parts.append(f"-l {f[2:]}" if f.startswith("--") else f"-s {f[1:]}")
            if o.takes_value:
                if isinstance(o.value, list):
                    parts.append(f"-xa {_fquote(' '.join(o.value))}")
                elif o.value == "file":
                    parts.append("-rF")
                else:
                    parts.append("-x")
            parts.append(f"-d {_fquote(o.help)}")
            out.append(" ".join(parts))
    return "\n".join(out) + "\n"


# --- powershell ---------------------------------------------------------------


def _powershell(parser: argparse.ArgumentParser) -> str:
    cmds = _commands(parser)
    top = [n for c in cmds for n in c.names] + _global_flags(parser)
    branches = [
        f"        '{c.names[0]}' {{ @({_ps_list(f for o in c.opts for f in o.flags)}) }}"
        for c in cmds
    ]
    branches.append(f"        default {{ @({_ps_list(top)}) }}")
    body = "\n".join(branches)
    return f"""$block = {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $tokens = @($commandAst.CommandElements | ForEach-Object {{ $_.ToString() }})
    $cmd = $null
    for ($i = 1; $i -lt $tokens.Count; $i++) {{
        if ($tokens[$i] -notlike '-*') {{ $cmd = $tokens[$i]; break }}
    }}
    $candidates = switch ($cmd) {{
{body}
    }}
    $candidates | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }}
}}
Register-ArgumentCompleter -Native -CommandName mew -ScriptBlock $block
"""


def _ps_list(items) -> str:
    return ", ".join(f"'{i}'" for i in items)


_GENERATORS = {"bash": _bash, "zsh": _zsh, "fish": _fish, "powershell": _powershell}


def generate(shell: str, parser: argparse.ArgumentParser) -> str:
    """Return the completion script for ``shell`` built from ``parser``."""
    return _GENERATORS[shell](parser)
