import torch
import torchvision
from torchvision.models.detection import MaskRCNN
from torchvision.models.detection.anchor_utils import AnchorGenerator

from pytorch_References import utils
from pytorch_References.engine import train_one_epoch, evaluate

from typing import Type, Tuple, List
from copy import deepcopy
import time
import os
import pandas as pd
import numpy as np

from eR_Mask_RCNN.transforms import get_augmentation


def check_GPU_memory() -> None:
    t = torch.cuda.get_device_properties(0).total_memory
    r = torch.cuda.memory_reserved(0)
    a = torch.cuda.memory_allocated(0)
    f = r - a  # free inside reserved

    print(f"Total memory {t}")
    print(f"Reserved memory {r}")
    print(f"Allocated memory {a}")
    print(f"Free memory {f}")


def MRCNN_model(num_classes: int) -> MaskRCNN:
    """
    Create Mask R-CNN model with given number of classes.
    """
    backbone = torchvision.models.detection.backbone_utils.resnet_fpn_backbone(
                'resnet101', pretrained=True) # noqa E126 
    anchor_generator = AnchorGenerator(sizes=(8, 16, 32, 64, 128),
                                       aspect_ratios=(0.5, 1.0, 2.0))
    roi_pooler = torchvision.ops.MultiScaleRoIAlign(featmap_names=['0'],
                                                    output_size=7,
                                                    sampling_ratio=2)

    mask_roi_pooler = torchvision.ops.MultiScaleRoIAlign(featmap_names=['0'],
                                                         output_size=14,
                                                         sampling_ratio=2)

    model = MaskRCNN(backbone,
                     num_classes=num_classes,
                     rpn_anchor_generator=anchor_generator,
                     box_roi_pool=roi_pooler,
                     mask_roi_pool=mask_roi_pooler)
    model.eval()
    return model


class My_Mask_RCNN:
    def __init__(self, num_classes: int, train_path: str, val_path: str, model_path: str,
                 device: str = 'cuda', torch_home: str = None, save_hist_freq: int = 10) -> None:
        """
        Create model and get configuration.
        """

        self.train_path = train_path
        self.val_path = val_path
        if os.path.isdir(model_path):
            model_path = os.path.join(model_path, 'mrcnn_ep{}.pth')
        if torch_home is None:
            torch_home = os.path.dirname(model_path)
        os.environ['TORCH_HOME'] = torch_home
        self.model_path = model_path

        self.model = MRCNN_model(num_classes)

        self.device = torch.device(device)
        self.model.to(self.device)

        params = [p for p in self.model.parameters() if p.requires_grad]
        self.optimizer = torch.optim.SGD(params, lr=0.001,
                                         momentum=0.9, weight_decay=0.0005)
        self.lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
                self.optimizer, milestones=[15, 25, 35, 50], gamma=0.1) # noqa 126
        self.losses = []
        self.metrics = []
        self.save_hist_freq = save_hist_freq

    def prepare_data(self, Dataset_class: Type[torch.utils.data.Dataset], batch_size: int,
                     add_aug: bool = True, dataset_args: dict = {}, aug_args: dict = {}) -> None:
        """
        Create data loaders for train and test.
        """
        dataset = Dataset_class(self.train_path, get_augmentation(add_aug, **aug_args),
                                **dataset_args)
        dataset_val = Dataset_class(self.val_path, get_augmentation(add_aug, **aug_args),
                                    **dataset_args)

        self.data_loader = torch.utils.data.DataLoader(
            dataset, batch_size=batch_size, shuffle=True, num_workers=4,
            collate_fn=utils.collate_fn)

        self.data_loader_val = torch.utils.data.DataLoader(
            dataset_val, batch_size=batch_size, shuffle=False, num_workers=4,
            collate_fn=utils.collate_fn)

    def train_model(self, num_epochs: int, print_freq: int = 120) -> None:
        """
        Train initialized model. prent_freq controls freqency for metrics output.
        """
        for epoch in range(num_epochs):
            epoch_losses = {}
            epoch_metrics = {}
            losses = deepcopy(
                train_one_epoch(self.model, self.optimizer, self.data_loader,
                                self.device, epoch, print_freq=print_freq).meters)
            self.lr_scheduler.step()
            losses = {k: v.avg for k, v in losses.items()}
            epoch_losses.update(losses)

            metr = deepcopy(evaluate(self.model, self.data_loader, device=self.device).coco_eval)
            val_metr = deepcopy(
                evaluate(self.model, self.data_loader_val, device=self.device).coco_eval)
            metr = {k: v.stats for k, v in metr.items()}
            val_metr = {'val_' + k: v.stats for k, v in val_metr.items()}
            epoch_metrics.update(metr)
            epoch_metrics.update(val_metr)

            torch.save(self.model.state_dict(), self.model_path.format(epoch))

            self.losses.append(epoch_losses)
            self.metrics.append(epoch_metrics)

            if epoch % self.save_hist_freq == 0 or epoch == num_epochs - 1:
                self.save_history(epoch)

    def make_prediction(self, idx: int = 0) -> Tuple[torch.Tensor, ...]:
        """
        Make prediction for first batch.
        """
        image, gt = self.data_loader_val.dataset[idx]

        image = image.to(self.device)
        prediction = self.model(image.reshape((1,) + image.shape))[0]
        for prm in ['masks', 'boxes']:
            prediction[prm] = move_to_device(prediction[prm], torch.device('cpu'))
            if gt is not None:
                gt[prm] = move_to_device(gt[prm], torch.device('cpu'))
        image = image.to(torch.device('cpu'))
        prediction["image_id"] = gt["image_id"]
        return image, prediction, gt

    def measure_inf_time(self) -> float:
        """
        Measure inference time for one batch.
        """
        for images, gts in self.data_loader_val:
            break
        images = move_to_device(images, self.device)
        start = time.time()
        self.model(images)
        end = time.time()
        print(f"For {len(images)} images with shape {images[0].shape}\
                inference time is {end-start}.")
        return end - start

    def extract_metrics(self) -> List[dict]:
        """
        From all of the types of IoUs get only
        Average Precision and Average Recall at IoU=0.50:0.95.
        """
        new_metrics = []
        metrics = self.metrics
        for line in metrics:
            new_line = {}
            for k, v in line.items():
                new_line[k + '_AP'] = v[0]
                new_line[k + '_AR'] = v[6]
            new_metrics.append(new_line)
        return new_metrics

    def save_history(self, epoch) -> None:
        """
        Save losses and metrics data.
        """
        dirname = os.path.dirname(self.model_path)

        metrics = self.extract_metrics()
        metrics = pd.DataFrame(metrics, index=np.arange(len(metrics)))
        metrics.index.name = 'epoch'
        metrics.to_csv(os.path.join(dirname, 'metrics.csv'))

        losses = pd.DataFrame(self.losses, index=np.arange(len(self.losses)))
        losses.index.name = 'epoch'
        losses.to_csv(os.path.join(dirname, 'losses.csv'))

        print(f"History for {epoch} epochs is saved.")


def move_to_device(objects: list, device: torch.device) -> list:
    """
    Move all objects to chosen device.
    """
    objects = list(obj.to(device) for obj in objects)
    return objects
