# Source this file from Bash or Zsh after adding lab/vtfs/bin to PATH.
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
    # Resolve in a subshell first: failed saving must leave this shell's cwd alone.
    goi_destination=$(builtin cd "$@" >/dev/null && builtin pwd -P) || return $?
    command goi switch --from-cd "$goi_destination" || return $?
    builtin cd "$@"
}
