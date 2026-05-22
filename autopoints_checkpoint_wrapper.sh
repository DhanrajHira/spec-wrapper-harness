COMMANDS_FILE=commands.json
AUTOPOINTS=autopoints
SIMPOINTS_BIN=simpoints
OUT_DIR=./

 python run_spec_with_wrapper.py --install-root . --commands-file $COMMANDS_FILE --dry-run --jobs -1 -- \
     python $AUTOPOINTS collect \
     --program-cwd . \
     --simpoint-bin $SIMPOINTS_BIN \
     --max-k 10 \
     --output-dir $OUT_DIR \
     --stdout {stdout} \
     --stderr-append {stderr-append} \
     -- {benchmark_argv}
