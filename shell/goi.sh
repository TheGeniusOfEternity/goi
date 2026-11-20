# Source this file from Bash or Zsh after adding goi/bin to PATH.
# A shell function is needed because a child process cannot change our cwd.

goi() {
    if [ "${1-}" = switch ]; then
        local goi_arg goi_destination
        for goi_arg in "$@"; do
            case "$goi_arg" in
                -h|--help) command goi "$@"; return $? ;;
            esac
        done
        goi_destination=$(command goi "$@") || return $?
        builtin cd -- "$goi_destination"
    else
        command goi "$@"
    fi
}

cd() {
    local goi_destination
    # First try normal shell resolution (including CDPATH and cd -).
    if goi_destination=$(builtin cd "$@" 2>/dev/null >/dev/null && builtin pwd -P); then
        command goi _shell cd -- "$goi_destination" || return $?
    else
        GOI_CDPATH="${CDPATH-}" GOI_OLDPWD="${OLDPWD-}" command goi _shell cd -- "$@" || return $?
        goi_destination=$(builtin cd "$@" >/dev/null && builtin pwd -P) || return $?
    fi
    # A failed save must leave the current shell's cwd unchanged.
    command goi switch --from-cd "$goi_destination" || return $?
    builtin cd "$@"
}

function ls {
    command goi _shell ls -- "$@" || return $?
    command ls "$@"
}
