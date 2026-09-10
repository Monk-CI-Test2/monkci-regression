#!/usr/bin/env bash
# Live failure-path tests for the MIGlet runner-download / warm-but-dead fixes
# (monkci-core-miglet-agent branch fix/runner-download-retry).
#
# Runs against a real pool VM in dev/staging/prod via IAP SSH and checks the
# controller and custom-mig logs for the expected reactions. Requires gcloud
# auth with compute + logging read on the project and IAP SSH to pool VMs.
#
# Usage:
#   scripts/runner-failure-tests.sh <env> <test> [vm-name]
#
#   env    dev | staging | prod        (GCP project + k8s namespace)
#   test   preflight  image script, units and binary on the VM      (read-only)
#          boot       newest pool VM booted clean and reported ready (read-only)
#          invariant  every RUNNING pool VM has a live miglet         (read-only)
#          transient  github.com blackholed ~75s: download must retry and
#                     recover in place, miglet must come back ready
#          persistent github.com blackholed for the whole retry loop: init must
#                     fail, systemd must retry it, miglet must NOT report ready;
#                     after lifting the blackhole the VM must recover by itself.
#                     On a hand-off image it must instead report ERROR.
#          gate       runner dir removed, miglet restarted: exactly ONE error
#                     must reach the controller, VM retired and replaced.
#                     DESTROYS THE VM (custom-mig replaces it).
#          all        preflight boot invariant transient persistent gate
#   vm     optional; default = first RUNNING ${POOL_PREFIX} VM of <env>
#
# Overrides: POOL_PREFIX (default monkci--ubuntu-24-04-4),
#            RUNNER_INSTALL_TIMEOUT_S (default 120, must match the agent config).
#
# Runtime: preflight/boot/invariant ~1 min, transient ~5 min, persistent ~11 min,
# gate ~6 min. Exit code is non-zero if any check fails.
set -uo pipefail

ENV="${1:-}"; TEST="${2:-}"; VM="${3:-}"
POOL_PREFIX="${POOL_PREFIX:-monkci--ubuntu-24-04-4}"
GATE_S="${RUNNER_INSTALL_TIMEOUT_S:-120}"
BLACKHOLE="192.0.2.1 github.com"

case "$ENV" in
  prod)    PROJECT=monk-ci-prod; NS=prod ;;
  staging) PROJECT=monkcidev;    NS=staging ;;
  dev)     PROJECT=monkcidev;    NS=dev ;;
  *) sed -n 2,34p "$0"; exit 2 ;;
esac
[[ -n "$TEST" ]] || { sed -n 2,34p "$0"; exit 2; }

FAILS=0
pass() { echo "  PASS: $*"; }
fail() { echo "  FAIL: $*"; FAILS=$((FAILS+1)); }
say()  { echo; echo "== $(date -u +%H:%M:%S) $*"; }
now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }

pool_vms() {  # pool_vms [extra-filter]
  gcloud compute instances list --project "$PROJECT" \
    --filter="name~'^${POOL_PREFIX}' AND status=RUNNING AND metadata.items.environment=${NS}${1:+ AND $1}" \
    --sort-by=~creationTimestamp --format='value(name,creationTimestamp)'
}
zone_of() {  # retries: the GCE list API occasionally returns a transient 5xx
  local z i
  for i in 1 2 3 4 5; do
    z=$(gcloud compute instances list --project "$PROJECT" --filter="name=$1" --format='value(zone.basename())' 2>/dev/null)
    [[ -n "$z" ]] && { echo "$z"; return 0; }
    sleep 5
  done
  return 1
}
vm_ssh() {  # vm_ssh <vm> <zone> <script>
  gcloud compute ssh "$1" --project "$PROJECT" --zone "$2" --tunnel-through-iap --quiet \
    --ssh-flag='-o ServerAliveInterval=30' --ssh-flag='-o ConnectTimeout=20' --command "$3" 2>&1 \
    | grep -v '^Warning\|NumPy\|please see\|^$\|known_hosts\|^WARNING\|closed by remote\|Broken pipe\|troubleshoot\|exited with return code'
}
ctl_logs() {  # ctl_logs <container> <since-rfc3339> <extra-filter>
  gcloud logging read --project "$PROJECT" --order asc --limit 200 --format 'value(timestamp,jsonPayload.message)' \
    "resource.labels.namespace_name=\"$NS\" AND resource.labels.container_name=\"$1\" AND timestamp>=\"$2\" AND $3" 2>/dev/null
}
count_ctl() { ctl_logs "$1" "$2" "$3" | grep -c "$4"; }
wait_for() {  # wait_for <budget-s> <interval-s> <description> <command...>
  local budget=$1 step=$2 desc=$3; shift 3
  local waited=0
  until "$@"; do
    sleep "$step"; waited=$((waited+step))
    (( waited < budget )) || { echo "  (timed out after ${budget}s waiting for: $desc)"; return 1; }
  done
  echo "  (ok after ${waited}s: $desc)"
}
ctl_has() { ctl_logs "$1" "$2" "$3" | grep -q "$4"; }

