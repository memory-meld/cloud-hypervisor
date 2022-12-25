#!/bin/bash
set -xe

usage() { echo "Usage: $0 [-p] [-c <count>]" 1>&2; exit -1; }
# credit: https://stackoverflow.com/a/34531699
while getopts ":pc:h" o; do
    case "$o" in
        p)
            PARSEONLY=true
            ;;
        c)
            COUNT=${OPTARG}
            ;;
        h | *)
            usage
            ;;
    esac
done
shift $((OPTIND-1))

DIR="$(dirname "$(readlink -f "$0")")"
pushd "$DIR"

GUESTADDR="192.168.122.166"
YCSB="go-ycsb"
[ type $YCSB &>/dev/null ] || {
  [ -f go-ycsb-linux-amd64.tar.gz ] || wget https://github.com/pingcap/go-ycsb/releases/download/latest-313a79e9c48a60772ee5305d56006874325306c6/go-ycsb-linux-amd64.tar.gz
  [ -f go-ycsb ] || tar axf go-ycsb-linux-amd64.tar.gz
  YCSB="$DIR"/go-ycsb
}

WORKLOADA=" -p workload=core -p readallfields=true -p readproportion=0.5  -p updateproportion=0.5  -p scanproportion=0    -p insertproportion=0    -p requestdistribution=uniform"
WORKLOADB=" -p workload=core -p readallfields=true -p readproportion=0.95 -p updateproportion=0.05 -p scanproportion=0    -p insertproportion=0    -p requestdistribution=uniform"
WORKLOADC=" -p workload=core -p readallfields=true -p readproportion=1    -p updateproportion=0    -p scanproportion=0    -p insertproportion=0    -p requestdistribution=uniform"
WORKLOADD=" -p workload=core -p readallfields=true -p readproportion=0.95 -p updateproportion=0    -p scanproportion=0    -p insertproportion=0.05 -p requestdistribution=latest"
WORKLOADE=" -p workload=core -p readallfields=true -p readproportion=0    -p updateproportion=0    -p scanproportion=0.95 -p insertproportion=0.05 -p requestdistribution=uniform -p maxscanlength=1 -p scanlengthdistribution=uniform"
WORKLOADF=" -p workload=core -p readallfields=true -p readproportion=0.5  -p updateproportion=0    -p scanproportion=0    -p insertproportion=0    -p requestdistribution=uniform -p readmodifywriteproportion=0.5"

ch-remote() {
  local CHREMOTE="$DIR/../../target/release/ch-remote"
  "$CHREMOTE" --api-socket ~/Projects/ch-test/vm.socket "$@"
}

ssh-cmd() {
  ssh "$GUESTADDR" "$@"
}

if [ "$PARSEONLY" != true ]; then
  for dram in $(seq 0 8); do
    bash ~/Projects/ch-test/run-clr.sh & &> ycsb-d-$dram.vm
    until ssh-cmd uname -a; do sleep 3; done
    ch-remote resize --balloon [${dram}G,$((8-dram))G]
    sleep 10
    ssh-cmd tmux new -d redis-server --save "" --appendonly no --protected-mode no
    "$YCSB" load redis -p redis.addr=$GUESTADDR:6379 -p recordcount=$COUNT -p operationcount=$COUNT -p threadcount=4 $WORKLOADD |& tee ycsb-d-$dram.log
    ssh-cmd cat /proc/meminfo |& tee ycsb-d-$dram.meminfo
    ssh-cmd sudo poweroff || true
    sleep 3
  done
fi

echo ==YCSB==
echo ops avg p99
seq 0 8 | xargs -I{} tail -n 1 ycsb-d-{}.log | tr :,- " "  | awk '{ print $7 " " $9 " " $15 }'

popd
