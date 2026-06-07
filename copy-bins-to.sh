CMD_FILE=commands.json
DEST_PATH=$(realpath $1)

python run_spec_with_wrapper.py \
    --install-root ../ \
    --commands-file $CMD_FILE \
    -- cp {benchmark_exe} $DEST_PATH
