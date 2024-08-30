# Last modified: 2024-04-30
#
# Copyright 2023 Bingxin Ke, ETH Zurich. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# --------------------------------------------------------------------------
# If you find this code useful, we kindly ask you to cite our paper in your work.
# Please find bibtex at: https://github.com/prs-eth/Marigold#-citation
# If you use or adapt this code, please attribute to https://github.com/prs-eth/marigold.
# More information about the method can be found at https://marigoldmonodepth.github.io
# --------------------------------------------------------------------------

import io
import os
import random
from enum import Enum
from typing import Union

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset
from torchvision.transforms import(
  InterpolationMode,
  Compose,
  RandomHorizontalFlip,
  Resize,
  RandomResizedCrop,
  ColorJitter,
  Normalize,
  ToTensor,
  functional
)

from util.alignment import depth2disparity

from util.depth_transform import DepthNormalizerBase


class DatasetMode(Enum):
    RGB_ONLY = "rgb_only"
    EVAL = "evaluate"
    TRAIN = "train"


class DepthFileNameMode(Enum):
    """Prediction file naming modes"""

    id = 1  # id (nyuv2, kitti)
    rgb_id = 2  # rgb_id (vkitti2)
    frame_id_color = 3  # frame.id.color (hypersim)

class BaseDepthDataset(Dataset):
    def __init__(
        self,
        mode: DatasetMode,
        norm_name: str,
        filename_ls_path: str,
        dataset_dir: str,
        disp_name: str,
        min_depth: float,
        max_depth: float,
        has_filled_depth: bool,
        name_mode: DepthFileNameMode,
        depth_transform: Union[DepthNormalizerBase, None] = None,
        augmentation_args: dict = None,
        resize_to_hw=None,
        move_invalid_to_far_plane: bool = True,
        rgb_transform=lambda x: x / 255.0 * 2 - 1,  #  [0, 255] -> [-1, 1],
        gt_depth_type = 'depth_raw_linear',
        **kwargs,
    ) -> None:
        super().__init__()
        self.mode = mode
        # dataset info
        self.norm_name = norm_name
        self.filename_ls_path = filename_ls_path
        self.dataset_dir = dataset_dir
        assert os.path.exists(
            self.dataset_dir
        ), f"Dataset does not exist at: {self.dataset_dir}"
        self.disp_name = disp_name
        self.has_filled_depth = has_filled_depth
        self.name_mode: DepthFileNameMode = name_mode
        self.min_depth = min_depth
        self.max_depth = max_depth
        self.gt_depth_type = gt_depth_type

        if self.norm_name == 'vkitti2':
            self.means, self.stds = [0.3849854,  0.38966277, 0.3119897], [0.3849854,  0.38966277, 0.3119897]
        elif self.norm_name == 'hypersim':
            self.means, self.stds = [0.43323305, 0.40123018, 0.3691462], [0.43323305, 0.40123018, 0.3691462]
        elif self.norm_name == 'mixed':
            self.means, self.stds = [0.42240857, 0.398635  , 0.356323], [0.42240857, 0.398635,   0.356323]
        elif self.norm_name == 'nyu':
            self.means, self.stds = [0.48012177, 0.41071795, 0.39187136], [0.28875302, 0.29516797, 0.30792887]
        elif self.norm_name == 'kitti':
            self.means, self.stds = [0.38416928, 0.4104948 , 0.38838536], [0.30759685, 0.31810902, 0.32846335]
        else:
            raise NotImplementedError
        
        self.trans = Compose([
          ToTensor(),
          Normalize(mean=self.means, std=self.stds)
        ])

        # training arguments
        self.depth_transform: DepthNormalizerBase = depth_transform
        self.augm_args = augmentation_args
        self.resize_to_hw = resize_to_hw
        self.rgb_transform = rgb_transform
        self.move_invalid_to_far_plane = move_invalid_to_far_plane

        # Load filenames
        with open(self.filename_ls_path, "r") as f:
            self.filenames = [
                s.split() for s in f.readlines()
            ]  # [['rgb.png', 'depth.tif'], [], ...]

    def __len__(self):
        return len(self.filenames)

    def __getitem__(self, index):
        rasters, other = self._get_data_item(index)
        if DatasetMode.TRAIN == self.mode:
            rasters = self._training_preprocess(rasters)
        else:
            rasters = {k: self.trans(v) if k == "rgb_img" else v for k, v in rasters.items()}
        # merge
        outputs = rasters
        outputs.update(other)
        return outputs

    def _get_data_item(self, index):
        # rgb_rel_path, depth_rel_path, filled_rel_path = self._get_data_path(index=index)
        rgb_rel_path, depth_rel_path = self._get_data_path(index=index)

        rasters = {}

        # RGB data
        rasters.update(self._load_rgb_data(rgb_rel_path=rgb_rel_path))

        # Depth data
        if DatasetMode.RGB_ONLY != self.mode:
            # load data
            depth_data = self._load_depth_data(
                depth_rel_path=depth_rel_path #, filled_rel_path=filled_rel_path
            )
            rasters.update(depth_data)
            # valid mask
            rasters["valid_mask_raw"] = self._get_valid_mask(
                rasters["depth_raw_linear"]
            ).clone()
            # rasters["valid_mask_filled"] = self._get_valid_mask(
            #     rasters["depth_filled_linear"]
            # ).clone()

        other = {"index": index, "rgb_relative_path": rgb_rel_path}

        return rasters, other

    def _load_rgb_data(self, rgb_rel_path):
        # Read RGB data
        rgb = self._read_rgb_file(rgb_rel_path)
        # rgb_norm = rgb / 255.0 * 2.0 - 1.0  #  [0, 255] -> [-1, 1]

        outputs = {
            "rgb_img": rgb,
            # "rgb_int": torch.from_numpy(rgb).int(),
            # "rgb_norm": torch.from_numpy(rgb_norm).float(),
        }
        return outputs

    def _load_depth_data(self, depth_rel_path): # , filled_rel_path):
        # Read depth data
        outputs = {}
        depth_raw = self._read_depth_file(depth_rel_path).squeeze()
        depth_raw_linear = torch.from_numpy(depth_raw).float().unsqueeze(0)  # [1, H, W]
        outputs["depth_raw_linear"] = depth_raw_linear.clone()
        
        disparity = depth2disparity(depth_raw_linear)
        outputs["disparity"] = disparity.clone() 
        
        # if self.has_filled_depth:
        #     depth_filled = self._read_depth_file(filled_rel_path).squeeze()
        #     depth_filled_linear = torch.from_numpy(depth_filled).float().unsqueeze(0)
        #     outputs["depth_filled_linear"] = depth_filled_linear
        # else:
        #     outputs["depth_filled_linear"] = depth_raw_linear.clone()

        return outputs

    def _get_data_path(self, index):
        filename_line = self.filenames[index]

        # Get data path
        rgb_rel_path = filename_line[0]

        depth_rel_path, filled_rel_path = None, None
        if DatasetMode.RGB_ONLY != self.mode:
            depth_rel_path = filename_line[1]
            # if self.has_filled_depth:
            #     filled_rel_path = filename_line[2]
        return rgb_rel_path, depth_rel_path #, filled_rel_path

    def _read_image(self, img_rel_path) -> np.ndarray:
        image_to_read = os.path.join(self.dataset_dir, img_rel_path)
        image = Image.open(image_to_read)  # [H, W, rgb]
        # image = np.asarray(image)
        return image

    def _read_rgb_file(self, rel_path) -> np.ndarray:
        rgb = self._read_image(rel_path)
        # rgb = np.transpose(rgb, (2, 0, 1)).astype(int)  # [rgb, H, W]
        return rgb

    def _read_depth_file(self, rel_path):
        depth_in = self._read_image(rel_path)
        depth_in = np.asarray(depth_in)
        #  Replace code below to decode depth according to dataset definition
        depth_decoded = depth_in

        return depth_decoded

    def _get_valid_mask(self, depth: torch.Tensor):
        valid_mask = torch.logical_and(
            (depth > self.min_depth), (depth < self.max_depth)
        ).bool()
        return valid_mask

    def _training_preprocess(self, rasters):
        
        # Normalization
        rasters["depth_raw_norm"] = self.depth_transform(
            rasters["depth_raw_linear"], rasters["valid_mask_raw"]
        ).clone()
        
        # Augmentation
        if self.augm_args is not None:
            rasters = self._augment_data(rasters)
        else:
            rasters = {k: self.trans(v) if k == "rgb_img" else v for k, v in rasters.items()}

        # Resize
        if self.resize_to_hw is not None:
            resize_transform = Resize(
                size=self.resize_to_hw, interpolation=InterpolationMode.NEAREST_EXACT
            )
            rasters = {k: resize_transform(v) for k, v in rasters.items()}

        return rasters

    def _augment_data(self, rasters_dict):
        # jitter:
        if self.augm_args.jitter.in_use :
            JIT_transform = ColorJitter(**self.augm_args.jitter.args)
            if random.random() < self.augm_args.jitter.p:
                rasters_dict = {k: JIT_transform(v) if 'rgb_img'==k else v for k, v in rasters_dict.items()}
        #  red_green_channel_swap:
            elif self.augm_args.red_green_channel_swap.in_use :
                if random.random() < self.augm_args.red_green_channel_swap.p:
                    im = rasters_dict['rgb_img'].convert('RGB')
                    r, g, b = im.split()
                    result = Image.merge('RGB', (g, r, b))
                    rasters_dict['rgb_img'] = result
        # random_horizontal_flip:
        if self.augm_args.random_horizontal_flip.in_use :
            if random.random() < self.augm_args.random_horizontal_flip.p:
                rasters_dict = {k: RandomHorizontalFlip(p=1)(v) for k, v in rasters_dict.items()}     
        # this augmentaion for relative depth and needs rescale norm depth
        # random_resize_crop:
        # if self.augm_args.random_resize_crop.in_use :
        #     crop = RandomResizedCrop(size=[list(rasters_dict[list(rasters_dict.keys())[0]].size)[-1], list(rasters_dict[list(rasters_dict.keys())[0]].size)[-2]])
        #     params = crop.get_params(rasters_dict[list(rasters_dict.keys())[0]],  scale=(0.08, 1.0), ratio=(0.75, 1.33))
        #     if random.random() < self.augm_args.random_resize_crop.p:
        #         rasters_dict = {k: functional.crop(v, *params) for k, v in rasters_dict.items()}
        
        rasters_dict = {k: self.trans(v) if k == "rgb_img" else v for k, v in rasters_dict.items()}

        # cutdepth:
        if self.augm_args.cutdepth.in_use :
            if random.random() < self.augm_args.cutdepth.p:
              if self.gt_depth_type in ['depth_raw_linear','depth_raw_norm','disparity']:
                temp_depth = rasters_dict[self.gt_depth_type]
              else:
                raise NotImplementedError
              temp_im = rasters_dict['rgb_img']
              h = int(temp_im.size()[-2])
              W = int(temp_im.size()[-1])
              l = int(np.random.uniform() * W )
              w = int(max((W - l) * np.random.uniform() * self.augm_args.cutdepth.par, 1))
              M = torch.zeros(h,W)
              M[:,l:l+w] = 1
              augm = (temp_depth * M - temp_im * (M-1))
              rasters_dict['rgb_img'] = augm

        return rasters_dict


def get_pred_name(rgb_basename, name_mode, suffix=".png"):
    if DepthFileNameMode.rgb_id == name_mode:
        pred_basename = rgb_basename.replace("rgb_", "pred_")
    elif DepthFileNameMode.frame_id_color == name_mode:
        pred_basename = rgb_basename.replace("color", "pred")
    elif DepthFileNameMode.id == name_mode:
        pred_basename = "pred_" + rgb_basename
    else:
        raise NotImplementedError
    # change suffix
    pred_basename = os.path.splitext(pred_basename)[0] + suffix

    return pred_basename
