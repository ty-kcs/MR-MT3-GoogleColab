"""
MT3 XL training. 
To use random order, use `dataset.dataset_2_random_xl`.
"""
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 

from torch.optim import AdamW
from omegaconf import OmegaConf
import shutil
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateMonitor
from pytorch_lightning.callbacks import TQDMProgressBar
from pytorch_lightning.loggers import TensorBoardLogger
from transformers import T5Config
from dataset.dataset_2_random_xl import SlakhDataset, collate_fn    # NOTE: random is commented out now in dataloader
from torch.utils.data import DataLoader
from models.t5_xl import T5WithXLDecoder                            # vanilla XL
# from models.t5_xl_instrument import T5WithXLDecoder               # XL but only carry forward instrument + timestamp
import torch.nn as nn
from utils import get_cosine_schedule_with_warmup, get_result_dir
import torch
import json
import pytorch_lightning as pl
import os
pl.seed_everything(365)
import copy
import argparse
import mlflow
import datetime
import math
import numpy as np


class MT3Net(pl.LightningModule):

    def __init__(self, config, model_config_path='config/mt3_config.json', result_dir='./results'):
        super().__init__()

        self.config = config
        self.result_dir = result_dir
        with open(model_config_path) as f:
            config_dict = json.load(f)
        config = T5Config.from_dict(config_dict)
        self.model: nn.Module = T5WithXLDecoder(config)

    def forward(self, *args, **kwargs):
        return self.model.forward(*args, **kwargs)

    def training_step(self, batch, batch_idx):
        inputs, targets = batch
        outputs = self.forward(
            inputs=inputs, 
            labels=targets
        )
        self.log('train_loss', outputs.loss, prog_bar=True, on_step=True, on_epoch=False, sync_dist=True)
        return outputs.loss

    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        inputs, targets = batch
        outputs = self.forward(
            inputs=inputs, 
            labels=targets
        )
        self.log('val_loss', outputs.loss, prog_bar=True, on_step=False, on_epoch=True, sync_dist=True)
        return outputs.loss

    def configure_optimizers(self):
        optimizer = AdamW(self.model.parameters(), self.config.lr)
        warmup_step = int(self.config.warmup_steps)
        print('warmup step: ', warmup_step)
        schedule = {
            'scheduler': get_cosine_schedule_with_warmup(
                optimizer=optimizer, 
                num_warmup_steps=warmup_step, 
                num_training_steps=1289*self.config.num_epochs,
                min_lr=5e-5
            ),
            'interval': 'step',
            'frequency': 1
        }
        return [optimizer], [schedule]

    def train_dataloader(self):
        train_path = self.config.data.train_path
        # dataset = MidiMixIterDataset(train_path, mel_length=self.config.mel_length, event_length=self.config.event_length, **self.config.data.config)
        dataset = SlakhDataset(root_dir='/data/slakh2100_flac_redux/train/', mel_length=self.config.mel_length, event_length=self.config.event_length, **self.config.data.config)
        train_loader = DataLoader(
            dataset, 
            batch_size=1, 
            num_workers=12, 
            collate_fn=collate_fn
        )
        return train_loader

    def val_dataloader(self):
        test_path = self.config.data.test_path
        # dataset = MidiMixIterDataset(test_path, mel_length=self.config.mel_length, event_length=self.config.event_length, **self.config.data.config)
        dataset = SlakhDataset(root_dir='/data/slakh2100_flac_redux/validation/', mel_length=self.config.mel_length, event_length=self.config.event_length, **self.config.data.config)
        val_loader = DataLoader(
            dataset, 
            batch_size=1, 
            num_workers=12, 
            collate_fn=collate_fn
        )
        return val_loader


def main(config, model_config, result_dir, mode, path):
    model = MT3Net(config, model_config, result_dir)
    logger = TensorBoardLogger(save_dir='/'.join(result_dir.split('/')[:-1]),
                               name=result_dir.split('/')[-1])

    num_epochs = int(config['num_epochs'])
    lr_monitor = LearningRateMonitor(logging_interval='step')

    checkpoint_callback = ModelCheckpoint(
        monitor='val_loss', 
        mode='min', 
        save_last=True, 
        save_top_k=5, 
        save_weights_only=False, 
        filename='{epoch}-{step}-{val_loss:.4f}'
    )
    tqdm_callback = TQDMProgressBar(refresh_rate=1)

    if mode == "train":
        trainer = pl.Trainer(
            logger=logger,
            default_root_dir=os.path.join(os.getcwd(), 'logs'),
            callbacks=[lr_monitor, checkpoint_callback, tqdm_callback],
            precision=32,
            max_epochs=num_epochs,
            accelerator='gpu',
            accumulate_grad_batches=config.grad_accum,
            num_sanity_val_steps=2,
            log_every_n_steps=645,
            strategy="ddp_find_unused_parameters_false",
            devices=2
        )
        trainer.fit(model)

    else:
        model = MT3Net.load_from_checkpoint(
            path,
            config=config,
            model_config=model_config,
            result_dir=result_dir
        )
        model.eval()
        dic = {}
        for key in model.state_dict():
            if "model." in key:
                dic[key.replace("model.", "")] = model.state_dict()[key]
            else:
                dic[key] = model.state_dict()[key]
        torch.save(dic, path.replace(".ckpt", ".pth"))   # TODO: need to specify save .pth

        # trainer = pl.Trainer(
        #     logger=logger,
        #     default_root_dir=os.path.join(os.getcwd(), 'logs'),
        #     callbacks=[lr_monitor, checkpoint_callback, tqdm_callback],
        #     precision=32,
        #     max_epochs=num_epochs,
        #     accelerator='gpu',
        #     accumulate_grad_batches=config.grad_accum,
        #     num_sanity_val_steps=2,
        #     log_every_n_steps=645,
        #     check_val_every_n_epoch=30
        #     # strategy='ddp_find_unused_parameters_true',
        # )
        # trainer.validate(model, ckpt_path=path)



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default="train")      # option that takes a value
    parser.add_argument('--path')      # option that takes a value
    args = parser.parse_args()

    conf_file = 'config/config.yaml'
    model_config = 'config/mt3_config.json'
    print(f'Config {conf_file}')
    conf = OmegaConf.load(conf_file)
    
    result_dir = ""
    if args.mode == "train":
        datetime_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        result_dir = f"logs/results_norm_xl2_inst_2048_{datetime_str}"
        print('Creating: ', result_dir)
        os.makedirs(result_dir, exist_ok=False)
        shutil.copy(conf_file, f'{result_dir}/config.yaml')
    
    main(conf, model_config, result_dir, args.mode, args.path)