need_vm() {
  [[ -n "$VM" ]] || VM=$(pool_vms | tail -1 | cut -f1)
  [[ -n "$VM" ]] || { fail "no RUNNING ${POOL_PREFIX} VM with environment=${NS} in ${PROJECT}"; return 1; }
  ZONE=$(zone_of "$VM") || { fail "could not resolve the zone of $VM"; return 1; }
  echo "  target VM: $VM ($ZONE)"
}

t_preflight() {
  say "preflight: image script, units, binary on $VM"
  local out
  out=$(vm_ssh "$VM" "$ZONE" '
    echo "retry_loop=$(grep -c RUNNER_DL_DELAY /opt/monkci/startup.sh)"
    echo "handoff=$(grep -c "continuing so MIGlet" /opt/monkci/startup.sh)"
    echo "init_restart=$(grep -c "Restart=on-failure" /etc/systemd/system/miglet-init.service)"
    echo "init_nolimit=$(grep -c "StartLimitIntervalSec=0" /etc/systemd/system/miglet-init.service)"
    echo "requeue=$(grep -c "systemctl start --no-block miglet" /opt/monkci/startup.sh)"
    echo "binary=$(sudo grep -ho "MIGlet [0-9a-f]* downloaded" /var/log/miglet-startup.log | tail -1)"
    echo "units=$(systemctl is-active miglet-init miglet | paste -sd,)"')
  sed 's/^/    /' <<<"$out"
  grep -q 'retry_loop=[1-9]' <<<"$out" && pass "image has the download retry loop" || fail "image lacks the retry loop (old image?)"
  grep -q 'init_restart=1'   <<<"$out" && pass "miglet-init has Restart=on-failure" || fail "miglet-init has no restart policy"
  grep -q 'init_nolimit=1'   <<<"$out" && pass "miglet-init start limit disabled" || fail "miglet-init start limit not disabled"
  grep -q 'requeue=1'        <<<"$out" && pass "startup.sh re-queues miglet.service" || fail "no miglet re-queue in startup.sh"
  grep -q 'handoff=1'        <<<"$out" && echo "  INFO: image HAS the hand-off (VM retires on final download failure)" \
                                       || echo "  INFO: image has NO hand-off (VM keeps retrying GitHub on final failure)"
  grep -q 'units=active,active' <<<"$out" && pass "miglet-init and miglet active" || fail "units not active: $(grep -o 'units=.*' <<<"$out")"
}

t_boot() {
  say "boot: newest ${POOL_PREFIX} VM in $NS booted clean"
  local name ts created log
  read -r name ts < <(pool_vms | head -1)
  [[ -n "${name:-}" ]] || { fail "no running pool VM"; return; }
  created=$(date -u -d "$ts" +%Y-%m-%dT%H:%M:%SZ)
  echo "  VM $name created $created"
  # The Ops Agent labels entries with either the bare name or the FQDN, and it
  # often starts tailing the file only after the boot lines were written, so
  # prefer the file on the VM and use Cloud Logging as the fallback.
  local zone; zone=$(zone_of "$name")
  log=$(vm_ssh "$name" "$zone" 'sudo cat /var/log/miglet-startup.log 2>/dev/null' | head -200)
  if ! grep -q "startup script started" <<<"$log"; then
    echo "  (could not read the log on the VM, falling back to Cloud Logging)"
    log=$(gcloud logging read --project "$PROJECT" --order asc --limit 200 --format 'value(jsonPayload.message)' \
      "logName=\"projects/${PROJECT}/logs/miglet_startup_file\" AND labels.\"compute.googleapis.com/resource_name\"=~\"^${name}(\\.|\$)\"" 2>/dev/null)
  fi
  grep -q "Runner v.* installed" <<<"$log" && pass "runner installed at boot" || fail "no 'Runner installed' line in startup log"
  grep -q "retrying in" <<<"$log" && echo "  INFO: boot needed download retries (recovered)" || pass "no download retries needed"
  grep -q "Failed to download" <<<"$log" && fail "download failed at boot"
  ctl_has mig-controller "$created" "jsonPayload.vm_id=\"$name\"" . \
    && pass "controller received events from $name" || fail "controller has no events from $name since creation"
}

t_invariant() {
  say "invariant: every RUNNING ${POOL_PREFIX}* VM in $NS has a live miglet"
  local bad=0 n=0 name ts age
  while read -r name ts; do
    [[ -n "$name" ]] || continue
    n=$((n+1)); age=$(( $(date +%s) - $(date -d "$ts" +%s) ))
    if gcloud logging read --project "$PROJECT" --limit 1 --format 'value(timestamp)' \
        "logName=\"projects/${PROJECT}/logs/miglet_journald\" AND jsonPayload.MESSAGE:\"$name\" AND jsonPayload.MESSAGE:\"agent started successfully\"" 2>/dev/null | grep -q .; then
      echo "    ok   $name (age ${age}s)"
    elif (( age < 600 )); then
      echo "    new  $name (age ${age}s, still booting)"
    else
      echo "    DEAD $name (age ${age}s, miglet never started)"; bad=$((bad+1))
    fi
  done < <(pool_vms)
  (( bad == 0 )) && pass "$n running pool VMs, none dead" || fail "$bad of $n running pool VMs have no miglet"
}

t_transient() {
  say "transient: blackhole github.com ~75s, expect retry then recovery on $VM"
  local since out; since=$(now)
  out=$(vm_ssh "$VM" "$ZONE" "
    sudo sh -c 'echo \"$BLACKHOLE\" >> /etc/hosts'
    sudo rm -rf /home/runner/actions-runner /run/miglet-startup.complete
    sudo systemctl restart miglet-init --no-block
    sleep 75
    echo \"retries_before_lift=\$(sudo grep -c 'retrying in' /var/log/miglet-startup.log)\"
    sudo sed -i '/192.0.2.1 github.com/d' /etc/hosts
    sleep 120
    sudo tail -n 40 /var/log/miglet-startup.log | grep -E 'retrying|installed|ERROR|script completed' | tail -4
    echo \"units=\$(systemctl is-active miglet-init miglet | paste -sd,)\"")
  sed 's/^/    /' <<<"$out"
  grep -q 'retries_before_lift=[1-9]' <<<"$out" && pass "download retried while blackholed" || fail "no retry observed"
  grep -q 'Runner v.* installed' <<<"$out" && pass "runner installed after blackhole lifted" || fail "runner not installed"
  grep -q 'units=active,active' <<<"$out" && pass "miglet re-queued and active" || fail "miglet not active after recovery"
  wait_for 120 15 "controller event from $VM" ctl_has mig-controller "$since" "jsonPayload.vm_id=\"$VM\"" . \
    && pass "controller heard from $VM after recovery" || fail "controller never heard from $VM after recovery"
}

t_persistent() {
  say "persistent: blackhole github.com for the whole retry loop on $VM (~8 min)"
  local since out; since=$(now)
  out=$(vm_ssh "$VM" "$ZONE" "
    sudo sh -c 'echo \"$BLACKHOLE\" >> /etc/hosts'
    sudo rm -rf /home/runner/actions-runner /run/miglet-startup.complete
    sudo systemctl restart miglet-init --no-block
    sleep 440
    sudo grep -hE 'retrying|ERROR|continuing so MIGlet' /var/log/miglet-startup.log | tail -7
    echo \"units=\$(systemctl is-active miglet-init miglet | paste -sd,)\"
    echo \"init_restarts=\$(sudo journalctl -u miglet-init --since '-9min' --no-pager -o cat | grep -c 'Scheduled restart job')\"
    echo \"ready_reports=\$(sudo journalctl -u miglet --since '-9min' --no-pager -o cat | grep -c '\\\"to_state\\\":\\\"ready\\\"')\"")
  sed 's/^/    /' <<<"$out"
  (( $(grep -c 'retrying in' <<<"$out") >= 5 )) && pass "all retry delays exercised" || fail "retry loop did not run to exhaustion"
  grep -q 'ERROR: Failed to download' <<<"$out" && pass "final failure logged" || fail "no final failure line"
  grep -q 'ready_reports=0' <<<"$out" && pass "miglet never reported ready without a runner" || fail "miglet reported ready with no runner"
  if grep -q 'continuing so MIGlet' <<<"$out"; then
    echo "  INFO: hand-off image: expecting ERROR report and retirement instead of init retries"
    wait_for $((GATE_S+120)) 20 "controller 'MIGlet reported error' for $VM" ctl_has mig-controller "$since" "jsonPayload.vm_id=\"$VM\"" 'reported error' \
      && pass "VM reported ERROR after hand-off" || fail "no ERROR report after hand-off"
    wait_for 240 20 "custom-mig deletes $VM" ctl_has custom-mig "$since" "jsonPayload.vmId=\"$VM\"" 'deleted successfully' \
      && pass "custom-mig deleted the VM" || fail "custom-mig did not delete the VM"
    # The VM is gone; later tests must pick the replacement once it has reported in.
    local old=$VM; VM=""
    wait_for 300 20 "replacement VM reports to the controller" bash -c "$(declare -f pool_vms ctl_has ctl_logs); PROJECT=$PROJECT NS=$NS POOL_PREFIX=$POOL_PREFIX; v=\$(pool_vms 'name!=$old' | tail -1 | cut -f1); [ -n \"\$v\" ] && ctl_has mig-controller '$since' \"jsonPayload.vm_id=\\\"\$v\\\"\" ." \
      && pass "replacement VM created and reported ready" || fail "no replacement VM reported to the controller"
    return
  fi
  grep -q 'init_restarts=[1-9]' <<<"$out" && pass "systemd restarted miglet-init" || fail "miglet-init was not restarted"
  grep -qE 'units=(activating|failed|active),(inactive|failed)' <<<"$out" \
    && pass "miglet stayed down while runner missing" || fail "unexpected unit state: $(grep -o 'units=.*' <<<"$out")"
  echo "  lifting blackhole, expecting self-recovery"
  out=$(vm_ssh "$VM" "$ZONE" "
    sudo sed -i '/192.0.2.1 github.com/d' /etc/hosts
    sleep 150
    sudo grep -hE 'installed|script completed' /var/log/miglet-startup.log | tail -2
    echo \"units=\$(systemctl is-active miglet-init miglet | paste -sd,)\"")
  sed 's/^/    /' <<<"$out"
  grep -q 'Runner v.* installed' <<<"$out" && pass "runner installed on a later init attempt" || fail "runner never installed after lifting"
  grep -q 'units=active,active' <<<"$out" && pass "miglet came up by itself" || fail "miglet not active after recovery"
}

t_gate() {
  say "gate: remove runner, restart miglet; expect ONE error, retirement, replacement (DESTROYS $VM)"
  local since errs moves; since=$(now)
  vm_ssh "$VM" "$ZONE" "sudo mv /home/runner/actions-runner /home/runner/actions-runner.bak && sudo systemctl restart miglet && echo restarted" | sed 's/^/    /'
  wait_for $((GATE_S+90)) 20 "controller 'MIGlet reported error' for $VM" ctl_has mig-controller "$since" "jsonPayload.vm_id=\"$VM\"" 'reported error' \
    || { fail "controller never logged 'MIGlet reported error'"; return; }
  errs=$(count_ctl mig-controller "$since" "jsonPayload.vm_id=\"$VM\"" 'reported error')
  moves=$(count_ctl mig-controller "$since" "jsonPayload.vm_id=\"$VM\"" 'from warm pool to completed')
  (( errs == 1 )) && pass "exactly one ERROR report" || fail "expected 1 ERROR report, got $errs (duplicate-report race?)"
  (( moves == 1 )) && pass "VM moved warm -> completed" || fail "warm->completed move count $moves"
  # custom-mig logs the VM name under jsonPayload.vmId (the controller uses vm_id).
  wait_for 240 20 "custom-mig deletes $VM" ctl_has custom-mig "$since" "jsonPayload.vmId=\"$VM\"" 'deleted successfully' \
    && pass "custom-mig deleted the VM" || fail "custom-mig did not delete the VM"
  wait_for 180 20 "a replacement pool VM appears" bash -c "$(declare -f pool_vms); PROJECT=$PROJECT NS=$NS POOL_PREFIX=$POOL_PREFIX; pool_vms 'name!=$VM' | grep -q ." \
    && pass "replacement VM created" || fail "no replacement VM"
}

run() { case "$1" in
  preflight)  need_vm && t_preflight ;;
  boot)       t_boot ;;
  invariant)  t_invariant ;;
  transient)  need_vm && t_transient ;;
  persistent) need_vm && t_persistent ;;
  gate)       need_vm && t_gate ;;
  *) echo "unknown test: $1"; exit 2 ;;
esac; }

echo "env=$ENV project=$PROJECT namespace=$NS test=$TEST"
if [[ "$TEST" == all ]]; then
  for t in preflight boot invariant transient persistent gate; do run "$t"; done
else
  run "$TEST"
fi
echo; (( FAILS == 0 )) && echo "ALL PASSED" || echo "$FAILS FAILURE(S)"
exit $(( FAILS > 0 ))
