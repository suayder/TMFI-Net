import argparse
import glob, os
# os.environ['CUDA_VISIBLE_DEVICES'] = '0'
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:512"
from sched import scheduler
import torch
import sys
import time
import torch.nn as nn
import pickle
from torch.autograd import Variable
from torchvision import transforms, utils
from PIL import Image
from torch.utils.data import DataLoader
import numpy as np
import torch.nn.init as init
import torch.nn.functional as F
from dataloader import *
from loss import *
import cv2
from model import *
from utils1 import *
from tensorboardX import SummaryWriter

parser = argparse.ArgumentParser()
parser.add_argument('--kldiv', default=True, type=bool)
parser.add_argument('--cc', default=True, type=bool)
parser.add_argument('--nss', default=True, type=bool)
parser.add_argument('--sim', default=True, type=bool)
parser.add_argument('--l1', default=False, type=bool)
parser.add_argument('--kldiv_coeff', default=1.0, type=float)
parser.add_argument('--cc_coeff', default=-1.0, type=float)
parser.add_argument('--sim_coeff', default=-1.0, type=float)
parser.add_argument('--nss_coeff', default=-1.0, type=float)
parser.add_argument('--l1_coeff', default=1.0, type=float)

parser.add_argument('--no_epochs', default=300, type=int)
parser.add_argument('--lr', default=1e-4, type=float)
parser.add_argument('--batch_size', default=2, type=int)
parser.add_argument('--log_interval', default=10, type=int)
parser.add_argument('--no_workers', default=4, type=int)
parser.add_argument('--model_val_path', default="/scratch/suayder/jbcs_experiments/", type=str)
parser.add_argument('--clip_size', default=32, type=int)

parser.add_argument('--train_path_data', default="/scratch/suayder/urbanaccess_dhf1/train", type=str)
parser.add_argument('--val_path_data', default="/scratch/suayder/urbanaccess_dhf1/val", type=str)
parser.add_argument('--load_path', type=str, default='best_DHF1K.pth')
parser.add_argument('--dataset', default="DHF1KDataset", type=str)
parser.add_argument('--alternate', default=1, type=int)
args = parser.parse_args()

if args.dataset == "DHF1KDataset":
    train_dataset = DHF1KDataset(args.train_path_data, args.clip_size, mode="train", alternate=args.alternate)
    val_dataset = DHF1KDataset(args.val_path_data, args.clip_size, mode="val", alternate=args.alternate)
else:
    train_dataset = Hollywood_UCFDataset(args.train_path_data, args.clip_size, mode="train")
    val_dataset = Hollywood_UCFDataset(args.val_path_data, args.clip_size, mode="val")

train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True,
                                           num_workers=args.no_workers)
val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=1, shuffle=False, num_workers=args.no_workers)

# model = VideoSaliencyModel(pretrain=None)
model = VideoSaliencyModel(pretrain=args.load_path)

if not os.path.exists(args.model_val_path):
    os.makedirs(args.model_val_path)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
if torch.cuda.device_count() > 1:
    print("Let's use", torch.cuda.device_count(), "GPUs!")
    model = nn.DataParallel(model)
model.to(device)

optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

def is_finite(model):
    return any(torch.isfinite(p).all() for p in model.parameters())

def train(model, optimizer, loader, epoch, device, args, writer):
    # torch.autograd.set_detect_anomaly(True)
    model.train()

    total_loss = AverageMeter()
    cur_loss = AverageMeter()

    for idx, sample in enumerate(loader):
        img_clips = sample[0]
        gt_sal = sample[1]
        img_clips = img_clips.to(device)
        img_clips = img_clips.permute((0, 2, 1, 3, 4))
        gt_sal = gt_sal.to(device)
        assert torch.isfinite(img_clips).all() and torch.isfinite(gt_sal).all(), 'non finite value at the input'

        z0 = model(img_clips)
    
        assert z0.size() == gt_sal.size()
        loss = loss_func(z0, gt_sal, args)
        assert torch.isfinite(loss).all(), 'non finite value returned at loss function'

        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

        for name, param in model.named_parameters():
            if param.grad is not None:
                if torch.isnan(param.grad).any():
                    print(f"❌ NaN detected in gradient of {name}!")
                    exit()  # Stop execution if NaN is found
                if torch.isinf(param.grad).any():
                    print(f"⚠️ Inf detected in gradient of {name}!")
                    exit()

        optimizer.step()
        total_loss.update(loss.item())
        cur_loss.update(loss.item())

        if idx % args.log_interval == 0:
            print('epoch: {:2d}, idx: {:5d}, avg_loss: {:.3f}'.format(epoch, idx, cur_loss.avg))
            writer.add_scalar('Loss1', cur_loss.avg, global_step=epoch)
            cur_loss.reset()
            sys.stdout.flush()

    print('epoch: {:2d}, Avg_loss: {:.3f}'.format(epoch, total_loss.avg))
    writer.add_scalar('Loss2', total_loss.avg, global_step=epoch)
    sys.stdout.flush()

    return total_loss.avg


def validate(model, loader, epoch, device, args, writer):
    model.eval()
    total_loss = AverageMeter()
    total_cc_loss = AverageMeter()
    total_sim_loss = AverageMeter()
    tic = time.time()
    for idx, sample in enumerate(loader):
        img_clips = sample[0]
        gt_sal = sample[1]
        img_clips = img_clips.to(device)
        img_clips = img_clips.permute((0, 2, 1, 3, 4))

        pred_sal = model(img_clips)

        gt_sal = gt_sal.squeeze(0).numpy()
        pred_sal = pred_sal.cpu().squeeze(0).numpy()
        pred_sal = cv2.resize(pred_sal, (gt_sal.shape[1], gt_sal.shape[0]))
        pred_sal = blur(pred_sal).unsqueeze(0).cuda()
        gt_sal = torch.FloatTensor(gt_sal).unsqueeze(0).cuda()

        assert pred_sal.size() == gt_sal.size()
        loss = loss_func(pred_sal, gt_sal, args)
        cc_loss = cc(pred_sal, gt_sal)
        sim_loss = similarity(pred_sal, gt_sal)

        total_loss.update(loss.item())
        total_cc_loss.update(cc_loss.item())
        total_sim_loss.update(sim_loss.item())

    writer.add_scalar('CC', total_cc_loss.avg, global_step=epoch)
    writer.add_scalar('SIM', total_sim_loss.avg, global_step=epoch)
    writer.add_scalar('Loss', total_loss.avg, global_step=epoch)
    print('epoch：{:2d}, avg_loss: {:.3f}, cc_loss: {:.3f}, sim_loss: {:.3f}, time: {:2f}h'.format
          (epoch, total_loss.avg, total_cc_loss.avg, total_sim_loss.avg, (time.time() - tic) / 3600))
    sys.stdout.flush()

    return total_cc_loss.avg


writer = SummaryWriter('logs')
best_loss = 0
for epoch in range(0, args.no_epochs):
    loss = train(model, optimizer, train_loader, epoch, device, args, writer)
    if epoch % 3 == 0:
        with torch.no_grad():
            cc_loss = validate(model, val_loader, epoch, device, args, writer)
            if epoch == 0:
                best_loss = cc_loss
            if best_loss < cc_loss:
                best_loss = cc_loss
                if torch.cuda.device_count() > 1:
                    torch.save(model.module.state_dict(), args.model_val_path + 'best_VSTNet.pth'.format(epoch))
                else:
                    torch.save(model.state_dict(), args.model_val_path + 'best_VSTNet.pth'.format(epoch))
                print('model saved at:', args.model_val_path + 'best_VSTNet.pth')
writer.close()
