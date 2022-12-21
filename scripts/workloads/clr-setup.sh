#!/bin/bash
set -x

if grep /etc/os-release Clear &>/dev/null; then
  echo "This script only supports clear linux"
  exit -1
fi

ensure_install() {
  local CMD="$1"
  local PKG="$2"
  if ! type "$CMD" &>/dev/null; then
    sudo swupd bundle-add "$PKG"
  fi
}

ensure_install redis-server redis-native
ensure_install nginx nginx
ensure_install go go-basic
ensure_install wget wget

DIR="$(dirname "$(readlink -f "$0")")"
pushd "$DIR"

# setup cockroach db
COCKROACH="$DIR/cockroach-v22.2.0.linux-amd64/cockroach"
COCKROACHARGS="$COCKROACH"
if ! "$COCKROACH" --version &>/dev/null; then
  [ -e cockroach-v22.2.0.linux-amd64.tgz ] || wget https://binaries.cockroachdb.com/cockroach-v22.2.0.linux-amd64.tgz
  tar axf cockroach-v22.2.0.linux-amd64.tgz
fi

# setup redis
REDIS="redis-server"
REDISARGS="$REDIS"
if ! "$REDIS" --version &>/dev/null; then
  echo "failed to find redis"
  exit -1
fi

# setup nginx
NGINX="nginx"
NGINXARGS="$NGINX"
if ! "$NGINX" -version &>/dev/null; then
  echo "failed to find nginx"
  exit -1
fi

# setup gbbs
GBBS="$DIR/gbbs/bazel-bin/benchmarks/PageRank/PageRank_main"
GBBSARGS="$GBBS -rounds 1 -s -b -c $DIR/gbbs/inputs/twitter-2010.bcsr"
go install github.com/bazelbuild/bazelisk@latest
[ -d "$DIR"/gbbs ] || git clone --recurse-submodules https://github.com/ParAlg/gbbs "$DIR"/gbbs
pushd "$DIR"/gbbs
git checkout 19bba1aacf052bacd7ad175ac145347267cec629
CC=clang CXX=clang++ bazelisk build //utils/... //benchmarks/PageRank:PageRank_main
[ -e inputs/twitter-2010.txt.gz ] || wget -O inputs/twitter-2010.txt.gz https://snap.stanford.edu/data/twitter-2010.txt.gz
[ -e inputs/twitter-2010.txt ] || gzip -dkq inputs/twitter-2010.txt.gz
[ -e inputs/twitter-2010.adj ] || bazel-bin/utils/snap_converter -s -i inputs/twitter-2010.txt -o inputs/twitter-2010.adj
[ -e inputs/twitter-2010.bcsr ] || bazel-bin/utils/compressor -s -o inputs/twitter-2010.bcsr inputs/twitter-2010.adj
popd
$GBBSARGS

echo "all done!"

