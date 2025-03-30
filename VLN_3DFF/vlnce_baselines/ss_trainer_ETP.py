import gc
import os
import sys
import random
import warnings
from collections import defaultdict
from typing import Dict, List
import jsonlines

import lmdb
import msgpack_numpy
import numpy as np
import math
import time
import torch
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.parallel import DistributedDataParallel as DDP
from copy import deepcopy
import tqdm
from gym import Space
from habitat import Config, logger
from habitat_baselines.common.baseline_registry import baseline_registry
from habitat_baselines.common.environments import get_env_class
from habitat_baselines.common.obs_transformers import (
    apply_obs_transforms_batch,
    apply_obs_transforms_obs_space,
    get_active_obs_transforms,
)
from habitat_baselines.common.tensorboard_utils import TensorboardWriter
from habitat_baselines.utils.common import batch_obs
from habitat.utils.geometry_utils import quaternion_rotate_vector
from habitat.tasks.utils import cartesian_to_polar
from vlnce_baselines.common.aux_losses import AuxLosses
from vlnce_baselines.common.base_il_trainer import BaseVLNCETrainer
from vlnce_baselines.common.env_utils import construct_envs, construct_envs_for_rl, is_slurm_batch_job
from vlnce_baselines.common.utils import extract_instruction_tokens
from vlnce_baselines.models.graph_utils import GraphMap, MAX_DIST, calculate_vp_rel_pos_fts
from vlnce_baselines.utils import reduce_loss

from .utils import get_camera_orientations12
from .utils import (
    length2mask, dir_angle_feature_with_ele,
)
from vlnce_baselines.common.utils import dis_to_con, gather_list_and_concat
from habitat_extensions.measures import NDTW, StepsTaken
from fastdtw import fastdtw

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=FutureWarning)
    import tensorflow as tf  # noqa: F401

import torch.distributed as distr
import gzip
import json
from copy import deepcopy
from torch.cuda.amp import autocast, GradScaler
from vlnce_baselines.common.ops import pad_tensors_wgrad, gen_seq_masks
from torch.nn.utils.rnn import pad_sequence
import cv2
from PIL import Image
import vlnce_baselines.waypoint_networks.utils as utils
from .utils import get_camera_orientations12
from .utils import (
    length2mask, dir_angle_feature_with_ele )
from vlnce_baselines.common.utils import dis_to_con, gather_list_and_concat
from vlnce_baselines.waypoint_networks.semantic_grid import SemanticGrid
from vlnce_baselines.waypoint_networks import get_img_segmentor_from_options
from vlnce_baselines.waypoint_networks.resnetUnet import ResNetUNet
import vlnce_baselines.waypoint_networks.viz_utils as viz_utils
import matplotlib.pyplot as plt
from datetime import timedelta

