set -ex

COMMANDS_FILE=commands.json

# Important to use full/absolute paths here.
AUTOPOINTS=/path/to/autopoints/main.py
SIMPOINTS_BIN=/path/to/simpoint-bin/simpoint
OUT_DIR=/path/to/where/to/put/checkpoints
GEM5_BIN=/path/to/build/X86/gem5.opt

python generate_command_file.py \
    --install-root ../ \
    --config gcc-pgo-lto-all-nopie.cfg \
    --output commands.json  \
    --suite intrate \
    --suite fprate

# Need this because x264 needs to be run ones to generate the inputs for all the commands.
python run_spec_with_wrapper.py \
    --install-root ../ \
    --bench x264 \
    --serialize-benchmark-commands \
    --commands-file $COMMANDS_FILE \
    -- {benchmark_cmd}

python run_spec_with_wrapper.py \
    --install-root ../ \
    --commands-file $COMMANDS_FILE \
    --jobs -1 \
    -- python $AUTOPOINTS collect \
     --program-cwd . \
     --simpoint-bin $SIMPOINTS_BIN \
     --max-k 10 \
     --bench {benchmark_name}-{command_index} \
     --output-dir $OUT_DIR \
     --stdin {stdin} \
     --stdout {stdout} \
     --stderr-append {stderr-append} \
     -- {benchmark_argv}

python $AUTOPOINTS checkpoint \
    --gem5-bin $GEM5_BIN \
    --output-dir $OUT_DIR \
    --jobs 60
