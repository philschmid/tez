import multiprocessing
import time
import warnings
from dataclasses import dataclass
from typing import Optional

import torch
from accelerate import Accelerator
from torch.utils.data import DataLoader

from tez import enums
from tez.callbacks import CallbackRunner, Progress
from tez.logger import logger
from tez.utils import AverageMeter

from .config import TezConfig


warnings.filterwarnings("ignore", category=UserWarning)


@dataclass
class Tez:

    # required stuff
    model: torch.nn.Module

    # training essentials
    config: Optional[TezConfig] = None
    train_dataset = None
    valid_dataset = None
    optimizer = None
    scheduler = None

    # training parameters
    scaler = None
    num_gpu: Optional[int] = 0
    num_train_steps: Optional[int] = None
    num_valid_steps: Optional[int] = None

    # internals
    current_epoch = 0
    train_batch_index = 0
    valid_batch_index = 0
    _train_step = 0
    _valid_step = 0
    _test_step = 0
    _model_state = None
    _train_state = None
    train_loader_bs = None
    valid_loader_bs = None

    # multi-gpu
    local_rank = -1
    world_size = 1

    # metrics
    train_meter = None
    valid_meter = None

    metrics = {}
    metrics["train"] = {}
    metrics["valid"] = {}
    metrics["test"] = {}
    _progress = None

    def _init_trainer(self, train_dataset, valid_dataset, config, **kwargs):
        self.config = config
        self.train_dataset = train_dataset
        self.valid_dataset = valid_dataset
        self._accel = Accelerator(device_placement=True)
        self.config.device = self._accel.device

        if "train_loader" in kwargs:
            self.train_loader = kwargs["train_loader"]
        else:
            self.train_loader = None
        if "valid_loader" in kwargs:
            self.valid_loader = kwargs["valid_loader"]
        else:
            self.valid_loader = None

        if "train_sampler" in kwargs:
            self.train_sampler = kwargs["train_sampler"]
        else:
            self.train_sampler = None

        if "valid_sampler" in kwargs:
            self.valid_sampler = kwargs["valid_sampler"]
        else:
            self.valid_sampler = None

        if "train_collate_fn" in kwargs:
            self.train_collate_fn = kwargs["train_collate_fn"]
        else:
            self.train_collate_fn = None

        if "valid_collate_fn" in kwargs:
            self.valid_collate_fn = kwargs["valid_collate_fn"]
        else:
            self.valid_collate_fn = None

        self.num_train_steps = int(len(self.train_dataset) / self.config.training_batch_size * self.config.epochs)
        if self.valid_dataset:
            self.num_valid_steps = int(len(self.valid_dataset) / self.config.validation_batch_size)
        else:
            self.num_valid_steps = None

        self._progress = Progress(num_train_steps=self.num_train_steps, num_valid_steps=self.num_valid_steps)

        if "callbacks" in kwargs:
            self.callbacks = [self._progress] + kwargs["callbacks"]
        else:
            self.callbacks = [self._progress]

        if self.config.num_jobs == -1:
            self.config.num_jobs = multiprocessing.cpu_count()
            if self.config.num_jobs > 4:
                self.config.num_jobs -= 2

        if self.train_loader is None:
            self.train_loader = DataLoader(
                self.train_dataset,
                batch_size=self.config.training_batch_size,
                num_workers=self.config.num_jobs,
                sampler=self.train_sampler,
                shuffle=self.config.train_shuffle,
                collate_fn=self.train_collate_fn,
                drop_last=self.config.train_drop_last,
                pin_memory=self.config.pin_memory,
            )

        if self.valid_loader is None:
            if self.valid_dataset is not None:
                self.valid_loader = DataLoader(
                    self.valid_dataset,
                    batch_size=self.config.validation_batch_size,
                    num_workers=self.config.num_jobs,
                    sampler=self.valid_sampler,
                    shuffle=self.config.valid_shuffle,
                    collate_fn=self.valid_collate_fn,
                    drop_last=self.config.valid_drop_last,
                    pin_memory=self.config.pin_memory,
                )

        self.optimizer, self.scheduler = self.model.optimizer_scheduler()

        if self.optimizer is None:
            raise Exception("No optimizer found")

        if self.valid_loader is not None:
            self.model, self.optimizer, self.train_loader, self.valid_loader = self._accel.prepare(
                self.model, self.optimizer, self.train_loader, self.valid_loader
            )
        else:
            self.model, self.optimizer, self.train_loader = self._accel.prepare(
                self.model, self.optimizer, self.train_loader
            )

        self._callback_runner = CallbackRunner(self.callbacks, self)
        self.train_state = enums.TrainingState.TRAIN_START

    @property
    def model_state(self):
        return self._model_state

    @model_state.setter
    def model_state(self, value):
        self._model_state = value

    @property
    def train_state(self):
        return self._train_state

    @train_state.setter
    def train_state(self, value):
        self._train_state = value
        if self._callback_runner is not None:
            if self._accel.is_local_main_process:
                self._callback_runner(value)

    def name_to_metric(self, metric_name):
        if metric_name == "current_epoch":
            return self.current_epoch
        v_1 = metric_name.split("_")[0]
        v_2 = "_".join(metric_name.split("_")[1:])
        return self.metrics[v_1][v_2]

    def update_metrics(self, losses, monitor):
        if self._model_state == enums.ModelState.END:
            return
        self.metrics[self._model_state.value].update(monitor)
        self.metrics[self._model_state.value]["loss"] = losses.avg

    def save(self, model_path, weights_only=False):
        # self._accel.wait_for_everyone()
        model_state_dict = self._accel.unwrap_model(self.model).state_dict()

        if weights_only:
            if self._accel.is_main_process:
                self._accel.save(
                    model_state_dict,
                    model_path,
                )
            return

        if self.optimizer is not None:
            opt_state_dict = self.optimizer.state_dict()
        else:
            opt_state_dict = None

        if self.scheduler is not None:
            sch_state_dict = self.scheduler.state_dict()
        else:
            sch_state_dict = None

        model_dict = {}
        model_dict["state_dict"] = model_state_dict
        model_dict["optimizer"] = opt_state_dict
        model_dict["scheduler"] = sch_state_dict
        model_dict["config"] = self.config

        if self._accel.is_main_process:
            self._accel.save(
                model_dict,
                model_path,
            )

    def load(self, model_path, weights_only=False, config: TezConfig = None):
        if config is None:
            config = TezConfig()

        self._accel.wait_for_everyone()

        model_dict = torch.load(model_path, map_location="cpu")
        if weights_only:
            self._accel.unwrap_model(self.model).load_state_dict(model_dict)
        else:
            self._accel.unwrap_model(self.model).load_state_dict(model_dict["state_dict"])
            self.optimizer.load_state_dict(model_dict["optimizer"])

    def model_fn(self, data):
        output, loss, metrics = self.model(**data)
        metrics = self._accel.gather(metrics)
        metrics = {key: value.mean() for key, value in metrics.items()}
        return output, loss, metrics

    def _zero_grad(self):
        if self.config.gradient_accumulation_steps == 1 and self.train_batch_index == 0:
            self.model.zero_grad()

    def _backward(self, loss):
        loss = loss / self.config.gradient_accumulation_steps
        self._accel.backward(loss)

    def _clip_grad_norm(self):
        if self.config.clip_grad_norm != -1:
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.clip_grad_norm)

    def _step(self):
        is_bi_mod_acc_zero = (self.train_batch_index + 1) % self.config.gradient_accumulation_steps == 0
        is_bi_end = self.train_batch_index + 1 == self.train_loader_bs
        if is_bi_mod_acc_zero or is_bi_end:

            self.optimizer.step()

            if self.scheduler is not None:
                if self.config.step_scheduler_after == "batch":
                    if self.config.step_scheduler_metric is None:
                        self.scheduler.step()
                    else:
                        step_metric = self.name_to_metric(self.config.step_scheduler_metric)
                        self.scheduler.step(step_metric)

            self.model.zero_grad()

    def train_step(self, data):
        self._zero_grad()
        _, loss, metrics = self.model_fn(data)
        self._backward(loss)
        self._clip_grad_norm()
        self._step()
        return loss, metrics

    def predict_step(self, data):
        _, loss, metrics = self.model_fn(data)
        metrics = self._accel.gather(metrics)
        metrics = {key: value.mean() for key, value in metrics.items()}
        return loss, metrics

    def _set_training_epoch_start(self, data_loader):
        self.model_state = enums.ModelState.TRAIN
        self.train_state = enums.TrainingState.TRAIN_EPOCH_START
        self.train_loader_bs = data_loader.batch_sampler.batch_size
        self.model.train()
        if self.config.gradient_accumulation_steps > 1:
            self.optimizer.zero_grad()

    def _set_training_epoch_end(self, losses, monitor):
        self.update_metrics(losses=losses, monitor=monitor)
        self.train_state = enums.TrainingState.TRAIN_EPOCH_END

    def _update_monitor(self, losses, metrics):
        monitor = {}

        if self._model_state == enums.ModelState.TRAIN:
            metrics_meter = self.train_meter
            _bs = self.train_loader_bs
        elif self._model_state == enums.ModelState.VALID:
            metrics_meter = self.valid_meter
            _bs = self.valid_loader_bs
        else:
            raise ValueError("Invalid model state")

        for m_m in metrics_meter:
            metrics_meter[m_m].update(metrics[m_m].cpu().detach().numpy(), _bs)
            monitor[m_m] = metrics_meter[m_m].avg

        if self._model_state == enums.ModelState.TRAIN:
            self.train_meter = metrics_meter
        elif self._model_state == enums.ModelState.VALID:
            self.valid_meter = metrics_meter
        else:
            raise ValueError("Invalid model state")
        self.update_metrics(losses=losses, monitor=monitor)
        return monitor

    def _update_loss_metrics(self, losses, loss, metrics):
        if self._model_state == enums.ModelState.TRAIN:
            if self.train_batch_index == 0:
                self.train_meter = {k: AverageMeter() for k in metrics}
            losses.update(loss.item() * self.config.gradient_accumulation_steps, self.train_loader_bs)
        elif self._model_state == enums.ModelState.VALID:
            if self.valid_batch_index == 0:
                self.valid_meter = {k: AverageMeter() for k in metrics}
            loss = self._accel.gather(loss).mean()
            losses.update(loss.item(), self.valid_loader_bs)
        else:
            raise ValueError("Invalid model state")

        monitor = self._update_monitor(losses, metrics)

        if self._model_state == enums.ModelState.TRAIN:
            self._train_step += 1
        elif self._model_state == enums.ModelState.VALID:
            self._valid_step += 1
        else:
            raise ValueError("Invalid model state")
        return losses, monitor

    def train(self, data_loader):
        self._set_training_epoch_start(data_loader)
        losses = AverageMeter()
        for batch_index, data in enumerate(data_loader):
            self.train_batch_index = batch_index
            self.train_state = enums.TrainingState.TRAIN_STEP_START
            loss, metrics = self.train_step(data)
            losses, monitor = self._update_loss_metrics(losses, loss, metrics)
            self.train_state = enums.TrainingState.TRAIN_STEP_END

            if self.valid_loader and self.config.val_strategy == "batch":
                if self._train_step % self.config.val_steps == 0 or self._train_step == self.num_train_steps:
                    self.validate(self.valid_loader)

            if self._model_state == enums.ModelState.END:
                break

        self._set_training_epoch_end(losses, monitor)

    def _set_validation_epoch_start(self, data_loader):
        self.train_state = enums.TrainingState.VALID_EPOCH_START
        self.model_state = enums.ModelState.VALID
        self.valid_loader_bs = data_loader.batch_sampler.batch_size
        self.model.eval()

    def _set_validation_epoch_end(self, losses, monitor):
        self.update_metrics(losses=losses, monitor=monitor)
        self.train_state = enums.TrainingState.VALID_EPOCH_END
        if self.config.val_strategy == "batch" and self._model_state != enums.ModelState.END:
            self.model_state = enums.ModelState.TRAIN
            self.train_state = enums.TrainingState.TRAIN_EPOCH_START
            self.model.train()

    def validate(self, data_loader):
        self._set_validation_epoch_start(data_loader)
        losses = AverageMeter()

        for batch_index, data in enumerate(data_loader):
            self.valid_batch_index = batch_index
            self.train_state = enums.TrainingState.VALID_STEP_START
            with torch.no_grad():
                loss, metrics = self.predict_step(data)
            losses, monitor = self._update_loss_metrics(losses, loss, metrics)
            self.train_state = enums.TrainingState.VALID_STEP_END
        self._set_validation_epoch_end(losses, monitor)

    def _step_scheduler_after_epoch(self):
        if self.scheduler is not None:
            if self.config.step_scheduler_after == "epoch":
                if self.config.step_scheduler_metric is None:
                    self.scheduler.step()
                else:
                    step_metric = self.name_to_metric(self.config.step_scheduler_metric)
                    self.scheduler.step(step_metric)

    def fit(self, train_dataset, valid_dataset=None, config: TezConfig = None, **kwargs):
        if config is None:
            config = TezConfig()
        self._init_trainer(train_dataset, valid_dataset, config, **kwargs)
        for _ in range(self.config.epochs):
            self.train_state = enums.TrainingState.EPOCH_START
            self.train(self.train_loader)
            if self.valid_loader and self.config.val_strategy == "epoch":
                self.validate(self.valid_loader)
            self._step_scheduler_after_epoch()
            self.train_state = enums.TrainingState.EPOCH_END
            if self._model_state == enums.ModelState.END:
                time.sleep(2)
                break
            self.current_epoch += 1
        self.train_state = enums.TrainingState.TRAIN_END

    def process_output(self, output):
        output = output.cpu().detach().numpy()
        return output

    def predict(self, dataset, **kwargs):

        self.model_state = enums.ModelState.TEST

        if "sampler" in kwargs:
            sampler = kwargs["sampler"]
        else:
            sampler = None

        if "collate_fn" in kwargs:
            collate_fn = kwargs["collate_fn"]
        else:
            collate_fn = None

        if "batch_size" in kwargs:
            batch_size = kwargs["batch_size"]
        else:
            batch_size = self.config.test_batch_size

        if "num_jobs" in kwargs:
            num_jobs = kwargs["num_jobs"]
        else:
            num_jobs = self.config.num_jobs

        if "pin_memory" in kwargs:
            pin_memory = kwargs["pin_memory"]
        else:
            pin_memory = self.config.pin_memory

        if num_jobs == -1:
            num_jobs = multiprocessing.cpu_count()
            if num_jobs > 4:
                num_jobs -= 2

        if batch_size == 1:
            num_jobs = 0

        data_loader = DataLoader(
            dataset,
            batch_size=batch_size,
            num_workers=num_jobs,
            sampler=sampler,
            collate_fn=collate_fn,
            pin_memory=pin_memory,
        )

        if self.model.training:
            self.model.eval()

        for data in data_loader:
            with torch.no_grad():
                out, _, _ = self.model_fn(data)
                out = self.process_output(out)
                yield out
