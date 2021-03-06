import argparse
import glob
import json
import multiprocessing
import os
import random
import re
from importlib import import_module
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import torch
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import wandb

import torch.nn.functional as F
import torch.nn as nn
from dataset import MaskBaseDataset, AgeBaseDataset
from loss import create_criterion

from sklearn.metrics import f1_score

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def grid_image(np_images, gts, preds, n=16, shuffle=False):
    batch_size = np_images.shape[0]
    assert n <= batch_size

    choices = random.choices(range(batch_size), k=n) if shuffle else list(range(n))
    figure = plt.figure(figsize=(12, 18 + 2))  # cautions: hardcoded, 이미지 크기에 따라 figsize 를 조정해야 할 수 있습니다. 
    plt.subplots_adjust(top=0.8)               # cautions: hardcoded, 이미지 크기에 따라 top 를 조정해야 할 수 있습니다. 
    n_grid = np.ceil(n ** 0.5)
    tasks = ["mask", "gender", "age"]
    for idx, choice in enumerate(choices):
        gt = gts[choice].item()
        pred = preds[choice].item()
        image = np_images[choice]
        # title = f"gt: {gt}, pred: {pred}"
        gt_decoded_labels = MaskBaseDataset.decode_multi_class(gt)
        pred_decoded_labels = MaskBaseDataset.decode_multi_class(pred)
        title = "\n".join([
            f"{task} - gt: {gt_label}, pred: {pred_label}"
            for gt_label, pred_label, task
            in zip(gt_decoded_labels, pred_decoded_labels, tasks)
        ])

        plt.subplot(n_grid, n_grid, idx + 1, title=title)
        plt.xticks([])
        plt.yticks([])
        plt.grid(False)
        plt.imshow(image, cmap=plt.cm.binary)

    return figure


def increment_path(path, exist_ok=False):
    """ Automatically increment path, i.e. runs/exp --> runs/exp0, runs/exp1 etc.
    Args:
        path (str or pathlib.Path): f"{model_dir}/{args.name}".
        exist_ok (bool): whether increment path (increment if False).
    """
    path = Path(path)
    if (path.exists() and exist_ok) or (not path.exists()):
        return str(path)
    else:
        dirs = glob.glob(f"{path}*")
        matches = [re.search(rf"%s(\d+)" % path.stem, d) for d in dirs]
        i = [int(m.groups()[0]) for m in matches if m]
        n = max(i) + 1 if i else 2
        return f"{path}{n}"


