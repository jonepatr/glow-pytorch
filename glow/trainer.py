import datetime
import os
import re
from os.path import join

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from Speech2Face import utils
from tensorboardX import SummaryWriter
from tqdm import tqdm

from . import thops
from .config import JsonConfig
from .models import Glow
from .utils import VideoRender, load, plot_prob, save

# torch.set_num_threads(1)


def fix_img(img):
    img -= img.min()
    img /= img.max()
    return img
    # return (img - img.min()) / np.max(img.max(), np.abs(img.min()))
    # return (img + 1) / 2


def dump_model_to_tensorboard(model, writer, channels=64, time=64):
    dummy_input = torch.ones((1, channels, time, 1)).to("cuda")  # en input som fungerar
    writer.add_graph(model, dummy_input)


class Trainer(object):
    def __init__(
        self,
        graph,
        optim,
        lrschedule,
        loaded_step,
        devices,
        data_device,
        train_dataset,
        validation_dataset,
        hparams,
    ):
        if isinstance(hparams, str):
            hparams = JsonConfig(hparams)
        # set members
        # append date info
        date = str(datetime.datetime.now())
        date = (
            date[: date.rfind(":")].replace("-", "").replace(":", "").replace(" ", "_")
        )
        self.log_dir = os.path.join(hparams.Dir.log_root, "log_" + date)
        self.checkpoints_dir = os.path.join(self.log_dir, "checkpoints")
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        # write hparams
        hparams.dump(self.log_dir)
        if not os.path.exists(self.checkpoints_dir):
            os.makedirs(self.checkpoints_dir)
        self.checkpoints_gap = hparams.Train.checkpoints_gap
        self.max_checkpoints = hparams.Train.max_checkpoints
        # model relative
        self.graph = graph
        self.optim = optim
        self.weight_y = hparams.Train.weight_y
        # grad operation
        self.max_grad_clip = hparams.Train.max_grad_clip
        self.max_grad_norm = hparams.Train.max_grad_norm
        # copy devices from built graph
        self.devices = devices
        self.data_device = data_device
        # number of training batches
        self.batch_size = hparams.Train.batch_size
        self.data_loader = DataLoader(
            train_dataset,
            batch_size=self.batch_size,
            # num_workers=20,
            shuffle=True,
            drop_last=True,
            pin_memory=True,
        )
        self.validation_loader = DataLoader(
            validation_dataset,
            batch_size=self.batch_size,
            drop_last=True,
            pin_memory=True,
        )
        self.n_epoches = hparams.Train.num_batches + len(self.data_loader) - 1
        self.n_epoches = self.n_epoches // len(self.data_loader)
        self.global_step = 0
        # lr schedule
        self.lrschedule = lrschedule
        self.loaded_step = loaded_step
        # data relative
        self.y_classes = hparams.Glow.y_classes
        self.y_condition = hparams.Glow.y_condition
        self.y_criterion = hparams.Criterion.y_condition
        assert self.y_criterion in ["multi-classes", "single-class"]

        # log relative
        # tensorboard
        self.writer = SummaryWriter(log_dir=self.log_dir)

        if False:
            print("DIMPUTING MODLLES")
            dump_model_to_tensorboard(self.graph, self.writer)
            print("done")
        self.scalar_log_gaps = hparams.Train.scalar_log_gap
        self.plot_gaps = hparams.Train.plot_gap
        self.inference_gap = hparams.Train.inference_gap
        self.validation_gap = hparams.Train.validation_gap
        self.video_url = hparams.Misc.video_url
        self.video_render = VideoRender(
            hparams.Misc.render_url, ffmpeg_bin=hparams.Misc.ffmpeg_bin
        )

    def train(self):
        # set to training state

        self.global_step = self.loaded_step
        # begin to train
        for epoch in range(self.n_epoches):
            print("epoch", epoch)
            progress = tqdm(self.data_loader)
            for i_batch, batch in enumerate(progress):
                self.graph.train()
                # update learning rate
                lr = self.lrschedule["func"](
                    global_step=self.global_step, **self.lrschedule["args"]
                )
                for param_group in self.optim.param_groups:
                    param_group["lr"] = lr
                self.optim.zero_grad()
                if self.global_step % self.scalar_log_gaps == 0:
                    self.writer.add_scalar("lr/lr", lr, self.global_step)
                # get batch data
                for k in batch:
                    try:
                        batch[k] = batch[k].to(self.data_device)
                    except AttributeError:
                        pass
                x = batch["x"]

                y = None
                y_onehot = None
                if self.y_condition:
                    if self.y_criterion == "multi-classes":
                        assert (
                            "y_onehot" in batch
                        ), "multi-classes ask for `y_onehot` (torch.FloatTensor onehot)"
                        y_onehot = batch["y_onehot"]
                    elif self.y_criterion == "single-class":
                        assert (
                            "y" in batch
                        ), "single-class ask for `y` (torch.LongTensor indexes)"
                        y = batch["y"]
                        y_onehot = thops.onehot(y, num_classes=self.y_classes)

                # at first time, initialize ActNorm
                if self.global_step == 0:
                    self.graph(
                        x[: self.batch_size // len(self.devices), ...],
                        batch["audio_features"][
                            : self.batch_size // len(self.devices), ...
                        ],
                        y_onehot[: self.batch_size // len(self.devices), ...]
                        if y_onehot is not None
                        else None,
                    )
                # parallel
                if len(self.devices) > 1 and not hasattr(self.graph, "module"):
                    print("[Parallel] move to {}".format(self.devices))
                    self.graph = torch.nn.parallel.DataParallel(
                        self.graph, self.devices, self.devices[0]
                    )

                # forward phase

                z, nll, y_logits = self.graph(
                    x=x, audio_features=batch["audio_features"], y_onehot=y_onehot
                )

                # loss
                loss_generative = Glow.loss_generative(nll)
                loss_classes = 0

                if self.global_step % self.scalar_log_gaps == 0:
                    for name, param in self.graph.named_parameters():
                        self.writer.add_histogram(
                            name, param.clone().cpu().data.numpy(), self.global_step
                        )
                    self.writer.add_scalar(
                        "loss/loss_generative", loss_generative, self.global_step
                    )
                    if self.y_condition:
                        self.writer.add_scalar(
                            "loss/loss_classes", loss_classes, self.global_step
                        )
                loss = loss_generative + loss_classes * self.weight_y

                # backward
                self.graph.zero_grad()
                self.optim.zero_grad()
                loss.backward()
                # operate grad
                if self.max_grad_clip is not None and self.max_grad_clip > 0:
                    torch.nn.utils.clip_grad_value_(
                        self.graph.parameters(), self.max_grad_clip
                    )
                if self.max_grad_norm is not None and self.max_grad_norm > 0:
                    grad_norm = torch.nn.utils.clip_grad_norm_(
                        self.graph.parameters(), self.max_grad_norm
                    )
                    if self.global_step % self.scalar_log_gaps == 0:
                        self.writer.add_scalar(
                            "grad_norm/grad_norm", grad_norm, self.global_step
                        )
                # step
                self.optim.step()
                self.graph.eval()
                # checkpoints
                if (
                    self.global_step % self.checkpoints_gap == 0
                    and self.global_step > 0
                ):
                    save(
                        global_step=self.global_step,
                        graph=self.graph,
                        optim=self.optim,
                        pkg_dir=self.checkpoints_dir,
                        is_best=True,
                        max_checkpoints=self.max_checkpoints,
                    )
                with torch.no_grad():
                    # inference
                    if self.global_step % self.inference_gap == 0 or os.path.isfile(
                        "do_inference"
                    ):
                        for val_batch in self.validation_loader:

                            i = 0

                            x = self.graph(
                                z=None,
                                audio_features=val_batch["audio_features"],
                                y_onehot=y_onehot,
                                eps_std=1,
                                reverse=True,
                            )
                            new_path = os.path.join(
                                self.writer.log_dir,
                                "samples",
                                f"{str(self.global_step).zfill(7)}-{i}.mp4",
                            )
                            self.video_render.render(
                                new_path,
                                x[i].cpu().detach().numpy().transpose(1, 0, 2),
                                val_batch["audio_path"][i],
                                val_batch["video_path"][i],
                                val_batch["first_frame"][i],
                            )
                            self.writer.add_text(
                                f"video",
                                self.video_url
                                + self.writer.log_dir
                                + f"/samples/{str(self.global_step).zfill(7)}-{i}.mp4",
                                self.global_step,
                            )
                            break

                        if os.path.isfile("do_inference"):
                            os.remove("do_inference")

                    if self.global_step % self.validation_gap == 0:

                        validation_loss = 0
                        for i_val_batch, val_batch in enumerate(
                            tqdm(self.validation_loader, desc="Validation")
                        ):
                            z, nll, y_logits = self.graph(
                                x=val_batch["x"],
                                audio_features=val_batch["audio_features"],
                            )

                            # loss
                            validation_loss += Glow.loss_generative(nll)
                        self.writer.add_scalar(
                            "loss/validation_loss_generative",
                            validation_loss / (i_val_batch + 1),
                            self.global_step,
                        )

                self.global_step += 1

        self.writer.export_scalars_to_json(
            os.path.join(self.log_dir, "all_scalars.json")
        )
        self.writer.close()
        self.writer.close()
