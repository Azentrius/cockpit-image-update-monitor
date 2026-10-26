#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# A throw-away virtual machine for the end-to-end tests on the RHEL rebuilds, with SELinux enforcing
# (CI only, on a GitHub runner with KVM; never on a server). Meant to become the same file in every
# Azentrius add-on, like tools/build-packages.sh.
#
#   sh tools/vm.sh start rocky-9|rocky-10   boot the pinned cloud image, wait for SSH and cloud-init
#   sh tools/vm.sh image rocky-9|rocky-10   the pinned image's file name (the key of the CI cache)
#   sh tools/vm.sh root COMMAND...          run a command as root in the machine (one shell line)
#   sh tools/vm.sh back PORT...             make ports of 127.0.0.1 here reachable at 127.0.0.1 there
#   sh tools/vm.sh copy DIR FILE...         copy files from here into a folder of the machine
#   sh tools/vm.sh fetch DIR PATH...        copy files or folders of the machine to here
#   sh tools/vm.sh avc-mark                 print the SELinux denials so far, then mark the audit log
#   sh tools/vm.sh avc-check                fail on every SELinux denial after the mark
#   sh tools/vm.sh avc-selftest             cause a denial on purpose and fail unless the check sees it
#   sh tools/vm.sh logs DIR                 the console, the journal and the audit log, to DIR
#
# Needs qemu-system-x86_64, qemu-img, cloud-localds (cloud-image-utils), curl, ssh, scp, sha256sum
# and /dev/kvm. The images are pinned by name and SHA-256, taken from the CHECKSUM files Rocky signs
# (signatures checked against Rocky's release keys 21CB256A... for 9 and FC226859... for 10), and
# kept in VM_CACHE between runs; the machine's own files are in VM_DIR.
set -eu

cache=${VM_CACHE:-$HOME/.cache/azentrius-vm}
dir=${VM_DIR:-${RUNNER_TEMP:-/tmp}/azentrius-vm}
port=${VM_SSH_PORT:-2222}
# More TCP ports of the machine, reachable at the same port on 127.0.0.1 here (e.g. "9091" for a
# Cockpit the browser on the runner opens). The other way round, `back` tunnels ports over SSH:
# 10.0.2.2 would reach this side too, but a server bound to 127.0.0.1 that checks the Host header
# against DNS rebinding (Playwright's does) refuses that name.
forward=${VM_PORTS:-}
cfg=$dir/ssh_config
mark=/var/tmp/azentrius-vm.avc-mark
# The records of the audit log that tell of a denial.
denials='type=(AVC|USER_AVC|SELINUX_ERR|USER_SELINUX_ERR) '

die() { echo "vm.sh: $*" >&2; exit 1; }
guest() { ssh -F "$cfg" vm "$@"; }

image() {
    case $1 in
        rocky-9)
            name=Rocky-9-GenericCloud-Base-9.8-20260525.0.x86_64.qcow2
            sum=92c206cc6f790c61583247eefe87890f8828420662c17cacf247cec78ab4eec8
            url=https://dl.rockylinux.org/pub/rocky/9/images/x86_64/$name ;;
        rocky-10)
            name=Rocky-10-GenericCloud-Base-10.2-20260525.0.x86_64.qcow2
            sum=9fc9e9ff16888bb68ac39b0392e25c9c92684d50c85f1cce6ab549363bbc4b48
            url=https://dl.rockylinux.org/pub/rocky/10/images/x86_64/$name ;;
        *) die "unknown system $1 (rocky-9 or rocky-10)" ;;
    esac
}

start() {
    image "${1:?rocky-9 or rocky-10}"
    [ -w /dev/kvm ] || die "no access to /dev/kvm"
    mkdir -p "$cache" "$dir"
    if ! echo "$sum  $cache/$name" | sha256sum -c --status 2> /dev/null; then
        echo "Downloading $name"
        curl -fsSL --retry 3 --retry-delay 10 --max-time 900 -o "$cache/$name.part" "$url"
        mv "$cache/$name.part" "$cache/$name"
    fi
    echo "$sum  $cache/$name" | sha256sum -c - || die "$name does not match its pinned SHA-256"
    rm -f "$dir/disk.qcow2" "$dir/seed.img" "$dir/id_ed25519" "$dir/id_ed25519.pub"
    qemu-img create -q -f qcow2 -b "$cache/$name" -F qcow2 "$dir/disk.qcow2" 20G
    ssh-keygen -q -t ed25519 -N '' -C azentrius-vm -f "$dir/id_ed25519"
    cat > "$dir/user-data" << EOF
#cloud-config
users:
  - name: ci
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$dir/id_ed25519.pub")
ssh_pwauth: false
EOF
    printf 'instance-id: azentrius-vm\nlocal-hostname: azentrius-vm\n' > "$dir/meta-data"
    cloud-localds "$dir/seed.img" "$dir/user-data" "$dir/meta-data"
    cat > "$cfg" << EOF
Host vm
    HostName 127.0.0.1
    Port $port
    User ci
    IdentityFile $dir/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
    BatchMode yes
    ConnectTimeout 10
    ServerAliveInterval 15
EOF
    net="user,id=net0,hostfwd=tcp:127.0.0.1:$port-:22"
    for p in $forward; do
        case $p in ''|*[!0-9]*) die "VM_PORTS: not a port: $p" ;; esac
        net="$net,hostfwd=tcp:127.0.0.1:$p-:$p"
    done
    # -cpu host: Rocky 10 needs x86-64-v3.
    qemu-system-x86_64 -machine accel=kvm -cpu host -smp 2 -m 4096 \
        -drive "file=$dir/disk.qcow2,if=virtio,format=qcow2" \
        -drive "file=$dir/seed.img,if=virtio,format=raw" \
        -netdev "$net" -device virtio-net-pci,netdev=net0 \
        -display none -serial "file:$dir/console.log" -daemonize -pidfile "$dir/qemu.pid"
    up=0
    for _ in $(seq 1 60); do
        if guest true 2> /dev/null; then up=1; break; fi
        sleep 5
    done
    if [ "$up" != 1 ]; then
        tail -n 40 "$dir/console.log" >&2 || true
        die "no SSH to the machine after five minutes"
    fi
    guest sudo cloud-init status --wait > /dev/null 2>&1 || guest sudo cloud-init status --long || true
    mode=$(guest getenforce)
    [ "$mode" = Enforcing ] || die "SELinux is $mode, not Enforcing"
    guest sudo systemctl is-active --quiet auditd || die "auditd does not run: denials would go unseen"
    # fetch packs folders with tar, which a cloud image may lack.
    guest 'command -v tar > /dev/null || sudo dnf install -y -q tar'
    guest '. /etc/os-release; echo "$PRETTY_NAME, kernel $(uname -r), SELinux $(getenforce), $(rpm -q selinux-policy-targeted)"'
}

