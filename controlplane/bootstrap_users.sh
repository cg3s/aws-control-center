fail() {
    printf 'AWSCC: %s\n' "$*" >&2
    exit 1
}

verify_root_password() {
    if [ -n "$root_password_hash" ]; then
        installed_hash=$(getent shadow root | cut -d: -f2)
        [ "$installed_hash" = "$root_password_hash" ] || fail 'Root password hash was not applied or was changed'
    fi
}

install_authorized_keys() {
    local user=$1 keys=$2 home
    home=$(getent passwd "$user" | cut -d: -f6)
    [ -n "$home" ] && [ "$home" != / ] && [ -d "$home" ] || fail "No usable home directory for $user"
    # Write as the target account so an existing .ssh symlink cannot give it root writes.
    runuser -u "$user" -- sh -es -- "$home" "$keys" <<'AWSCC_KEYS'
umask 077
dir=$1/.ssh
file=$dir/authorized_keys
mkdir -p "$dir"
chmod 0700 "$dir"
candidate=$(mktemp "$dir/authorized_keys.awscc.XXXXXX")
trap 'rm -f "$candidate"' EXIT
# Put the keys explicitly chosen for this launch before any vendor redirect entries.
printf '%s\n' "$2" > "$candidate"
if [ -f "$file" ]; then
    grep -Fvx -e "$2" -- "$file" >> "$candidate" || [ "$?" -eq 1 ]
fi
mv -f "$candidate" "$file"
AWSCC_KEYS
    if command -v restorecon >/dev/null 2>&1; then
        restorecon -R "$home/.ssh"
    fi
}

default_login_user() {
    local candidate path detected=''
    if command -v cloud-init >/dev/null 2>&1; then
        for path in merged_system_cfg merged_cfg; do
            candidate=$(cloud-init query "$path.system_info.default_user.name" 2>/dev/null) || continue
            if [ "$candidate" != root ] && id "$candidate" >/dev/null 2>&1; then
                printf '%s\n' "$candidate"
                return
            fi
        done
    fi
    for candidate in ubuntu admin debian ec2-user bitnami centos rocky almalinux fedora ubuntu-aws opensuse; do
        if id "$candidate" >/dev/null 2>&1; then
            [ -z "$detected" ] || return 1
            detected=$candidate
        fi
    done
    [ -n "$detected" ] && printf '%s\n' "$detected"
}

if [ -n "$root_password_hash" ]; then
    printf 'root:%s\n' "$root_password_hash" | chpasswd -e || fail 'Setting the system root password failed'
    verify_root_password
    printf 'AWSCC: System root password applied\n'
fi
if [ -n "$root_keys" ]; then
    install_authorized_keys root "$root_keys"
    printf 'AWSCC: Root SSH public keys installed\n'
fi
if [ -n "$extra_user" ]; then
    if ! id "$extra_user" >/dev/null 2>&1; then
        useradd -m -s /bin/bash -- "$extra_user"
    fi
    [ "$(id -u "$extra_user")" -ge 1000 ] || fail "The additional user is a system account: $extra_user"
    sudo_file=$(mktemp)
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$extra_user" > "$sudo_file"
    visudo -c -f "$sudo_file" >/dev/null
    install -m 0440 "$sudo_file" "/etc/sudoers.d/90-awscc-$extra_user"
    rm -f "$sudo_file"
    if [ -n "$login_keys" ]; then
        install_authorized_keys "$extra_user" "$login_keys"
    fi
elif [ -n "$login_keys" ]; then
    user=$(default_login_user) || fail 'Cannot identify the image SSH user; specify an additional user'
    install_authorized_keys "$user" "$login_keys"
fi
