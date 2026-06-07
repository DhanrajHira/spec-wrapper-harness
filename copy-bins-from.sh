CMD_FILE=commands.json
SRC_PATH=$(realpath $1)

python run_spec_with_wrapper.py \
    --install-root ../ \
    --commands-file $CMD_FILE \
    --placeholder bin_name 'basename {benchmark_exe}' \
    -- cp $1/{bin_name} {benchmark_exe}