@baseline_registry.register_trainer(name="SS-ETP")
class RLTrainer(BaseVLNCETrainer):
    def __init__(self, config=None):
        super().__init__(config)
        self.max_len = int(config.IL.max_traj_len) #  * 0.97 transfered gt path got 0.96 spl
        self.config = config
        #self.config.defrost()
        #self.config.VIDEO_OPTION = ['disk']


    def _make_dirs(self):
        if self.config.local_rank == 0:
            self._make_ckpt_dir()
            # os.makedirs(self.lmdb_features_dir, exist_ok=True)
            if self.config.EVAL.SAVE_RESULTS:
                self._make_results_dir()

    def save_checkpoint(self, iteration: int):
        torch.save(
            obj={
                "state_dict": self.policy.state_dict(),
                "optim_state": self.optimizer.state_dict(),
                "iteration": iteration,
            },
            f=os.path.join(self.config.CHECKPOINT_FOLDER, f"ckpt.iter{iteration}.pth"),
        )

    def _set_config(self):

        self.batch_size = self.config.IL.batch_size

        self.world_size = self.config.GPU_NUMBERS
        self.local_rank = self.config.local_rank
        torch.cuda.set_device(self.device)
        if self.world_size > 1:
            torch.distributed.init_process_group("nccl",timeout=timedelta(seconds=7200000))
            self.local_rank, self.world_size = torch.distributed.get_rank(), torch.distributed.get_world_size()
            self.device = self.local_rank % torch.cuda.device_count()
            self.config.defrost()
            self.config.TORCH_GPU_ID = self.config.TORCH_GPU_IDS[self.local_rank]
            self.config.local_rank = self.local_rank
            self.config.freeze()
            torch.cuda.set_device(self.device)

        self.split = self.config.TASK_CONFIG.DATASET.SPLIT
        self.config.defrost()
        self.config.TASK_CONFIG.TASK.NDTW.SPLIT = self.split
        self.config.TASK_CONFIG.TASK.SDTW.SPLIT = self.split
        self.config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.MAX_SCENE_REPEAT_STEPS = -1
        self.config.SIMULATOR_GPU_IDS = self.config.SIMULATOR_GPU_IDS[self.config.local_rank]
        self.config.use_pbar = not is_slurm_batch_job()
        ''' if choosing image '''
        resize_config = self.config.RL.POLICY.OBS_TRANSFORMS.RESIZER_PER_SENSOR.SIZES
        crop_config = self.config.RL.POLICY.OBS_TRANSFORMS.CENTER_CROPPER_PER_SENSOR.SENSOR_CROPS
        task_config = self.config.TASK_CONFIG
        camera_orientations = get_camera_orientations12()
        for sensor_type in ["RGB", "DEPTH"]:
            resizer_size = dict(resize_config)[sensor_type.lower()]
            cropper_size = dict(crop_config)[sensor_type.lower()]
            sensor = getattr(task_config.SIMULATOR, f"{sensor_type}_SENSOR")
            for action, orient in camera_orientations.items():
                camera_template = f"{sensor_type}_{action}"
                camera_config = deepcopy(sensor)
                camera_config.ORIENTATION = camera_orientations[action]
                camera_config.UUID = camera_template.lower()
                setattr(task_config.SIMULATOR, camera_template, camera_config)
                task_config.SIMULATOR.AGENT_0.SENSORS.append(camera_template)
                resize_config.append((camera_template.lower(), resizer_size))
                crop_config.append((camera_template.lower(), cropper_size))
        self.config.RL.POLICY.OBS_TRANSFORMS.RESIZER_PER_SENSOR.SIZES = resize_config
        self.config.RL.POLICY.OBS_TRANSFORMS.CENTER_CROPPER_PER_SENSOR.SENSOR_CROPS = crop_config
        self.config.TASK_CONFIG = task_config
        self.config.SENSORS = task_config.SIMULATOR.AGENT_0.SENSORS
        if self.config.VIDEO_OPTION:
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("TOP_DOWN_MAP_VLNCE")
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("DISTANCE_TO_GOAL")
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("SUCCESS")
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("SPL")
            os.makedirs(self.config.VIDEO_DIR, exist_ok=True)
            shift = 0.
            orient_dict = {
                'Back': [0, math.pi + shift, 0],            # Back
                'Down': [-math.pi / 2, 0 + shift, 0],       # Down
                'Front':[0, 0 + shift, 0],                  # Front
                'Right':[0, math.pi / 2 + shift, 0],        # Right
                'Left': [0, 3 / 2 * math.pi + shift, 0],    # Left
                'Up':   [math.pi / 2, 0 + shift, 0],        # Up
            }
            sensor_uuids = []
            #H = 224
            for sensor_type in ["RGB"]:
                sensor = getattr(self.config.TASK_CONFIG.SIMULATOR, f"{sensor_type}_SENSOR")
                for camera_id, orient in orient_dict.items():
                    camera_template = f"{sensor_type}{camera_id}"
                    camera_config = deepcopy(sensor)
                    #camera_config.WIDTH = H
                    #camera_config.HEIGHT = H
                    camera_config.ORIENTATION = orient
                    camera_config.UUID = camera_template.lower()
                    camera_config.HFOV = 90
                    sensor_uuids.append(camera_config.UUID)
                    setattr(self.config.TASK_CONFIG.SIMULATOR, camera_template, camera_config)
                    self.config.TASK_CONFIG.SIMULATOR.AGENT_0.SENSORS.append(camera_template)
        self.config.freeze()

        

    def _init_envs(self):
        # for DDP to load different data
        self.config.defrost()
        self.config.TASK_CONFIG.SEED = self.config.TASK_CONFIG.SEED + self.local_rank
        self.config.freeze()

        self.envs = construct_envs(
            self.config, 
            get_env_class(self.config.ENV_NAME),
            auto_reset_done=False
        )
        env_num = self.envs.num_envs
        dataset_len = sum(self.envs.number_of_episodes)
        logger.info(f'LOCAL RANK: {self.local_rank}, ENV NUM: {env_num}, DATASET LEN: {dataset_len}')
        observation_space = self.envs.observation_spaces[0]
        action_space = self.envs.action_spaces[0]
        self.obs_transforms = get_active_obs_transforms(self.config)
        observation_space = apply_obs_transforms_obs_space(
            observation_space, self.obs_transforms
        )

        return observation_space, action_space

    def _initialize_policy(
        self,
        config: Config,
        load_from_ckpt: bool,
        observation_space: Space,
        action_space: Space,
        mode='train'
    ):
        start_iter = 0
        policy = baseline_registry.get_policy(self.config.MODEL.policy_name)
        self.policy = policy.from_config(
            config=config,
            observation_space=observation_space,
            action_space=action_space,
        )
        
        self.policy.net.vln_bert.feature_fields.load_state_dict(torch.load("pretrained/3dff.pth"),strict=True)
        
        if mode != 'train':
            ''' initialize the waypoint predictor here '''

            n_object_classes = 27

            ## Load the pre-trained img segmentation model
            self.img_segmentor = get_img_segmentor_from_options(n_object_classes,1.0)
            self.img_segmentor = self.img_segmentor.to(self.device)

            if self.config.GPU_NUMBERS > 1:
                self.img_segmentor = DDP(self.img_segmentor,device_ids=[self.device], output_device=self.device)
            else:
                self.img_segmentor = torch.nn.DataParallel(self.img_segmentor)

            checkpoint = torch.load("pretrained/segm.pt")
            self.img_segmentor.load_state_dict(checkpoint['models']['img_segm_model'])         
            self.img_segmentor.eval()

            self.policy.net.occupancy_map_predictor = ResNetUNet(3,3,True)
            self.policy.net.semantic_map_predictor = ResNetUNet(n_object_classes+3,n_object_classes,True)
            self.policy.net.waypoint_predictor = ResNetUNet(n_object_classes+3,1,True)
            
            self.policy.net.rgb_encoder.eval()
            self.policy.net.occupancy_map_predictor.eval()
            self.policy.net.semantic_map_predictor.eval()
            self.policy.net.waypoint_predictor.eval()
            
            self.cross_entropy_loss = torch.nn.CrossEntropyLoss()
            self.mse_loss = torch.nn.MSELoss()

            self.noise_filter = torch.nn.Conv2d(1, 1, (7, 7), padding=(3,3)).to(self.device)
            noise_filter_weight = torch.ones(1,1,7,7).to(self.device) #/ (7.*7.)
            self.noise_filter.weight = torch.nn.Parameter(noise_filter_weight)
            self.noise_filter.eval()

            self.img_segm_size = (128,128)
            ## Build necessary info for ground-projecting the semantic segmentation
            self._xs, self._ys = torch.tensor(np.array(np.meshgrid(np.linspace(-1,1,self.img_segm_size[0]), np.linspace(1,-1,self.img_segm_size[1]))), device=self.device)
            self._xs = self._xs.reshape(1,self.img_segm_size[0],self.img_segm_size[1])
            self._ys = self._ys.reshape(1,self.img_segm_size[0],self.img_segm_size[1])
            _x, _y = torch.tensor(np.array(np.meshgrid(np.linspace(0, self.img_segm_size[0]-1, self.img_segm_size[0]), 
                                                        np.linspace(0, self.img_segm_size[1]-1, self.img_segm_size[1]))), device=self.device)
            _xy_img = torch.cat((_x.reshape(1,self.img_segm_size[0],self.img_segm_size[1]), _y.reshape(1,self.img_segm_size[0],self.img_segm_size[1])), dim=0)
            _points2D_step = _xy_img.reshape(2, -1)
            self._points2D_step = torch.transpose(_points2D_step, 0, 1) # Npoints x 2  

            self.policy.to(self.device)

            if self.config.GPU_NUMBERS > 1:
                print('Using', self.config.GPU_NUMBERS,'GPU!')
                # find_unused_parameters=False fix ddp bug
                self.policy.net = DDP(self.policy.net.to(self.device), device_ids=[self.device],
                    output_device=self.device, find_unused_parameters=True, broadcast_buffers=False)
            else:
                self.policy.net = torch.nn.DataParallel(self.policy.net.to(self.device),
                    device_ids=[self.device], output_device=self.device)
            
            ckpt_dict = self.load_checkpoint('pretrained/cwp_predictor.pth', map_location="cpu")           
            b = [key for key in ckpt_dict["state_dict"].keys()]
            for key in b:
                if 'rgb_encoder' in key:
                    ckpt_dict['state_dict'].pop(key) 
            self.policy.load_state_dict(ckpt_dict["state_dict"],strict=False)
            
            
        elif mode == 'train':
            ''' initialize the waypoint predictor here '''
            from vlnce_baselines.waypoint_pred.TRM_net import BinaryDistPredictor_TRM
            self.waypoint_predictor = BinaryDistPredictor_TRM(device=self.device)
            cwp_fn = 'data/wp_pred/check_cwp_bestdist_hfov79' if self.config.MODEL.task_type == 'rxr' else 'data/wp_pred/check_cwp_bestdist_hfov90'
            self.waypoint_predictor.load_state_dict(torch.load(cwp_fn, map_location = torch.device('cpu'))['predictor']['state_dict'])
            for param in self.waypoint_predictor.parameters():
                param.requires_grad_(False)

            self.policy.to(self.device)
            self.waypoint_predictor.to(self.device)
            self.waypoint_predictor.eval()
            
            if self.config.GPU_NUMBERS > 1:
                print('Using', self.config.GPU_NUMBERS,'GPU!')
                # find_unused_parameters=False fix ddp bug
                self.policy.net = DDP(self.policy.net.to(self.device), device_ids=[self.device],
                    output_device=self.device, find_unused_parameters=True, broadcast_buffers=False)
            else:
                self.policy.net = torch.nn.DataParallel(self.policy.net.to(self.device),
                    device_ids=[self.device], output_device=self.device)

        
        self.optimizer = torch.optim.AdamW(filter(lambda p: p.requires_grad, self.policy.parameters()), lr=self.config.IL.lr)
        
                
        if load_from_ckpt:
            ckpt_dict = self.load_checkpoint(config.IL.ckpt_to_load, map_location="cpu")           
            self.policy.load_state_dict(ckpt_dict["state_dict"],strict=False)
            start_iter = ckpt_dict["iteration"]
            if config.IL.is_requeue:
                try:
                    self.optimizer.load_state_dict(ckpt_dict["optim_state"])
                except:
                    print("Optim_state is not loaded")
            logger.info(f"Loaded weights from checkpoint: {config.IL.ckpt_to_load}, iteration: {start_iter}")

        '''
        if load_from_ckpt:

            ckpt_path = config.IL.ckpt_to_load
            ckpt_dict = self.load_checkpoint(ckpt_path, map_location="cpu")
            start_iter = ckpt_dict["iteration"]

            if 'module' in list(ckpt_dict['state_dict'].keys())[0] and self.config.GPU_NUMBERS == 1:
                self.policy.net = torch.nn.DataParallel(self.policy.net.to(self.device),
                    device_ids=[self.device], output_device=self.device)
                self.policy.load_state_dict(ckpt_dict["state_dict"],strict=False)
                self.policy.net = self.policy.net.module

            else:
                self.policy.load_state_dict(ckpt_dict["state_dict"],strict=False)

            if config.IL.is_requeue:
                try:
                    self.optimizer.load_state_dict(ckpt_dict["optim_state"])
                except:
                    print("Optim_state is not loaded")

            logger.info(f"Loaded weights from checkpoint: {ckpt_path}, iteration: {start_iter}")
		'''

        params = sum(param.numel() for param in self.policy.parameters())
        params_t = sum(
            p.numel() for p in self.policy.parameters() if p.requires_grad
        )
        logger.info(f"Agent parameters: {params/1e6:.2f} MB. Trainable: {params_t/1e6:.2f} MB.")
        logger.info("Finished setting up policy.")

        return start_iter

    def _teacher_action(self, batch_angles, batch_distances, candidate_lengths):
        if self.config.MODEL.task_type == 'r2r':
            cand_dists_to_goal = [[] for _ in range(len(batch_angles))]
            oracle_cand_idx = []
            for j in range(len(batch_angles)):
                for k in range(len(batch_angles[j])):
                    angle_k = batch_angles[j][k]
                    forward_k = batch_distances[j][k]
                    dist_k = self.envs.call_at(j, "cand_dist_to_goal", {"angle": angle_k, "forward": forward_k})
                    cand_dists_to_goal[j].append(dist_k)
                curr_dist_to_goal = self.envs.call_at(j, "current_dist_to_goal")
                # if within target range (which def as 3.0)
                if curr_dist_to_goal < 1.5:
                    oracle_cand_idx.append(candidate_lengths[j] - 1)
                else:
                    oracle_cand_idx.append(np.argmin(cand_dists_to_goal[j]))
            return oracle_cand_idx
        elif self.config.MODEL.task_type == 'rxr':
            kargs = []
            current_episodes = self.envs.current_episodes()
            for i in range(self.envs.num_envs):
                kargs.append({
                    'ref_path':self.gt_data[str(current_episodes[i].episode_id)]['locations'],
                    'angles':batch_angles[i],
                    'distances':batch_distances[i],
                    'candidate_length':candidate_lengths[i]
                })
            oracle_cand_idx = self.envs.call(["get_cand_idx"]*self.envs.num_envs, kargs)
            return oracle_cand_idx

    def _teacher_action_new(self, batch_gmap_vp_ids, batch_no_vp_left):
        teacher_actions = []
        cur_episodes = self.envs.current_episodes()
        for i, (gmap_vp_ids, gmap, no_vp_left) in enumerate(zip(batch_gmap_vp_ids, self.gmaps, batch_no_vp_left)):
            curr_dis_to_goal = self.envs.call_at(i, "current_dist_to_goal")
            if curr_dis_to_goal < 1.5:
                teacher_actions.append(0)
            else:
                if no_vp_left:
                    teacher_actions.append(-100)
                elif self.config.IL.expert_policy == 'spl':
                    ghost_vp_pos = [(vp, random.choice(pos)) for vp, pos in gmap.ghost_real_pos.items()]
                    ghost_dis_to_goal = [
                        self.envs.call_at(i, "point_dist_to_goal", {"pos": p[1]})
                        for p in ghost_vp_pos
                    ]
                    target_ghost_vp = ghost_vp_pos[np.argmin(ghost_dis_to_goal)][0]
                    teacher_actions.append(gmap_vp_ids.index(target_ghost_vp))
                elif self.config.IL.expert_policy == 'ndtw':
                    ghost_vp_pos = [(vp, random.choice(pos)) for vp, pos in gmap.ghost_real_pos.items()]
                    target_ghost_vp = self.envs.call_at(i, "ghost_dist_to_ref", {
                        "ghost_vp_pos": ghost_vp_pos,
                        "ref_path": self.gt_data[str(cur_episodes[i].episode_id)]['locations'],
                    })
                    teacher_actions.append(gmap_vp_ids.index(target_ghost_vp))
                else:
                    raise NotImplementedError
       
        return torch.tensor(teacher_actions).cuda()



    def _vp_feature_variable(self, obs):
        batch_rgb_fts, batch_loc_fts = [], []
        batch_nav_types, batch_view_lens = [], []

        for i in range(self.envs.num_envs):
            rgb_fts, loc_fts , nav_types = [], [], []
            cand_idxes = np.zeros(12, dtype=np.bool)
            cand_idxes[obs['cand_img_idxes'][i]] = True
            # cand
            rgb_fts.append(obs['cand_rgb'][i])
            loc_fts.append(obs['cand_angle_fts'][i])
            nav_types += [1] * len(obs['cand_angles'][i])
            # non-cand
            rgb_fts.append(obs['pano_rgb'][i][~cand_idxes])
            loc_fts.append(obs['pano_angle_fts'][~cand_idxes])
            nav_types += [0] * (12-np.sum(cand_idxes))
            
            batch_rgb_fts.append(torch.cat(rgb_fts, dim=0))
            batch_loc_fts.append(torch.cat(loc_fts, dim=0))
            batch_nav_types.append(torch.LongTensor(nav_types))
            batch_view_lens.append(len(nav_types))
        # collate
        batch_rgb_fts = pad_tensors_wgrad(batch_rgb_fts)
        batch_loc_fts = pad_tensors_wgrad(batch_loc_fts).cuda()
        batch_nav_types = pad_sequence(batch_nav_types, batch_first=True).cuda()
        batch_view_lens = torch.LongTensor(batch_view_lens).cuda()

        return {
            'rgb_fts': batch_rgb_fts, 'loc_fts': batch_loc_fts,
            'nav_types': batch_nav_types, 'view_lens': batch_view_lens,
        }


    def _nav_gmap_variable(self, cur_vp, cur_pos, cur_ori):
        batch_gmap_vp_ids, batch_gmap_step_ids, batch_gmap_lens = [], [], []
        batch_gmap_img_fts, batch_gmap_pos_fts = [], []
        batch_gmap_pair_dists, batch_gmap_visited_masks = [], []
        batch_no_vp_left = []

        for i, gmap in enumerate(self.gmaps):
            node_vp_ids = list(gmap.node_pos.keys())
            ghost_vp_ids = list(gmap.ghost_pos.keys())
            if len(ghost_vp_ids) == 0:
                batch_no_vp_left.append(True)
            else:
                batch_no_vp_left.append(False)

            gmap_vp_ids = [None] + node_vp_ids + ghost_vp_ids
            gmap_step_ids = [0] + [gmap.node_stepId[vp] for vp in node_vp_ids] + [0]*len(ghost_vp_ids)
            gmap_visited_masks = [0] + [1] * len(node_vp_ids) + [0] * len(ghost_vp_ids)

            gmap_img_fts = [gmap.get_node_embeds(vp) for vp in node_vp_ids] + \
                           [gmap.get_node_embeds(vp) for vp in ghost_vp_ids]
            gmap_img_fts = torch.stack(
                [torch.zeros_like(gmap_img_fts[0])] + gmap_img_fts, dim=0
            )

            gmap_pos_fts = gmap.get_pos_fts(
                cur_vp[i], cur_pos[i], cur_ori[i], gmap_vp_ids
            )
            gmap_pair_dists = np.zeros((len(gmap_vp_ids), len(gmap_vp_ids)), dtype=np.float32)
            for j in range(1, len(gmap_vp_ids)):
                for k in range(j+1, len(gmap_vp_ids)):
                    vp1 = gmap_vp_ids[j]
                    vp2 = gmap_vp_ids[k]
                    if not vp1.startswith('g') and not vp2.startswith('g'):
                        dist = gmap.shortest_dist[vp1][vp2]
                    elif not vp1.startswith('g') and vp2.startswith('g'):
                        front_dis2, front_vp2 = gmap.front_to_ghost_dist(vp2)
                        dist = gmap.shortest_dist[vp1][front_vp2] + front_dis2
                    elif vp1.startswith('g') and vp2.startswith('g'):
                        front_dis1, front_vp1 = gmap.front_to_ghost_dist(vp1)
                        front_dis2, front_vp2 = gmap.front_to_ghost_dist(vp2)
                        dist = front_dis1 + gmap.shortest_dist[front_vp1][front_vp2] + front_dis2
                    else:
                        raise NotImplementedError
                    gmap_pair_dists[j, k] = gmap_pair_dists[k, j] = dist / MAX_DIST
            
            batch_gmap_vp_ids.append(gmap_vp_ids)
            batch_gmap_step_ids.append(torch.LongTensor(gmap_step_ids))
            batch_gmap_lens.append(len(gmap_vp_ids))
            batch_gmap_img_fts.append(gmap_img_fts)
            batch_gmap_pos_fts.append(torch.from_numpy(gmap_pos_fts))
            batch_gmap_pair_dists.append(torch.from_numpy(gmap_pair_dists))
            batch_gmap_visited_masks.append(torch.BoolTensor(gmap_visited_masks))
        
        # collate
        batch_gmap_step_ids = pad_sequence(batch_gmap_step_ids, batch_first=True).cuda()
        batch_gmap_img_fts = pad_tensors_wgrad(batch_gmap_img_fts)
        batch_gmap_pos_fts = pad_tensors_wgrad(batch_gmap_pos_fts).cuda()
        batch_gmap_lens = torch.LongTensor(batch_gmap_lens)
        batch_gmap_masks = gen_seq_masks(batch_gmap_lens).cuda()
        batch_gmap_visited_masks = pad_sequence(batch_gmap_visited_masks, batch_first=True).cuda()

        bs = len(cur_vp)
        max_gmap_len = max(batch_gmap_lens)
        gmap_pair_dists = torch.zeros(bs, max_gmap_len, max_gmap_len).float()
        for i in range(bs):
            gmap_pair_dists[i, :batch_gmap_lens[i], :batch_gmap_lens[i]] = batch_gmap_pair_dists[i]
        gmap_pair_dists = gmap_pair_dists.cuda()

        return {
            'gmap_vp_ids': batch_gmap_vp_ids, 'gmap_step_ids': batch_gmap_step_ids,
            'gmap_img_fts': batch_gmap_img_fts, 'gmap_pos_fts': batch_gmap_pos_fts, 
            'gmap_masks': batch_gmap_masks, 'gmap_visited_masks': batch_gmap_visited_masks, 'gmap_pair_dists': gmap_pair_dists,
            'no_vp_left': batch_no_vp_left,
        }


    def _history_variable(self, obs):
        batch_size = obs['pano_rgb'].shape[0]
        hist_rgb_fts = obs['pano_rgb'][:, 0, ...].cuda()
        hist_pano_rgb_fts = obs['pano_rgb'].cuda()
        hist_pano_ang_fts = obs['pano_angle_fts'].unsqueeze(0).expand(batch_size, -1, -1).cuda()

        return hist_rgb_fts, hist_pano_rgb_fts, hist_pano_ang_fts

    @staticmethod
    def _pause_envs(envs, batch, envs_to_pause):
        if len(envs_to_pause) > 0:
            state_index = list(range(envs.num_envs))
            for idx in reversed(envs_to_pause):
                state_index.pop(idx)
                envs.pause_at(idx)
            
            for k, v in batch.items():
                batch[k] = v[state_index]

        return envs, batch

    def train(self):
        self._set_config()
        if self.config.MODEL.task_type == 'rxr':
            self.gt_data = {}
            for role in self.config.TASK_CONFIG.DATASET.ROLES:
                with gzip.open(
                    self.config.TASK_CONFIG.TASK.NDTW.GT_PATH.format(
                        split=self.split, role=role
                    ), "rt") as f:
                    self.gt_data.update(json.load(f))

        observation_space, action_space = self._init_envs()
        start_iter = self._initialize_policy(
            self.config,
            self.config.IL.load_from_ckpt,
            observation_space=observation_space,
            action_space=action_space,
            mode='train'
        )

        total_iter = self.config.IL.iters
        log_every  = self.config.IL.log_every
        writer     = TensorboardWriter(self.config.TENSORBOARD_DIR if self.local_rank < 1 else None)

        self.scaler = GradScaler()
        logger.info('Training Starts... GOOD LUCK!')
        for idx in range(start_iter, total_iter, log_every):
            interval = min(log_every, max(total_iter-idx, 0))
            cur_iter = idx + interval

            sample_ratio = self.config.IL.sample_ratio ** (idx // self.config.IL.decay_interval + 1)
            # sample_ratio = self.config.IL.sample_ratio ** (idx // self.config.IL.decay_interval)
            logs = self._train_interval(interval, self.config.IL.ml_weight, sample_ratio)

            if self.local_rank < 1:
                loss_str = f'iter {cur_iter}: '
                for k, v in logs.items():
                    logs[k] = np.mean(v)
                    loss_str += f'{k}: {logs[k]:.3f}, '
                    writer.add_scalar(f'loss/{k}', logs[k], cur_iter)
                logger.info(loss_str)
                self.save_checkpoint(cur_iter)
        
    def _train_interval(self, interval, ml_weight, sample_ratio):
        self.policy.train()
        if self.local_rank < 1:
            pbar = tqdm.trange(interval, leave=False, dynamic_ncols=True)
        else:
            pbar = range(interval)
        self.logs = defaultdict(list)

        for idx in pbar:
            self.optimizer.zero_grad()
            self.loss = 0.


            with autocast():
                self.rollout('train', ml_weight, sample_ratio)
                
            print(self.loss)
            self.scaler.scale(self.loss).backward() # self.loss.backward()
            #torch.nn.utils.clip_grad_norm_(parameters=self.policy.parameters(), max_norm=5, norm_type=2)
            self.scaler.step(self.optimizer)        # self.optimizer.step()
            self.scaler.update()

            gc.collect()
            torch.cuda.empty_cache()
            if self.local_rank < 1:
                pbar.set_postfix({'iter': f'{idx+1}/{interval}'})
            
        return deepcopy(self.logs)

    @torch.no_grad()
    def _eval_checkpoint(
        self,
        checkpoint_path: str,
        writer: TensorboardWriter,
        checkpoint_index: int = 0,
    ):
        if self.local_rank < 1:
            logger.info(f"checkpoint_path: {checkpoint_path}")
        self.config.defrost()
        self.config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.SHUFFLE = False
        self.config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.MAX_SCENE_REPEAT_STEPS = -1
        self.config.IL.ckpt_to_load = checkpoint_path
        if self.config.VIDEO_OPTION:
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("TOP_DOWN_MAP_VLNCE")
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("DISTANCE_TO_GOAL")
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("SUCCESS")
            self.config.TASK_CONFIG.TASK.MEASUREMENTS.append("SPL")
            os.makedirs(self.config.VIDEO_DIR, exist_ok=True)
            shift = 0.
            orient_dict = {
                'Back': [0, math.pi + shift, 0],            # Back
                'Down': [-math.pi / 2, 0 + shift, 0],       # Down
                'Front':[0, 0 + shift, 0],                  # Front
                'Right':[0, math.pi / 2 + shift, 0],        # Right
                'Left': [0, 3 / 2 * math.pi + shift, 0],    # Left
                'Up':   [math.pi / 2, 0 + shift, 0],        # Up
            }
            sensor_uuids = []
            #H = 224
            for sensor_type in ["RGB"]:
                sensor = getattr(self.config.TASK_CONFIG.SIMULATOR, f"{sensor_type}_SENSOR")
                for camera_id, orient in orient_dict.items():
                    camera_template = f"{sensor_type}{camera_id}"
                    camera_config = deepcopy(sensor)
                    #camera_config.WIDTH = H
                    #camera_config.HEIGHT = H
                    camera_config.ORIENTATION = orient
                    camera_config.UUID = camera_template.lower()
                    camera_config.HFOV = 90
                    sensor_uuids.append(camera_config.UUID)
                    setattr(self.config.TASK_CONFIG.SIMULATOR, camera_template, camera_config)
                    self.config.TASK_CONFIG.SIMULATOR.AGENT_0.SENSORS.append(camera_template)
        self.config.freeze()

        if self.config.EVAL.SAVE_RESULTS:
            fname = os.path.join(
                self.config.RESULTS_DIR,
                f"stats_ckpt_{checkpoint_index}_{self.config.TASK_CONFIG.DATASET.SPLIT}.json",
            )
            if os.path.exists(fname) and not os.path.isfile(self.config.EVAL.CKPT_PATH_DIR):
                print("skipping -- evaluation exists.")
                return
        self.envs = construct_envs(
            self.config, 
            get_env_class(self.config.ENV_NAME),
            episodes_allowed=self.traj[::5] if self.config.EVAL.fast_eval else self.traj,
            auto_reset_done=False, # unseen: 11006 
        )
        dataset_length = sum(self.envs.number_of_episodes)
        print('local rank:', self.local_rank, '|', 'dataset length:', dataset_length)

        obs_transforms = get_active_obs_transforms(self.config)
        observation_space = apply_obs_transforms_obs_space(
            self.envs.observation_spaces[0], obs_transforms
        )
        self._initialize_policy(
            self.config,
            load_from_ckpt=True,
            observation_space=observation_space,
            action_space=self.envs.action_spaces[0],
            mode='eval'
        )
        self.policy.eval()
        #self.waypoint_predictor.eval()

        if self.config.EVAL.EPISODE_COUNT == -1:
            eps_to_eval = sum(self.envs.number_of_episodes)
        else:
            eps_to_eval = min(self.config.EVAL.EPISODE_COUNT, sum(self.envs.number_of_episodes))
        self.stat_eps = {}
        self.pbar = tqdm.tqdm(total=eps_to_eval) if self.config.use_pbar else None
        with autocast():
            while len(self.stat_eps) < eps_to_eval:
                self.rollout('eval')
        self.envs.close()

        if self.world_size > 1:
            distr.barrier()
        aggregated_states = {}
        num_episodes = len(self.stat_eps)
        for stat_key in next(iter(self.stat_eps.values())).keys():
            aggregated_states[stat_key] = (
                sum(v[stat_key] for v in self.stat_eps.values()) / num_episodes
            )
        total = torch.tensor(num_episodes).cuda()
        if self.world_size > 1:
            distr.reduce(total,dst=0)
        total = total.item()

        if self.world_size > 1:
            logger.info(f"rank {self.local_rank}'s {num_episodes}-episode results: {aggregated_states}")
            for k,v in aggregated_states.items():
                v = torch.tensor(v*num_episodes).cuda()
                cat_v = gather_list_and_concat(v,self.world_size)
                v = (sum(cat_v)/total).item()
                aggregated_states[k] = v
        
        split = self.config.TASK_CONFIG.DATASET.SPLIT
        fname = os.path.join(
            self.config.RESULTS_DIR,
            f"stats_ep_ckpt_{checkpoint_index}_{split}_r{self.local_rank}_w{self.world_size}.json",
        )
        with open(fname, "w") as f:
            json.dump(self.stat_eps, f, indent=2)

        if self.local_rank < 1:
            if self.config.EVAL.SAVE_RESULTS:
                fname = os.path.join(
                    self.config.RESULTS_DIR,
                    f"stats_ckpt_{checkpoint_index}_{split}.json",
                )
                with open(fname, "w") as f:
                    json.dump(aggregated_states, f, indent=2)

            logger.info(f"Episodes evaluated: {total}")
            checkpoint_num = checkpoint_index + 1
            for k, v in aggregated_states.items():
                logger.info(f"Average episode {k}: {v:.6f}")
                writer.add_scalar(f"eval_{k}/{split}", v, checkpoint_num)

    @torch.no_grad()
    def inference(self):

        self.batch_size = self.config.IL.batch_size

        self.world_size = self.config.GPU_NUMBERS
        self.local_rank = self.config.local_rank
        torch.cuda.set_device(self.device)
        if self.world_size > 1:
            torch.distributed.init_process_group("nccl",timeout=timedelta(seconds=7200000))
            self.local_rank, self.world_size = torch.distributed.get_rank(), torch.distributed.get_world_size()
            self.device = self.local_rank % torch.cuda.device_count()
            self.config.defrost()
            self.config.TORCH_GPU_ID = self.config.TORCH_GPU_IDS[self.local_rank]
            self.config.local_rank = self.local_rank
            self.config.freeze()
            torch.cuda.set_device(self.device)

        checkpoint_path = self.config.INFERENCE.CKPT_PATH
        logger.info(f"checkpoint_path: {checkpoint_path}")
        self.config.defrost()
        self.config.IL.ckpt_to_load = checkpoint_path
        self.config.TASK_CONFIG.DATASET.SPLIT = self.config.INFERENCE.SPLIT
        self.config.TASK_CONFIG.DATASET.ROLES = ["guide"]
        self.config.TASK_CONFIG.DATASET.LANGUAGES = self.config.INFERENCE.LANGUAGES
        self.config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.SHUFFLE = False
        self.config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.MAX_SCENE_REPEAT_STEPS = -1
        self.config.TASK_CONFIG.TASK.MEASUREMENTS = ['POSITION_INFER']
        self.config.TASK_CONFIG.TASK.SENSORS = [s for s in self.config.TASK_CONFIG.TASK.SENSORS if "INSTRUCTION" in s]
        self.config.SIMULATOR_GPU_IDS = [self.config.SIMULATOR_GPU_IDS[self.config.local_rank]]
        # if choosing image
        resize_config = self.config.RL.POLICY.OBS_TRANSFORMS.RESIZER_PER_SENSOR.SIZES
        crop_config = self.config.RL.POLICY.OBS_TRANSFORMS.CENTER_CROPPER_PER_SENSOR.SENSOR_CROPS
        task_config = self.config.TASK_CONFIG
        camera_orientations = get_camera_orientations12()
        for sensor_type in ["RGB", "DEPTH"]:
            resizer_size = dict(resize_config)[sensor_type.lower()]
            cropper_size = dict(crop_config)[sensor_type.lower()]
            sensor = getattr(task_config.SIMULATOR, f"{sensor_type}_SENSOR")
            for action, orient in camera_orientations.items():
                camera_template = f"{sensor_type}_{action}"
                camera_config = deepcopy(sensor)
                camera_config.ORIENTATION = camera_orientations[action]
                camera_config.UUID = camera_template.lower()
                setattr(task_config.SIMULATOR, camera_template, camera_config)
                task_config.SIMULATOR.AGENT_0.SENSORS.append(camera_template)
                resize_config.append((camera_template.lower(), resizer_size))
                crop_config.append((camera_template.lower(), cropper_size))
        self.config.RL.POLICY.OBS_TRANSFORMS.RESIZER_PER_SENSOR.SIZES = resize_config
        self.config.RL.POLICY.OBS_TRANSFORMS.CENTER_CROPPER_PER_SENSOR.SENSOR_CROPS = crop_config
        self.config.TASK_CONFIG = task_config
        self.config.SENSORS = task_config.SIMULATOR.AGENT_0.SENSORS
        self.config.freeze()
        

        self.traj = self.collect_infer_traj()

        self.envs = construct_envs(
            self.config, 
            get_env_class(self.config.ENV_NAME),
            episodes_allowed=self.traj,
            auto_reset_done=False,
        )

        obs_transforms = get_active_obs_transforms(self.config)
        observation_space = apply_obs_transforms_obs_space(
            self.envs.observation_spaces[0], obs_transforms
        )
        self._initialize_policy(
            self.config,
            load_from_ckpt=True,
            observation_space=observation_space,
            action_space=self.envs.action_spaces[0],
            mode='eval'
        )
        self.policy.eval()
        #self.waypoint_predictor.eval()

        if self.config.INFERENCE.EPISODE_COUNT == -1:
            eps_to_infer = sum(self.envs.number_of_episodes)
        else:
            eps_to_infer = min(self.config.INFERENCE.EPISODE_COUNT, sum(self.envs.number_of_episodes))
        self.path_eps = defaultdict(list)
        self.inst_ids: Dict[str, int] = {}   # transfer submit format
        self.pbar = tqdm.tqdm(total=eps_to_infer)

        with autocast():
            while len(self.path_eps) < eps_to_infer:
                self.rollout('infer')
        self.envs.close()

        if self.world_size > 1:
            distr.barrier()
            aggregated_path_eps = [None for _ in range(self.world_size)]
            distr.all_gather_object(aggregated_path_eps, self.path_eps)
            tmp_eps_dict = {}
            for x in aggregated_path_eps:
                tmp_eps_dict.update(x)
            self.path_eps = tmp_eps_dict

            aggregated_inst_ids = [None for _ in range(self.world_size)]
            distr.all_gather_object(aggregated_inst_ids, self.inst_ids)
            tmp_inst_dict = {}
            for x in aggregated_inst_ids:
                tmp_inst_dict.update(x)
            self.inst_ids = tmp_inst_dict


        if self.config.MODEL.task_type == "r2r":
            with open(self.config.INFERENCE.PREDICTIONS_FILE, "w") as f:
                json.dump(self.path_eps, f, indent=2)
            logger.info(f"Predictions saved to: {self.config.INFERENCE.PREDICTIONS_FILE}")
        else:  # use 'rxr' format for rxr-habitat leaderboard
            preds = []
            for k,v in self.path_eps.items():
                # save only positions that changed
                path = [v[0]["position"]]
                for p in v[1:]:
                    if p["position"] != path[-1]: path.append(p["position"])
                preds.append({"instruction_id": self.inst_ids[k], "path": path})
            preds.sort(key=lambda x: x["instruction_id"])
            with jsonlines.open(self.config.INFERENCE.PREDICTIONS_FILE, mode="w") as writer:
                writer.write_all(preds)
            logger.info(f"Predictions saved to: {self.config.INFERENCE.PREDICTIONS_FILE}")

    def get_pos_ori(self):
        pos_ori = self.envs.call(['get_pos_ori']*self.envs.num_envs)
        pos = [x[0] for x in pos_ori]
        ori = [x[1] for x in pos_ori]
        return pos, ori

    def rollout(self, mode, ml_weight=None, sample_ratio=None):

        if mode == 'train':
            feedback = 'sample'
        elif mode == 'eval' or mode == 'infer':
            feedback = 'argmax'
        else:
            raise NotImplementedError

        self.envs.resume_all()

        observations = self.envs.reset()
        instr_max_len = self.config.IL.max_text_len # r2r 80, rxr 200
        instr_pad_id = 1 if self.config.MODEL.task_type == 'rxr' else 0
        observations = extract_instruction_tokens(observations, self.config.TASK_CONFIG.TASK.INSTRUCTION_SENSOR_UUID,
                                                  max_length=instr_max_len, pad_id=instr_pad_id)
        batch = batch_obs(observations, self.device)
        batch = apply_obs_transforms_batch(batch, self.obs_transforms)
        

        if mode == 'eval':
            env_to_pause = [i for i, ep in enumerate(self.envs.current_episodes()) 
                            if ep.episode_id in self.stat_eps]    
            self.envs, batch = self._pause_envs(self.envs, batch, env_to_pause)
            if self.envs.num_envs == 0: return
        if mode == 'infer':
            env_to_pause = [i for i, ep in enumerate(self.envs.current_episodes()) 
                            if ep.episode_id in self.path_eps]    
            self.envs, batch = self._pause_envs(self.envs, batch, env_to_pause)
            if self.envs.num_envs == 0: return
            curr_eps = self.envs.current_episodes()
            for i in range(self.envs.num_envs):
                if self.config.MODEL.task_type == 'rxr':
                    ep_id = curr_eps[i].episode_id
                    k = curr_eps[i].instruction.instruction_id
                    self.inst_ids[ep_id] = int(k)

        self.batch_size = self.envs.num_envs

        # encode instructions
        all_txt_ids = batch['instruction']
        all_txt_masks = (all_txt_ids != instr_pad_id)
        all_txt_embeds = self.policy.net(
            mode='language',
            txt_ids=all_txt_ids,
            txt_masks=all_txt_masks,
        )

        loss = 0.
        total_actions = 0.
        not_done_index = list(range(self.envs.num_envs))


        have_real_pos = (mode == 'train' or self.config.VIDEO_OPTION)
        ghost_aug = self.config.IL.ghost_aug if mode == 'train' else 0
        self.gmaps = [GraphMap(have_real_pos, 
                               self.config.IL.loc_noise, 
                               self.config.MODEL.merge_ghost,
                               ghost_aug) for _ in range(self.envs.num_envs)]
        prev_vp = [None] * self.envs.num_envs


        ##############
        loss = 0.
        total_actions = 0.

        if self.config.MODEL.task_type == 'r2r':
            hfov = 90. * np.pi / 180.
            vfov = 90. * np.pi / 180.
        elif self.config.MODEL.task_type == 'rxr':
            hfov = 79. * np.pi / 180.
            vfov = 79. * np.pi / 180.

        map_config={'hfov':hfov,'vfov':vfov,'global_dim':(512,512),'grid_dim':(192,192),'heatmap_size':192,'cell_size':0.05,'img_segm_size':(128,128),'spatial_labels':3,'object_labels':27,'img_size':[256,256],'occupancy_height_thresh':-1.0,'norm_depth':True}
        # 3d info
        xs, ys = torch.tensor(np.array(np.meshgrid(np.linspace(-1,1,map_config['img_size'][0]), np.linspace(1,-1,map_config['img_size'][1]))), device=self.device)

        xs = xs.reshape(1,map_config['img_size'][0],map_config['img_size'][1])
        ys = ys.reshape(1,map_config['img_size'][0],map_config['img_size'][1])
        K = np.array([
            [1 / np.tan(map_config['hfov'] / 2.), 0., 0., 0.],
            [0., 1 / np.tan(map_config['vfov'] / 2.), 0., 0.],
            [0., 0.,  1, 0],
            [0., 0., 0, 1]])
        inv_K = torch.tensor(np.linalg.inv(K), device=self.device)


        # For each episode we need a new instance of a fresh global grid
        sg_map_global = SemanticGrid(self.batch_size, map_config['global_dim'], map_config['heatmap_size'], map_config['cell_size'],
                            spatial_labels=map_config['spatial_labels'], object_labels=map_config['object_labels'])

        abs_poses = [[] for b in range(self.batch_size)]
        turn_state = [None for b in range(self.batch_size)]
        turn_observations = [None for b in range(self.batch_size)]
        positions = [None for b in range(self.batch_size)]
        headings = [None for b in range(self.batch_size)]

        policy_net = self.policy.net
        if hasattr(self.policy.net, 'module'):
            policy_net = self.policy.net.module

        for stepk in range(self.max_len):
            batch_size = self.envs.num_envs
            # agent's current position and heading
            if stepk == 0:
                for ob_i in range(batch_size):
                    agent_state_i = self.envs.call_at(ob_i,
                            "get_agent_info", {})
                    positions[ob_i] = agent_state_i['position']
                    headings[ob_i] = agent_state_i['heading']

                # Reset the settings of 3D feature fields
                if self.config.MODEL.task_type == 'r2r':
                    policy_net.vln_bert.feature_fields.initialize_camera_setting(hfov=90., vfov=90.)
                    policy_net.vln_bert.feature_fields.initialize_novel_view_setting(hfov=90., vfov=90.)
                elif self.config.MODEL.task_type == 'rxr':
                    policy_net.vln_bert.feature_fields.initialize_camera_setting(hfov=79., vfov=79.)
                    policy_net.vln_bert.feature_fields.initialize_novel_view_setting(hfov=79., vfov=79.)

                policy_net.vln_bert.feature_fields.reset(batch_size)

            policy_net.action_step = stepk + 1
            policy_net.positions = positions
            policy_net.headings = [(heading+2*math.pi)%(2*math.pi) for heading in headings]

            with torch.no_grad():
                for update_id in range(2):

                    batch_img = []
                    batch_depth = []
                    batch_local3D_step = []
                    batch_rel_abs_pose = []

                    for b in range(batch_size):
                    
                        ##################################
                        if update_id == 0 and turn_observations[b]!=None:
                            heading_vector = quaternion_rotate_vector(
                                turn_state[b].rotation.inverse(), np.array([0, 0, -1])
                            )
                            headings[b] = cartesian_to_polar(-heading_vector[2], heading_vector[0])[1]
                            positions[b] = turn_state[b].position.tolist()
                            img = turn_observations[b]['rgb']
                            depth = turn_observations[b]['depth'].reshape(map_config['img_size'][0], map_config['img_size'][1], 1)
                            agent_state = turn_state[b]

                        else:
                            agent_state_info = self.envs.call_at(b,
                                    "get_agent_info", {})
                            positions[b] = agent_state_info['position']
                            headings[b] = agent_state_info['heading']

                            img = observations[b]['rgb']
                            depth = observations[b]['depth'].reshape(map_config['img_size'][0], map_config['img_size'][1], 1)
                            agent_state = self.envs.call_at(b,"get_agent_state", {})


                        ################
                        policy_net.positions[b] = positions[b] #!!!!!!!!!!!!!!!!!!!!
                        policy_net.headings[b] = headings[b]   #!!!!!!!!!!!!!!!!!!!!
                        ################
                        
                        
                        if mode != 'train':
                            viz_img = img

                            img = torch.tensor(img, device=self.device)

                            depth = torch.tensor(depth, device=self.device)
                            viz_depth = depth

                            if map_config['norm_depth']:
                                if self.config.MODEL.task_type == 'r2r':
                                    depth_abs = utils.unnormalize_depth(depth, min=0.0, max=10.0) #!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                                elif self.config.MODEL.task_type == 'rxr':
                                    depth_abs = utils.unnormalize_depth(depth, min=0.5, max=5.0) #!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

                            batch_img.append(img.unsqueeze(0))
                            batch_depth.append(depth_abs.unsqueeze(0))

                            local3D_step = utils.depth_to_3D(depth_abs, map_config['img_size'], xs, ys, inv_K)
                            batch_local3D_step.append(local3D_step)

                            agent_pose, y_height = utils.get_sim_location(agent_state=agent_state)

                            if len(abs_poses[b]) < stepk+1:
                                abs_poses[b].append(agent_pose)
                            else:
                                abs_poses[b][stepk] = agent_pose


                            # Keep track of the agent's relative pose from the initial position
                            rel_abs_pose = utils.get_rel_pose(pos2=abs_poses[b][stepk], pos1=abs_poses[b][0])
                            _rel_abs_pose = torch.Tensor(rel_abs_pose).unsqueeze(0).to(torch.float16)
                            _rel_abs_pose = _rel_abs_pose.to(self.device)
                            batch_rel_abs_pose.append(_rel_abs_pose)

                    if mode != 'train' and batch_rel_abs_pose != []:
                        ### Run the img segmentation model to get the ground-projected semantic segmentation
                        batch_abs_poses = torch.tensor(abs_poses).to(self.device)
                        batch_rel_abs_pose = torch.cat(batch_rel_abs_pose,dim=0)

                        batch_img = torch.cat(batch_img,dim=0)
                        
                        batch_depth = torch.cat(batch_depth,dim=0)
                        depth_img = batch_depth.clone().permute(0,3,1,2)

                        depth_img = F.interpolate(depth_img, size=map_config['img_segm_size'], mode='nearest')
                        imgData = utils.preprocess_img(batch_img, cropSize=map_config['img_segm_size'], pixFormat='NCHW', normalize=True)

                        segm_batch = {'images':imgData.to(self.device).unsqueeze(1),
                                    'depth_imgs':depth_img.to(self.device).unsqueeze(1)}
                        
                        pred_ego_sseg, img_segm = utils.run_img_segm(model=self.img_segmentor, 
                                                                input_batch=segm_batch, 
                                                                object_labels=map_config['object_labels'], 
                                                                crop_size=map_config['global_dim'], 
                                                                cell_size=map_config['cell_size'],
                                                                xs=self._xs,
                                                                ys=self._ys,
                                                                inv_K=inv_K,
                                                                points2D_step=self._points2D_step)   

                        
                        # do ground-projection, update the projected map
                        ego_grid_sseg_3 = utils.est_occ_from_depth(batch_local3D_step, grid_dim=map_config['global_dim'], cell_size=map_config['cell_size'], 
                                                                                        device=self.device, occupancy_height_thresh=map_config['occupancy_height_thresh'])

                        # Transform the ground projected egocentric grids to geocentric using relative pose
                        occup_grid_sseg = sg_map_global.spatialTransformer(grid=ego_grid_sseg_3, pose=batch_rel_abs_pose, abs_pose=batch_abs_poses)
                        semantic_grid_sseg = sg_map_global.spatialTransformer(grid=pred_ego_sseg[:,0], pose=batch_rel_abs_pose, abs_pose=batch_abs_poses)

                        # step_geo_grid contains the map snapshot every time a new observation is added
                        global_step_occup_grid_sseg, global_step_segm_grid_sseg = sg_map_global.update_proj_grid_bayes(occup_grid_sseg.unsqueeze(1),semantic_grid_sseg.unsqueeze(1))

                    if update_id == 0 and turn_observations!=[None]*len(turn_observations):
                        post_turn_observations = [item for item in turn_observations if item !=None]
                        post_turn_observations = extract_instruction_tokens(post_turn_observations,self.config.TASK_CONFIG.TASK.INSTRUCTION_SENSOR_UUID)
                        turn_batch = batch_obs(post_turn_observations, self.device)
                        turn_batch = apply_obs_transforms_batch(turn_batch, self.obs_transforms)
                        for k in turn_batch:
                            for b in range(batch_size):
                                if turn_observations[b] == None:                            
                                    turn_batch[k] = torch.cat([turn_batch[k][:b],batch[k][b:b+1],turn_batch[k][b:]],0)

                        # update the feature field
                        self.policy.net(
                            mode = "feature_field",
                            observations = turn_batch,
                            in_train = (mode == 'train' and self.config.IL.waypoint_aug),
                        )

                    elif update_id == 1:
                        self.policy.net(
                            mode = "feature_field",
                            observations = batch,
                            in_train = (mode == 'train' and self.config.IL.waypoint_aug),
                        )

                if mode != 'train':
                    #########################################################

                    # transform the projected grid back to egocentric (step_ego_grid_sseg contains all preceding views at every timestep)
                    step_occup_grid_sseg = sg_map_global.rotate_map(grid=global_step_occup_grid_sseg.squeeze(1), rel_pose=batch_rel_abs_pose, abs_pose=batch_abs_poses)
                    step_segm_grid_sseg = sg_map_global.rotate_map(grid=global_step_segm_grid_sseg.squeeze(1), rel_pose=batch_rel_abs_pose, abs_pose=batch_abs_poses)

                    # Crop the grid around the agent at each timestep
                    step_occup_grid_maps = utils.crop_grid(grid=step_occup_grid_sseg, crop_size=map_config['grid_dim'])
                    step_segm_grid_maps = utils.crop_grid(grid=step_segm_grid_sseg, crop_size=map_config['grid_dim'])               

                    predicted_occup_grid_maps =  self.policy.net.module.occupancy_map_predictor(step_occup_grid_maps.unsqueeze(1))
                    step_segm_occup_grid_maps = torch.cat((step_segm_grid_maps,predicted_occup_grid_maps),dim=-3)
                    predicted_segm_grid_maps = self.policy.net.module.semantic_map_predictor(step_segm_occup_grid_maps.unsqueeze(1))
                    step_segm_occup_grid_maps = torch.cat((predicted_segm_grid_maps.unsqueeze(1),predicted_occup_grid_maps.unsqueeze(1)),dim=-3)
                    waypoint_grid_maps = self.policy.net.module.waypoint_predictor(step_segm_occup_grid_maps).view(batch_size,1,map_config['grid_dim'][0],map_config['grid_dim'][1]).squeeze(1)

                    for b in range(batch_size):
                        waypoint_grid_maps[b] = waypoint_grid_maps[b] - waypoint_grid_maps[b].min()


                    waypoint_grid_maps = self.noise_filter(waypoint_grid_maps).squeeze(1)

                    x = torch.arange(0, map_config['grid_dim'][0], dtype=torch.float16).to(self.device)
                    y = torch.arange(0, map_config['grid_dim'][1], dtype=torch.float16).to(self.device)
                    yg, xg = torch.meshgrid(y,x)
                    yg = -(yg.to(self.device) -  map_config['grid_dim'][1] / 2. + 0.5)
                    xg = xg.to(self.device) -  map_config['grid_dim'][0] / 2. + 0.5

                    grid_rel_angle = torch.atan2(xg, yg)
                    grid_rel_angle = (grid_rel_angle + 2*math.pi) % (2.*math.pi)

                    predicted_waypoints = [[] for direction_idx in range(12)]

                    for direction_idx in range(12):
                        back_angle = math.radians(direction_idx * 30.- 15.) 
                        front_angle = math.radians(direction_idx * 30.+ 15.)
                        if direction_idx == 0:
                            back_angle += 2.*math.pi
                            selected_part = (back_angle <= grid_rel_angle) | (grid_rel_angle <= front_angle)
                        else:
                            selected_part = (back_angle <= grid_rel_angle) & (grid_rel_angle <= front_angle)

                        tmp_waypoint_grid_maps = waypoint_grid_maps.clone()
                        tmp_waypoint_grid_maps[:,selected_part==False] = 0.
                        max_value, max_index = tmp_waypoint_grid_maps.view(batch_size,-1).max(dim=-1)
                        max_y = torch.div(max_index, map_config['grid_dim'][0], rounding_mode='floor')
                        max_x = max_index % map_config['grid_dim'][0]

                        predicted_waypoints[direction_idx] = torch.cat([max_value.view(batch_size,1),max_x.view(batch_size,1),max_y.view(batch_size,1)],dim=-1).unsqueeze(1)

                    predicted_waypoints = torch.cat(predicted_waypoints,dim=1)

                    # merge near waypoints
                    merge_scale = 8
                    for x_merge in range(2):
                        for y_merge in range(2):
                            tmp_predicted_waypoints = predicted_waypoints[:,:,1:].to(torch.int64)  
                            if x_merge == 1:
                                tmp_predicted_waypoints[:,:,0] = tmp_predicted_waypoints[:,:,0] + merge_scale
                            if y_merge == 1:
                                tmp_predicted_waypoints[:,:,1] = tmp_predicted_waypoints[:,:,1] + merge_scale

                            tmp_predicted_waypoints = torch.div(tmp_predicted_waypoints, merge_scale*2, rounding_mode='floor').to(torch.int32)
                            for b in range(batch_size):
                                tmp_dict = {}
                                for i in range(12):
                                    # delete occupied waypoints
                                    if predicted_occup_grid_maps[b,:,predicted_waypoints[b,i,1].to(torch.int64),predicted_waypoints[b,i,2].to(torch.int64)].argmax().cpu().item() == 1: # occupied
                                        predicted_waypoints[b,i,0] = 0.

                                    key = str([tmp_predicted_waypoints[b][i][0].cpu().item(), tmp_predicted_waypoints[b][i][1].cpu().item()])
                                    if key in tmp_dict:
                                        if predicted_waypoints[b,tmp_dict[key],0] > predicted_waypoints[b,i,0]:
                                            predicted_waypoints[b,i,0] = 0.
                                        else:
                                            predicted_waypoints[b,tmp_dict[key],0] = 0.
                                    else:
                                        tmp_dict[key] = i


                    # select k waypoints
                    selected_waypoint_index = torch.topk(predicted_waypoints[:,:,0], k=8, dim=-1, largest=True)[1]
                    selected_waypoints = [0 for b in range(batch_size)]
                    batch_angle_idxes = []
                    batch_distance_idxes = []
                    for b in range(batch_size):
                        selected_waypoints[b] = predicted_waypoints[b,selected_waypoint_index[b]]
                        selected_waypoints[b] = selected_waypoints[b][selected_waypoints[b][:,0]!=0]
                        selected_waypoints[b] = selected_waypoints[b][:,1:]
                        rel_y = -(selected_waypoints[b][:,1] - map_config['grid_dim'][1]//2 + 0.5) * 0.05
                        rel_x = (selected_waypoints[b][:,0] - map_config['grid_dim'][0]//2 + 0.5) * 0.05
                        rel_angle = torch.atan2(rel_x, rel_y)

                        rel_dist = torch.sqrt(torch.square(rel_x) + torch.square(rel_y))
                        mask = (0.1 < rel_dist) & (rel_dist < 5.)
                        rel_dist = rel_dist[mask]
                        rel_angle = (rel_angle + 2*math.pi) % (2.*math.pi)
                        rel_angle = rel_angle[mask]
                        selected_waypoints[b] = selected_waypoints[b][mask]

                        # Discretization
                        angle_idx = torch.div((rel_angle+(math.pi/120)), (math.pi/60), rounding_mode='floor').to(torch.int32)
                        distance_idx = torch.div(rel_dist+0.25/2., 0.25, rounding_mode='floor').to(torch.int32) 

                        batch_angle_idxes.append(angle_idx)
                        batch_distance_idxes.append(distance_idx)


                    '''
                    for b in range(batch_size):            

                        scene_id = self.envs.current_episodes()[b].scene_id.split('/')[-1][:-4]
                        episode_id = self.envs.current_episodes()[b].episode_id

                        plt.cla()
                        plt.imshow(waypoint_grid_maps[b].cpu().numpy())

                        fig = plt.gcf()
                        plt.margins(0,0)
                        fig.savefig("example/"+str(episode_id)+'_'+str(stepk)+'heatmap.png', dpi=500, bbox_inches='tight')
                        plt.show()

                        ################################## 
                        save_img_dir = "example/" + 'predicted_occup_' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(predicted_occup_grid_maps, save_img_dir, name="img_segm", t=stepk, labels=3)

                        save_img_dir = "example/" + 'predicted_semantic_' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(predicted_segm_grid_maps, save_img_dir, name="img_segm", t=stepk, labels=27)

                        save_img_dir = "example/" + 'occup_' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(step_occup_grid_maps, save_img_dir, name="img_segm", t=stepk, labels=3)


                        save_img_dir = "example/" + 'semantic_' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(step_segm_grid_maps, save_img_dir, name="img_segm", t=stepk, labels=27)

                        save_img_dir = "example/" + 'global_occup_' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(global_step_occup_grid_sseg.squeeze(0), save_img_dir, name="img_segm", t=stepk, labels=3)


                        save_img_dir = "example/" + 'global_semantic_' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(global_step_segm_grid_sseg.squeeze(0), save_img_dir, name="img_segm", t=stepk, labels=27)


                        save_img_dir = "example/" + 'segm' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(img_segm, save_img_dir, name="img_segm", t=stepk, labels=27)


                        save_img_dir = "example/" + 'img_' + str(episode_id) +'_'
                        im_path = save_img_dir + "_" + str(stepk) + ".png"
                        Image.fromarray(viz_img).save(im_path)


                        save_img_dir = "example/" + 'depth_' + str(episode_id) +'_'
                        im_path = save_img_dir + "_" + str(stepk) + ".png"

                        Image.fromarray((viz_depth.repeat(1,1,3)*255.).cpu().numpy().astype(np.uint8)).convert('L').save(im_path)


                        save_img_dir = "example/" + 'predicted_cwp_' + str(episode_id) +'_'
                        viz_utils.write_tensor_imgSegm(predicted_segm_grid_maps, save_img_dir, name="img_segm", t=stepk, labels=27, waypoints=selected_waypoints[b].cpu().to(torch.int64).numpy().tolist())
                        ##################################

                    '''
                if mode != 'train':
                    # cand waypoint representation, need to be freezed
                    wp_outputs = self.policy.net(
                        mode = "semantic_map_for_waypoint",
                        batch_angle_idxes = batch_angle_idxes,
                        batch_distance_idxes = batch_distance_idxes,
                        observations = batch,
                        in_train = (mode == 'train' and self.config.IL.waypoint_aug),
                    )
                    
                elif mode == 'train':
                    # cand waypoint representation, need to be freezed
                    wp_outputs = self.policy.net(
                        mode = "panorama_for_waypoint",
                        waypoint_predictor = self.waypoint_predictor,
                        observations = batch,
                        in_train = (mode == 'train' and self.config.IL.waypoint_aug),
                    )
                    
            ###############################
                
            # pano encoder
            vp_inputs = self._vp_feature_variable(wp_outputs)
            vp_inputs.update({
                'mode': 'panorama',
            })
            pano_embeds, pano_masks = self.policy.net(**vp_inputs)
            avg_pano_embeds = torch.sum(pano_embeds * pano_masks.unsqueeze(2), 1) / \
                                torch.sum(pano_masks, 1, keepdim=True)

            # get vp_id, vp_pos of cur_node and cand_node
            cur_pos, cur_ori = self.get_pos_ori()
            cur_vp, cand_vp, cand_pos = [], [], []
            for i in range(self.envs.num_envs):
                cur_vp_i, cand_vp_i, cand_pos_i = self.gmaps[i].identify_node(
                    cur_pos[i], cur_ori[i], wp_outputs['cand_angles'][i], wp_outputs['cand_distances'][i]
                )
                cur_vp.append(cur_vp_i)
                cand_vp.append(cand_vp_i)
                cand_pos.append(cand_pos_i)
            
            if mode == 'train' or self.config.VIDEO_OPTION:
                cand_real_pos = []
                for i in range(self.envs.num_envs):
                    cand_real_pos_i = [
                        self.envs.call_at(i, "get_cand_real_pos", {"angle": ang, "forward": dis})
                        for ang, dis in zip(wp_outputs['cand_angles'][i], wp_outputs['cand_distances'][i])
                    ]
                    cand_real_pos.append(cand_real_pos_i)
            else:
                cand_real_pos = [None] * self.envs.num_envs

            for i in range(self.envs.num_envs):
                cur_embeds = avg_pano_embeds[i]
                cand_embeds = pano_embeds[i][vp_inputs['nav_types'][i]==1]
                self.gmaps[i].update_graph(prev_vp[i], stepk+1,
                                            cur_vp[i], cur_pos[i], cur_embeds,
                                            cand_vp[i], cand_pos[i], cand_embeds,
                                            cand_real_pos[i])

            total_actions += self.envs.num_envs
            txt_masks = all_txt_masks[not_done_index]
            txt_embeds = all_txt_embeds[not_done_index]
                
            nav_inputs = self._nav_gmap_variable(cur_vp, cur_pos, cur_ori)
            nav_inputs.update({
                'mode': 'navigation',
                'txt_embeds': txt_embeds,
                'txt_masks': txt_masks,
            
            })
            no_vp_left = nav_inputs.pop('no_vp_left')
            ###################################
            nav_inputs['predicted_bev_fts'] = wp_outputs['predicted_bev_fts']
            ###################################
            nav_outs = self.policy.net(**nav_inputs)
            nav_logits = nav_outs['global_logits']
            nav_probs = F.softmax(nav_logits, 1)
            for i, gmap in enumerate(self.gmaps):
                gmap.node_stop_scores[cur_vp[i]] = nav_probs[i, 0].data.item()

            # random sample demo
            # logits = torch.randn(nav_inputs['gmap_masks'].shape).cuda()
            # logits.masked_fill_(~nav_inputs['gmap_masks'], -float('inf'))
            # logits.masked_fill_(nav_inputs['gmap_visited_masks'], -float('inf'))
            
            if mode == 'train' or self.config.VIDEO_OPTION:
                teacher_actions = self._teacher_action_new(nav_inputs['gmap_vp_ids'], no_vp_left)
            if mode == 'train':
                loss += F.cross_entropy(nav_logits, teacher_actions, reduction='sum', ignore_index=-100)


            if feedback == 'sample':
                c = torch.distributions.Categorical(nav_probs)
                a_t = c.sample().detach()          
                a_t = torch.where(torch.rand_like(a_t, dtype=torch.float)<=sample_ratio, teacher_actions, a_t)
                
            elif feedback == 'argmax':
                a_t = nav_logits.argmax(dim=-1)
            else:
                raise NotImplementedError

            cpu_a_t = a_t.cpu().numpy()
          
            # make equiv action
            env_actions = []
            use_tryout = (self.config.IL.tryout and not self.config.TASK_CONFIG.SIMULATOR.HABITAT_SIM_V0.ALLOW_SLIDING)
            for i, gmap in enumerate(self.gmaps):
                if cpu_a_t[i] == 0 or stepk == self.max_len - 1 or no_vp_left[i]:
                    # stop at node with max stop_prob
                    vp_stop_scores = [(vp, stop_score) for vp, stop_score in gmap.node_stop_scores.items()]
                    stop_scores = [s[1] for s in vp_stop_scores]
                    stop_vp = vp_stop_scores[np.argmax(stop_scores)][0]
                    stop_pos = gmap.node_pos[stop_vp]
                    if self.config.IL.back_algo == 'control':
                        back_path = [(vp, gmap.node_pos[vp]) for vp in gmap.shortest_path[cur_vp[i]][stop_vp]]
                        back_path = back_path[1:]
                    else:
                        back_path = None
                    vis_info = {
                            'nodes': list(gmap.node_pos.values()),
                            'ghosts': list(gmap.ghost_aug_pos.values()),
                            'predict_ghost': stop_pos,
                    }
                    env_actions.append(
                        {
                            'action': {
                                'act': 0,
                                'cur_vp': cur_vp[i],
                                'stop_vp': stop_vp, 'stop_pos': stop_pos,
                                'back_path': back_path,
                                'tryout': use_tryout,
                            },
                            'vis_info': vis_info,
                        }
                    )
                else:
                    ghost_vp = nav_inputs['gmap_vp_ids'][i][cpu_a_t[i]]
                    ghost_pos = gmap.ghost_aug_pos[ghost_vp]
                    _, front_vp = gmap.front_to_ghost_dist(ghost_vp)
                    front_pos = gmap.node_pos[front_vp]
                    if self.config.VIDEO_OPTION:
                        teacher_action_cpu = teacher_actions[i].cpu().item()
                        if teacher_action_cpu in [0, -100]:
                            teacher_ghost = None
                        else:
                            teacher_ghost = gmap.ghost_aug_pos[nav_inputs['gmap_vp_ids'][i][teacher_action_cpu]]
                        vis_info = {
                            'nodes': list(gmap.node_pos.values()),
                            'ghosts': list(gmap.ghost_aug_pos.values()),
                            'predict_ghost': ghost_pos,
                            'teacher_ghost': teacher_ghost,
                        }
                    else:
                        vis_info = None
                    # teleport to front, then forward to ghost
                    if self.config.IL.back_algo == 'control':
                        back_path = [(vp, gmap.node_pos[vp]) for vp in gmap.shortest_path[cur_vp[i]][front_vp]]
                        back_path = back_path[1:]
                    else:
                        back_path = None
                    env_actions.append(
                        {
                            'action': {
                                'act': 4,
                                'cur_vp': cur_vp[i],
                                'front_vp': front_vp, 'front_pos': front_pos,
                                'ghost_vp': ghost_vp, 'ghost_pos': ghost_pos,
                                'back_path': back_path,
                                'tryout': use_tryout,
                            },
                            'vis_info': vis_info,
                        }
                    )
                    prev_vp[i] = front_vp
                    if self.config.MODEL.consume_ghost:
                        gmap.delete_ghost(ghost_vp)

            outputs = self.envs.step(env_actions)
            
            observation_package, _, dones, infos = [list(x) for x in zip(*outputs)]
            
            observations, turn_state, turn_observations = [],[],[]
            for item in observation_package:
                item_1,item_2,item_3 = item
                observations.append(item_1)
                turn_state.append(item_2)
                turn_observations.append(item_3)

            # calculate metric
            if mode == 'eval':
                curr_eps = self.envs.current_episodes()
                for i in range(self.envs.num_envs):
                    if not dones[i]:
                        continue
                    info = infos[i]
                    ep_id = curr_eps[i].episode_id
                    gt_path = np.array(self.gt_data[str(ep_id)]['locations']).astype(np.float)
                    pred_path = np.array(info['position']['position'])
                    distances = np.array(info['position']['distance'])
                    metric = {}
                    metric['steps_taken'] = info['steps_taken']
                    metric['distance_to_goal'] = distances[-1]
                    metric['success'] = 1. if distances[-1] <= 3. else 0.
                    metric['oracle_success'] = 1. if (distances <= 3.).any() else 0.
                    metric['path_length'] = float(np.linalg.norm(pred_path[1:] - pred_path[:-1],axis=1).sum())
                    metric['collisions'] = info['collisions']['count'] / len(pred_path)
                    gt_length = distances[0]
                    metric['spl'] = metric['success'] * gt_length / max(gt_length, metric['path_length'])
                    dtw_distance = fastdtw(pred_path, gt_path, dist=NDTW.euclidean_distance)[0]
                    metric['ndtw'] = np.exp(-dtw_distance / (len(gt_path) * 3.))
                    metric['sdtw'] = metric['ndtw'] * metric['success']
                    metric['ghost_cnt'] = self.gmaps[i].ghost_cnt
                    print(metric['oracle_success'],metric['success'],metric['spl'])
                    self.stat_eps[ep_id] = metric
                    self.pbar.update()

            # record path
            if mode == 'infer':
                curr_eps = self.envs.current_episodes()
                for i in range(self.envs.num_envs):
                    if not dones[i]:
                        continue
                    info = infos[i]
                    ep_id = curr_eps[i].episode_id
                    self.path_eps[ep_id] = [
                        {
                            'position': info['position_infer']['position'][0],
                            'heading': info['position_infer']['heading'][0],
                            'stop': False
                        }
                    ]
                    for p, h in zip(info['position_infer']['position'][1:], info['position_infer']['heading'][1:]):
                        if p != self.path_eps[ep_id][-1]['position']:
                            self.path_eps[ep_id].append({
                                'position': p,
                                'heading': h,
                                'stop': False
                            })
                    self.path_eps[ep_id] = self.path_eps[ep_id][:500]
                    self.path_eps[ep_id][-1]['stop'] = True
                    self.pbar.update()

            # pause env
            if sum(dones) > 0:
                for i in reversed(list(range(len(dones)))):
                    if dones[i]:
                        not_done_index.pop(i)
                        self.envs.pause_at(i)
                        observations.pop(i)
                        sg_map_global.pop(i)
                        abs_poses.pop(i)
                        positions.pop(i)
                        headings.pop(i)
                        turn_state.pop(i)
                        turn_observations.pop(i)

                        policy_net.vln_bert.feature_fields.pop(i) # Remove the feature fields for i-th sample

                        # graph stop
                        self.gmaps.pop(i)
                        prev_vp.pop(i)

            
            if self.envs.num_envs == 0:
                break

            # obs for next step
            observations = extract_instruction_tokens(observations,self.config.TASK_CONFIG.TASK.INSTRUCTION_SENSOR_UUID)
            batch = batch_obs(observations, self.device)
            batch = apply_obs_transforms_batch(batch, self.obs_transforms)

        #exit()
        #if self.world_size > 1:
        #    torch.distributed.barrier()

        if mode == 'train':
            loss = ml_weight * loss / total_actions
            self.loss += loss
            if self.loss == 0:
                self.logs['IL_loss'].append(loss)
            elif self.loss < 0. or self.loss > 1000.:
                pass
            else:
                self.logs['IL_loss'].append(loss.item())
