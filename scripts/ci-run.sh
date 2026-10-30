#!/usr/bin/env bash
# Run a CI step; on failure, put the tail of its output in an annotation (logs are not always readable).
name="$1"; shift
out=$(mktemp)
"$@" >"$out" 2>&1
status=$?
cat "$out"
if [ "$status" -ne 0 ]; then
  msg=$(tail -c 3000 "$out" | sed -e 's/%/%25/g' -e 's/\r/%0D/g' | sed ':a;N;$!ba;s/\n/%0A/g')
  echo "::error title=${name}::${msg}"
fi
exit "$status"