def train(data_dir, model_dir, args):
    seed_everything(args.seed)

    save_dir = increment_path(os.path.join(model_dir, args.name))
    
    wandb.init(project='daindain', entity='dannykm', name=Path(save_dir).stem)
    wandb.config.update(args)
    
    # -- settings
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    # -- dataset
    dataset_module = getattr(import_module("dataset"), args.dataset)  # default: AgeBaseDataset
    dataset = dataset_module(
        data_dir=data_dir,
    )
    num_classes = dataset.num_classes  # 11

    # -- augmentation
    transform_module = getattr(import_module("dataset"), args.augmentation)  # default: BaseAugmentation
    transform = transform_module(
        resize=args.resize,
        mean=dataset.mean,
        std=dataset.std,
    )
    dataset.set_transform(transform)

    # -- data_loader
    train_set, val_set = dataset.split_dataset()

    train_loader = DataLoader(
        train_set,
        batch_size=args.batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=True,
        pin_memory=use_cuda,
        drop_last=True,
    )

    val_loader = DataLoader(
        val_set,
        batch_size=args.valid_batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=False,
        pin_memory=use_cuda,
        drop_last=True,
    )

    # -- model
    model_module = getattr(import_module("model"), args.model)  # default: EfficientnetB4
    model = model_module(
        num_classes=num_classes
    ).to(device)
    model = torch.nn.DataParallel(model)

    # -- loss & metric
    criterion = create_criterion(args.criterion)  # default: cross_entropy
    opt_module = getattr(import_module("torch.optim"), args.optimizer)  # default: Adam
    optimizer = opt_module(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=args.lr,
        weight_decay=5e-4
    )
    scheduler = StepLR(optimizer, args.lr_decay_step, gamma=0.5)

    # -- logging
    logger = SummaryWriter(log_dir=save_dir)
    with open(os.path.join(save_dir, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(vars(args), f, ensure_ascii=False, indent=4)

        
    wandb.watch(model)
    
    
    best_val_acc = 0
    best_val_f1 = 0
    best_val_loss = np.inf
    for epoch in range(args.epochs):
        # train loop
        model.train()
        loss_value = 0
        matches = 0
        original_matches = 0
        f1 = 0
        final_f1 = 0
        input_figure = None
        
        for idx, train_batch in enumerate(train_loader):
            inputs, org_labels, labels = train_batch # org_labels: 3 classes, labels: 11 classes
            inputs = inputs.to(device)
            labels = labels.to(device)
            org_labels = org_labels.to(device)

            optimizer.zero_grad()

            outs = model(inputs)
            preds = torch.argmax(outs, dim=-1)
            age_loss = criterion(outs, labels) # loss btw 11 classes predictions and ground truth
            
            outs_normal = (outs - torch.min(outs)) / (torch.max(outs) - torch.min(outs)) # normalized model outputs
            final_outs = (torch.cat([torch.sum(outs_normal[:,:5], dim = 1, keepdim = True), torch.sum(outs_normal[:,5:11], dim = 1, keepdim = True), outs_normal[:,-1].view(-1,1)], dim = 1))
            final_preds = AgeBaseDataset.encode_original_age(preds) # convert 11 classes to 3 classes
            
            weights = torch.FloatTensor([1.5, 1.5, 7]).to(device)
            original_criterion = nn.CrossEntropyLoss(weight = weights) # Weighted Cross Entropy Loss
            original_loss = original_criterion(final_outs ,org_labels) # loss btw 3 classes predictions and ground truth
            
            loss = 0.5 * age_loss + original_loss

            loss.backward()
            optimizer.step()

            loss_value += loss.item() / 2
            matches += (preds == labels).sum().item()
            original_matches += (final_preds == org_labels).sum().item()
            f1 += f1_score(labels.cpu().numpy(), preds.cpu().numpy(), average='macro').item()
            final_f1 += f1_score(org_labels.cpu().numpy(), final_preds.cpu().numpy(), average='macro').item()
            
            if (idx + 1) % args.log_interval == 0:
                train_loss = loss_value / args.log_interval
                train_acc = matches / args.batch_size / args.log_interval
                final_acc = original_matches / args.batch_size / args.log_interval
                f1 = f1 / args.log_interval
                final_f1 = final_f1 / args.log_interval
                current_lr = get_lr(optimizer)
                print(
                    f"Epoch[{epoch}/{args.epochs}]({idx + 1}/{len(train_loader)}) || "
                    f"training loss {train_loss:4.4} || training accuracy {train_acc:4.2%} || "
                    f"final accuracy {final_acc:4.2%} || training f1 {f1:3.2%} || final f1 {final_f1:3.2%} || "
                    f"lr {current_lr}"
                )
                logger.add_scalar("Train/loss", train_loss, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/accuracy", train_acc, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/final_acc", final_acc, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/f1", f1, epoch * len(train_loader) + idx)
                logger.add_scalar("Train/final_f1", final_f1, epoch * len(train_loader) + idx)

                wandb.log({
                        "Train Acc" : train_acc,
                        "Train Loss" : train_loss
                    })
                
                loss_value = 0
                matches = 0
                original_matches = 0
                f1 = 0
                final_f1 = 0           

        scheduler.step()
        
        if input_figure is None:
                train_np = torch.clone(inputs).detach().cpu().permute(0, 2, 3, 1).numpy()
                train_np = dataset_module.denormalize_image(train_np, dataset.mean, dataset.std)
                input_figure = grid_image(
                        train_np, labels, preds, n=11, shuffle=args.dataset != "MaskSplitByProfileDataset"
                )
        wandb.log({
                "Train image" : input_figure
            })

        # val loop
        with torch.no_grad():
            print("Calculating validation results...")
            model.eval()
            val_loss_items = []
            val_acc_items = []
            val_final_acc_items = []
            figure = None
            score = 0
            final_score = 0
            for val_batch in val_loader:
                inputs, org_labels, labels = val_batch
                inputs = inputs.to(device)
                labels = labels.to(device)
                org_labels = org_labels.to(device)

                outs = model(inputs)
                preds = torch.argmax(outs, dim=-1)

                final_preds = AgeBaseDataset.encode_original_age(preds)

                loss_item = criterion(outs, labels).item()
                acc_item = (labels == preds).sum().item()
                final_acc_item = (org_labels == final_preds).sum().item()
                val_loss_items.append(loss_item)
                val_acc_items.append(acc_item)
                val_final_acc_items.append(final_acc_item)

                if figure is None:
                    inputs_np = torch.clone(inputs).detach().cpu().permute(0, 2, 3, 1).numpy()
                    inputs_np = dataset_module.denormalize_image(inputs_np, dataset.mean, dataset.std)
                    figure = grid_image(
                        inputs_np, labels, preds, n=16, shuffle=args.dataset != "MaskSplitByProfileDataset"
                    )
                score += f1_score(labels.cpu().numpy(), preds.cpu().numpy(), average = 'macro')
                final_score += f1_score(org_labels.cpu().numpy(), final_preds.cpu().numpy(), average = 'macro')
            score = score / len(val_loader)
            final_score = final_score / len(val_loader)
            print(f"Validation F1 Score : {score:3.2%}, final f1 score : {final_score:3.2%}")
            
            val_loss = np.sum(val_loss_items) / len(val_loader)
            val_acc = np.sum(val_acc_items) / len(val_set)
            val_final_acc = np.sum(val_final_acc_items) / len(val_set)
            best_val_loss = min(best_val_loss, val_loss)
            best_val_acc = max(best_val_acc, val_acc)
            if score > best_val_f1:
                print(f"New best model for val f1 score : {score:3.2%}! saving the best model..")
                torch.save(model.module.state_dict(), f"{save_dir}/best.pth")
                best_val_f1 = score
            torch.save(model.module.state_dict(), f"{save_dir}/last.pth")
            print(
                f"[Val] f1: {score:3.2%}, final_f1: {final_score:3.2%}, acc : {val_acc:4.2%}, loss: {val_loss:4.2} || "
                f"best acc : {best_val_acc:4.2%}, best loss: {best_val_loss:4.2}, best f1 score: {best_val_f1:3.2%}"
            )
            logger.add_scalar("Val/loss", val_loss, epoch)
            logger.add_scalar("Val/accuracy", val_acc, epoch)
            logger.add_figure("results", figure, epoch)
            print()
            
            wandb.log({
                        "Valid Accuracy" : val_acc,
                        "Valid Loss" : val_loss,
                        "results" : figure
                })


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    from dotenv import load_dotenv
    import os
    load_dotenv(verbose=True)

    # Data and model checkpoints directories
    parser.add_argument('--seed', type=int, default=42, help='random seed (default: 42)')
    parser.add_argument('--epochs', type=int, default=30, help='number of epochs to train (default: 30)')
    parser.add_argument('--dataset', type=str, default='AgeBaseDataset', help='dataset augmentation type (default: AgeBaseDataset)')
    parser.add_argument('--augmentation', type=str, default='BaseAugmentation', help='data augmentation type (default: BaseAugmentation)')
    parser.add_argument("--resize", nargs="+", type=int, default=[256, 192], help='resize size for image when training')
    parser.add_argument('--batch_size', type=int, default=32, help='input batch size for training (default: 32)')
    parser.add_argument('--valid_batch_size', type=int, default=32, help='input batch size for validing (default: 32)')
    parser.add_argument('--model', type=str, default='EfficientnetB4', help='model type (default: EfficientnetB4)')
    parser.add_argument('--optimizer', type=str, default='Adam', help='optimizer type (default: Adam)')
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate (default: 1e-4)')
    parser.add_argument('--val_ratio', type=float, default=0.2, help='ratio for validaton (default: 0.2)')
    parser.add_argument('--criterion', type=str, default='cross_entropy', help='criterion type (default: cross_entropy)')
    parser.add_argument('--lr_decay_step', type=int, default=20, help='learning rate scheduler deacy step (default: 20)')
    parser.add_argument('--log_interval', type=int, default=20, help='how many batches to wait before logging training status')
    parser.add_argument('--name', default='age', help='model save at {SM_MODEL_DIR}/{name}')

    # Container environment
    parser.add_argument('--data_dir', type=str, default=os.environ.get('SM_CHANNEL_TRAIN', '/opt/ml/input/data/train/images'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR', './model'))

    args = parser.parse_args()
    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir

    train(data_dir, model_dir, args)