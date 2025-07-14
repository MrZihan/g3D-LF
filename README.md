### g3D-LF: Generalizable 3D-Language Feature Fields for Embodied Tasks

#### Zihan Wang, Gim Hee Lee

> We introduce Generalizable 3D-Language Feature Fields (g3D-LF), a 3D representation model pre-trained on large-scale 3D-language dataset for embodied tasks. Our g3D-LF processes posed RGB-D images from agents to encode feature fields for: 1) Novel view representation predictions from any position in the 3D scene; 2) Generations of BEV maps centered on the agent; 3) Querying targets using multi-granularity language within the above-mentioned representations. Our representation can be generalized to unseen environments, enabling real-time construction and dynamic updates. By volume rendering latent features along sampled rays and integrating semantic and spatial relationships through multiscale encoders, our g3D-LF produces representations at different scales and perspectives, aligned with multi-granularity language, via multi-level contrastive learning. Furthermore, we prepare a large-scale 3D-language dataset to align the representations of the feature fields with language. Extensive experiments on Vision-and-Language Navigation under both Panorama and Monocular settings, Zero-shot Object Navigation, and Situated Question Answering tasks highlight the significant advantages and effectiveness of our g3D-LF for embodied tasks. 

<div align=center><img src="https://github.com/MrZihan/g3D-LF/blob/main/Figure/introduction.png" width="500px" alt="Figure 1."/></div>

<div align=center><img src="https://github.com/MrZihan/g3D-LF/blob/main/Figure/framework.png" width="700px" alt="Figure 2. "/></div>

[Paper](https://openaccess.thecvf.com/content/CVPR2025/html/Wang_g3D-LF_Generalizable_3D-Language_Feature_Fields_for_Embodied_Tasks_CVPR_2025_paper.html) [Hugging Face](https://huggingface.co/datasets/MrZihanWang/g3D-LF/tree/main)  [TeraBox](https://1024terabox.com/s/1siccQ6fjdnnxAlPe0KB7rg)

## TODOs

* [x] Release the pre-training code of the g3D-LF Model.
* [x] Release the pre-training checkpoints of the g3D-LF Model.
* [x] Release the dataset used for pre-training g3D-LF Model.
* [x] Release the code and checkpoints of the Zero-shot Object Navigation.
* [x] Release the code and checkpoints of the Monocular VLN.
* [x] Release the code and checkpoints of the Panorama VLN.
* [ ] Release the code and checkpoints of the SQA3D.

### Requirements

1. Install `Habitat simulator`: follow instructions from [ETPNav](https://github.com/MarSaKi/ETPNav) or [VLN-CE](https://github.com/jacobkrantz/VLN-CE).

2. Install `torch_kdtree` for K-nearest feature search from [torch_kdtree](https://github.com/thomgrand/torch_kdtree).
   
   ```
   git clone https://github.com/thomgrand/torch_kdtree
   cd torch_kdtree
   git submodule init
   git submodule update
   pip3 install .
   ```

3. Install `tinycudann` for faster multi-layer perceptrons (MLPs) from [tiny-cuda-nn](https://github.com/NVlabs/tiny-cuda-nn).
   
   ```
   pip3 install git+https://github.com/NVlabs/tiny-cuda-nn/#subdirectory=bindings/torch
   ```

4. Download the preprocessed data and checkpoints from [Hugging Face](https://huggingface.co/datasets/MrZihanWang/g3D-LF/tree/main) or [TeraBox](https://1024terabox.com/s/1siccQ6fjdnnxAlPe0KB7rg).

5. (Optional) Download the Pre-training data.
    Download the `Habitat-Matterport 3D Research Dataset (HM3D)` from [habitat-matterport-3dresearch](https://github.com/matterport/habitat-matterport-3dresearch)
   
   ```
   hm3d-train-habitat-v0.2.tar
   hm3d-val-habitat-v0.2.tar
   ```
   
    Download RGB-D images of Structured3D, please follow [Structured3D](https://github.com/bertjiazheng/Structured3D)

### (Optional) Pre-train the 3D-Language Feature Fields

```
cd 3DFF_Pretrain
bash run_3dff/3dff.bash train 2341
python3 convert_ckpt.py # Convert the pre-trained checkpoint for downstream tasks, i.e., 3dff.pth
```

### Train the monocular VLN

1. (Optional) Pre-train the ETPNav without depth feature

```
cd ETPNav_without_depth
bash pretrain_src/run_pt/run_r2r.bash 2342
```

2. (Optional) Finetune the ETPNav without depth feature
   
   ```
   cd ETPNav_without_depth
   bash run_r2r/main.bash train 2343
   ```

3. Train and evaluate the monocular ETPNav with 3D-Language Feature Fields
   
   ```
   cd VLN_3DFF
   bash run_r2r/main.bash train 2344 # training
   bash run_r2r/main.bash eval 2344 # evaluation
   bash run_r2r/main.bash inter 2344 # inference
   ```

 ### Train the panorama VLN

1. (Optional) Pre-train the ETPNav

```
cd ETPNav
bash pretrain_src/run_pt/run_r2r.bash 2345
```

2. (Optional) Finetune the ETPNav
   
   ```
   cd ETPNav
   bash run_r2r/main.bash train 2346
   ```

3. Train and evaluate the Lookahead VLN model with 3D-Language Feature Fields
   
   ```
   cd Lookahead_3DFF
   bash run_r2r/main.bash train 2347 # training
   bash run_r2r/main.bash eval 2347 # evaluation
   bash run_r2r/main.bash inter 2347 # inference
   ```

### Run Zero-shot Object Navigation

Please follow [ObjectNav](https://github.com/MrZihan/g3D-LF/blob/main/ObjectNav/README.md)

## Citation

```bibtex
@inproceedings{wang2025g3d,
  title={g3d-lf: Generalizable 3d-language feature fields for embodied tasks},
  author={Wang, Zihan and Lee, Gim Hee},
  booktitle={Proceedings of the Computer Vision and Pattern Recognition Conference},
  pages={14191--14202},
  year={2025}
}
```

## Acknowledgments

Our code is based on [SceneVerse](https://github.com/scene-verse/sceneverse), [HNR](https://github.com/MrZihan/HNR-VLN), [VLN-3DFF](https://github.com/MrZihan/Sim2Real-VLN-3DFF), [ETPNav](https://github.com/MarSaKi/ETPNav) and [VLFM](https://github.com/bdaiinstitute/vlfm). Thanks for their great works!
