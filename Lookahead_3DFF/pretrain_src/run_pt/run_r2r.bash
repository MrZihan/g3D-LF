
NODE_RANK=0
NUM_GPUS=1
outdir=pretrained/r2r_ce/mlm.sap_habitat_depth

# train
CUDA_VISIBLE_DEVICES='0' python3 -m torch.distributed.launch \
    --nproc_per_node=${NUM_GPUS} --node_rank $NODE_RANK --master_port=$1 --use_env \
    pretrain_src/pretrain_src/train_r2r.py --world_size ${NUM_GPUS} \
    --vlnbert cmt \
    --model_config pretrain_src/run_pt/r2r_model_config_dep.json \
    --config pretrain_src/run_pt/r2r_pretrain_habitat.json \
    --output_dir $outdir
