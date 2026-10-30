#!/usr/bin/env bash
# Full taste-test run (SPEC.md §16.1): GPU and CPU model chains in parallel, then the evaluation.
cd "$(dirname "$0")"
LOG=../runs/taste-test
run_chain() {   # name, models...
  local name=$1; shift
  for m in "$@"; do
    echo "== $m $(date +%H:%M)" >> "$LOG/chain-$name.log"
    uv run python extract.py "$m" >> "$LOG/extract-$m.log" 2>&1 && echo "   ok $(date +%H:%M)" >> "$LOG/chain-$name.log" \
      || echo "   FAILED $(date +%H:%M) (see extract-$m.log)" >> "$LOG/chain-$name.log"
  done
  echo "== chain done $(date +%H:%M)" >> "$LOG/chain-$name.log"
}
rm -f "$LOG"/chain-*.log
run_chain gpu mert muq mulan omarrq stems signal &
run_chain cpu essentia maest &
wait
echo "== evaluate $(date +%H:%M)" >> "$LOG/chain-gpu.log"
uv run python evaluate.py > "$LOG/evaluate.log" 2>&1 && echo "== evaluate done $(date +%H:%M)" >> "$LOG/chain-gpu.log" \
  || echo "== evaluate FAILED (see evaluate.log)" >> "$LOG/chain-gpu.log"
