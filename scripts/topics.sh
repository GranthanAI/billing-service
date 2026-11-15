#!/usr/bin/env bash
# Creates every topic in scripts/topics.txt on the shared Kafka. Idempotent: a topic that exists is left alone
# (--if-not-exists), so it is safe to run on every start. Needs Docker and the shared `graphgpt-kafka` container.
#
#   bash scripts/topics.sh                  create what is missing, then list what is missing/present
#   PARTITIONS=3 bash scripts/topics.sh     partitions for NEW topics (default 6; existing topics keep theirs)
#   KAFKA_CONTAINER=other bash scripts/topics.sh
set -uo pipefail
cd "$(dirname "$0")/.."
CONTAINER="${KAFKA_CONTAINER:-graphgpt-kafka}"
PARTITIONS="${PARTITIONS:-6}"
BIN=/opt/kafka/bin/kafka-topics.sh
run() { MSYS_NO_PATHCONV=1 docker exec "$CONTAINER" "$BIN" --bootstrap-server localhost:9092 "$@"; }

want=$(sed -E 's/#.*$//; s/\|.*$//; s/[[:space:]]+$//; s/^[[:space:]]+//' scripts/topics.txt | grep -v '^$')
have=$(run --list 2>/dev/null) || { echo "cannot reach Kafka in container '$CONTAINER'" >&2; exit 1; }

created=0; present=0; failed=0
for t in $want; do
  if grep -qxF "$t" <<<"$have"; then
    present=$((present + 1)); continue
  fi
  if run --create --if-not-exists --topic "$t" --partitions "$PARTITIONS" --replication-factor 1 >/dev/null 2>&1; then
    echo "created  $t"; created=$((created + 1))
  else
    echo "FAILED   $t" >&2; failed=$((failed + 1))
  fi
done
echo "topics: $created created, $present already existed, $failed failed (of $(wc -l <<<"$want" | tr -d ' ') listed)"
[ "$failed" -eq 0 ]