avc_mark() {
    echo "SELinux denials before the mark (not counted):"
    guest sudo grep -E "'$denials'" /var/log/audit/audit.log || echo "none"
    guest sudo sh -c "'wc -l < /var/log/audit/audit.log > $mark'"
}

avc_check() {
    # auditd writes asynchronously: give the last records a moment.
    sleep 3
    guest sudo test -s "$mark" || die "no mark: run avc-mark first"
    # Into a file first: a failed read must not pass as "no denials".
    guest sudo sh -c "'tail -n +\$((\$(cat $mark) + 1)) /var/log/audit/audit.log'" > "$dir/audit-after-mark.log" ||
        die "could not read the audit log after the mark"
    # Starting services, sudo and logins always write records: none means the log was not read.
    [ -s "$dir/audit-after-mark.log" ] || die "no audit records at all after the mark"
    echo "$(wc -l < "$dir/audit-after-mark.log") audit records after the mark."
    found=$(grep -E "$denials" "$dir/audit-after-mark.log" || true)
    if [ -n "$found" ]; then
        echo "SELinux denials after the mark:"
        printf '%s\n' "$found"
        printf '%s\n' "$found" |
            guest 'cat > /tmp/denials.log && sudo ausearch -i -m AVC,USER_AVC,SELINUX_ERR,USER_SELINUX_ERR -if /tmp/denials.log' || true
        die "SELinux denied something (see above)"
    fi
    echo "No SELinux denials after the mark."
}

# A denial on purpose: root may not enter httpd_t with /usr/bin/true, which is no entry point of
# that domain. The check has to catch it, or "no denials" would prove nothing.
avc_selftest() {
    avc_mark > /dev/null
    guest sudo runcon system_u:system_r:httpd_t:s0 /usr/bin/true > /dev/null 2>&1 || true
    if (avc_check) > "$dir/avc-selftest.log" 2>&1; then
        cat "$dir/avc-selftest.log"
        die "self-test: no denial seen after one on purpose"
    fi
    if ! grep -q httpd_t "$dir/avc-selftest.log"; then
        cat "$dir/avc-selftest.log"
        die "self-test: the check failed, but not for the denial on purpose"
    fi
    echo "Self-test: the check caught the denial on purpose:"
    grep -E "$denials" "$dir/avc-selftest.log" | head -n 3
}

case ${1:-} in
    start) shift; start "$@" ;;
    image) shift; image "${1:?image: rocky-9 or rocky-10}"; echo "$name" ;;
    root) shift; [ $# -gt 0 ] || die "root: no command"; guest sudo "$@" ;;
    back)
        shift; [ $# -ge 1 ] || die "back: PORT..."
        for p in "$@"; do
            case $p in ''|*[!0-9]*) die "back: not a port: $p" ;; esac
            # -f: in the background once the tunnel stands; it ends with the machine. Its own
            # messages go to a file, so that no output of this step stays open.
            if ! ssh -F "$cfg" -f -N -o ExitOnForwardFailure=yes -E "$dir/back-$p.log" \
                    -R "127.0.0.1:$p:127.0.0.1:$p" vm < /dev/null > /dev/null; then
                cat "$dir/back-$p.log" >&2 || true
                die "back: no tunnel for port $p"
            fi
        done ;;
    copy)
        shift; [ $# -ge 2 ] || die "copy: DIR FILE..."
        to=$1; shift
        for f in "$@"; do
            scp -q -F "$cfg" "$f" "vm:/tmp/$(basename "$f")"
            guest sudo install -D -m 644 "/tmp/$(basename "$f")" "$to/$(basename "$f")"
        done ;;
    fetch)
        shift; [ $# -ge 2 ] || die "fetch: DIR PATH..."
        to=$1; shift
        mkdir -p "$to"
        for p in "$@"; do guest sudo tar -C / -cf - "${p#/}" | tar -C "$to" -xf -; done ;;
    avc-mark) avc_mark ;;
    avc-check) avc_check ;;
    avc-selftest) avc_selftest ;;
    logs)
        shift; to=${1:?logs: DIR}
        mkdir -p "$to"
        cp "$dir/console.log" "$to/" 2> /dev/null || true
        guest sudo journalctl -b --no-pager > "$to/journal.txt" 2>&1 || true
        guest sudo cat /var/log/audit/audit.log > "$to/audit.log" 2>&1 || true ;;
    *) sed -n '7,16p' "$0" >&2; exit 64 ;;
esac
