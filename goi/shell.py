"""Explicit shell navigation; filesystem lookups never activate repositories."""
import os
from pathlib import Path


def ls_paths(arguments):
    """Extract GNU ls operands without treating option values as paths.

    Unknown options fall back to normal ls without activation.
    """
    values = {'--block-size', '--format', '--hide', '--ignore', '--indicator-style',
              '--quoting-style', '--sort', '--time', '--time-style', '--tabsize', '--width'}
    flags = {'--all', '--almost-all', '--author', '--escape', '--classify', '--file-type',
             '--dereference', '--dereference-command-line', '--dereference-command-line-symlink-to-dir',
             '--full-time', '--group-directories-first', '--human-readable', '--inode',
             '--kibibytes', '--literal', '--numeric-uid-gid', '--quote-name', '--recursive',
             '--reverse', '--si', '--size', '--color', '--hyperlink', '--zero', '--context'}
    paths, skip = [], False
    for index, arg in enumerate(arguments):
        if skip:
            skip = False
            continue
        if arg == '--':
            return paths + arguments[index + 1:] or ['.']
        if arg in ('--directory', '--help', '--version'):
            return []
        if arg.startswith('--'):
            name = arg.split('=', 1)[0]
            if name in values:
                skip = '=' not in arg
            elif name not in flags:
                return []
        elif arg.startswith('-') and arg != '-':
            for char in arg[1:]:
                if char == 'd':
                    return []
                if char in 'ITw':
                    skip = arg.endswith(char)
                    break
                if char not in 'aAbBcCfFgGhHiklLmnNopqQrRsStuUvXxZ1':
                    return []
        else:
            paths.append(arg)
    return [] if skip else paths or ['.']


def prepare(kind, arguments):
    from .cli import Client, configurations
    configs = configurations()
    if not configs:
        return
    seen = set()

    def activate(path):
        target = Path(path).resolve()
        for mount, config in sorted(configs.items(), key=lambda item: -len(item[0])):
            root = Path(mount)
            if not target.is_relative_to(root):
                continue
            parts = target.relative_to(root).parts
            if len(parts) < 2 or not os.path.ismount(root):
                return
            repo = root.joinpath(*parts[:2])
            if repo in seen or not repo.is_dir():
                return
            seen.add(repo)
            Client(root, config).call('activate', path='/'.join(parts[:2]))
            return

    if kind == 'ls':
        for path in ls_paths(arguments):
            activate(path)
        return
    # The shell passes an already resolved destination when cd succeeds. For a
    # not-yet-visible nested path, activate its repository before retrying cd.
    paths = list(arguments)
    while paths and paths[0] in ('-L', '-P', '-e', '--'):
        option = paths.pop(0)
        if option == '--':
            break
    if len(paths) > 1:  # Leave shell-specific forms/errors to the actual builtin.
        return
    target = paths[0] if paths else os.environ.get('HOME', '')
    if target == '-':
        target = os.environ.get('GOI_OLDPWD', '')
    if not target:
        return
    if not Path(target).is_absolute() and not target.startswith(('./', '../')):
        for prefix in os.environ.get('GOI_CDPATH', '').split(':'):
            candidate = Path(prefix or '.') / target
            activate(candidate)
            if candidate.is_dir():
                return
    activate(target)
