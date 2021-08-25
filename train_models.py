import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from dataset.ml_dataloader import getDataloader
import torchvision
from models.model import get_model
import argparse
from pytz import timezone
import datetime as dt

def train_models(args):
    now = (dt.datetime.now().astimezone(timezone("Asia/Seoul")).strftime("%m%d_%H%M"))
    train_dataloader, val_dataloader = getDataloader(args)
    print('train_data: {}, val_data: {}'.format(len(train_dataloader), len(val_dataloader)))
    
    device = 'cuda'
    criterion = nn.CrossEntropyLoss()
    
    # torchvision models
    # if args.model_name == 'resnet18':
    #     model = torchvision.models.resnet18(pretrained=False)
    #     model.fc = torch.nn.Linear(in_features=512, out_features=18, bias=True)
    # elif args.model_name == 'vgg16':
    #     model = torchvision.models.vgg16(pretrained=False)
    #     model.classifier[6] = nn.Linear(in_features=4096, out_features=18, bias=True)
    
    # Custom models
    if args.pretrained:
        age_model = torch.load(args.pretrained_age)
        gender_model = torch.load(args.pretrained_gender)
        mask_model = torch.load(args.pretrained_mask)
    else:
        age_model = get_model(args)
        gender_model = get_model(args)
        mask_model = get_model(args)
        
        if args.model_name.find('resnet') != -1:
            age_model.fc = nn.Linear(in_features=256, out_features=3, bias=True)
            mask_model.fc = nn.Linear(in_features=256, out_features=3, bias=True)
            gender_model.fc = nn.Linear(in_features=256, out_features=2, bias=True)
    
    age_optimizer = torch.optim.Adam(age_model.parameters(), lr=0.0001)
    gender_optimizer = torch.optim.Adam(gender_model.parameters(), lr=0.0001)
    mask_optimizer = torch.optim.Adam(mask_model.parameters(), lr=0.0001)
    
    print(age_model)
    age_model = age_model.to(device)
    gender_model = gender_model.to(device)
    mask_model = mask_model.to(device)

    max_val_acc = 0
    age_model.train()
    gender_model.train()
    mask_model.train()
    for epoch in range(args.epochs):
        total_acc = 0
        total_age_acc = 0
        total_mask_acc = 0
        total_gender_acc = 0
        
        total_age_loss = 0
        total_mask_loss = 0
        total_gender_loss = 0
        
        print("----------------Train!----------------")
        for batch_iter, batch in enumerate(train_dataloader):
            img, label, gender, age, mask = batch
            img = img.to(device)
            label = label.to(device)
            gender = gender.to(device)
            age = age.to(device)
            mask = mask.to(device)

            # Forward path
            age_pred = age_model(img)
            gender_pred = gender_model(img)
            mask_pred = mask_model(img)
            
            age_loss = criterion(age_pred, age)
            gender_loss = criterion(gender_pred, gender)
            mask_loss = criterion(mask_pred, mask)
            
            age_acc = torch.sum(torch.max(age_pred, 1)[1] == age.data)/len(age.data)
            gender_acc = torch.sum(torch.max(gender_pred, 1)[1] == gender.data)/len(gender.data)
            mask_acc = torch.sum(torch.max(mask_pred, 1)[1] == mask.data)/len(mask.data)
            
            # make real class
            pred = torch.max(age_pred, 1)[1] + 3*torch.max(gender_pred, 1)[1] + 6*torch.max(mask_pred, 1)[1]
            acc = torch.sum(pred == label)/len(label)
            
            print('[epoch: {}, iter: {}] Age train acc: {:.5f}, Gender train acc: {:.5f}, Mask train acc: {:.5f}, train acc: {:.5f}'.format(epoch, batch_iter, age_acc, gender_acc, mask_acc, acc))
            
            age_optimizer.zero_grad()
            gender_optimizer.zero_grad()
            mask_optimizer.zero_grad()
            
            # backpropagate
            age_loss.backward()
            gender_loss.backward()
            mask_loss.backward()
            
            # optimizer update
            age_optimizer.step()
            gender_optimizer.step()
            mask_optimizer.step()
            
            total_age_loss += age_loss
            total_mask_loss += mask_loss
            total_gender_loss += gender_loss
            
            total_age_acc += age_acc
            total_mask_acc += mask_acc
            total_gender_acc += gender_acc
            total_acc += acc
        
        avg_age_loss = total_age_loss/len(train_dataloader)
        avg_mask_loss = total_mask_loss/len(train_dataloader)
        avg_gender_loss = total_gender_loss/len(train_dataloader)
        
        avg_acc = total_acc/len(train_dataloader)
        avg_age_acc = total_age_acc/len(train_dataloader)
        avg_mask_acc = total_mask_acc/len(train_dataloader)
        avg_gender_acc = total_gender_acc/len(train_dataloader)
        
        print('[epoch: {}] avg train acc: {:.5f}, avg Age acc: {:.5f}, avg Gender acc: {:.5f}, avg Mask acc: {:.5f}'.format(epoch, avg_acc, avg_age_acc, avg_gender_acc, avg_mask_acc))
        
        print("----------------validation!----------------")
        with torch.no_grad():
            age_model.eval()
            mask_model.eval()
            gender_model.eval()
            
            total_val_acc = 0
            total_age_acc = 0
            total_mask_acc = 0
            total_gender_acc = 0
            for val_iter, val_batch in enumerate(val_dataloader):
                img, label, gender, age, mask = val_batch
                img = img.to(device)
                label = label.to(device)
                gender = gender.to(device)
                age = age.to(device)
                mask = mask.to(device)
            
                age_pred = age_model(img)
                mask_pred = mask_model(img)
                gender_pred = gender_model(img)
                
                acc_out = torch.sum(torch.max(y_pred, 1)[1] == val_batch_out.data)/len(val_batch_out.data)
                print('[epoch: {}, iter: {}] validation acc: {}, validation loss: {}'.format(epoch, val_iter, acc_out, loss_out))
                total_val_loss += loss_out
                total_val_acc += acc_out
            avg_val_loss = total_val_loss/len(val_dataloader)
            avg_val_acc = total_val_acc/len(val_dataloader)
        
        # Print
        print ("epoch:[%d] val_acc: [%.3f], val_loss:[%.3f]."%(epoch, avg_val_acc, avg_val_loss))
        
        if avg_val_acc > max_val_acc:
            torch.save(model, '/opt/ml/checkpoint/{}_{}_{}_{:.5f}.pt'.format(now, args.model_name, epoch, avg_val_acc))
            max_val_acc = avg_val_acc
            print('----------------model saved!----------------')
    
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pretrained', default=None, type=str)
    parser.add_argument('--train_csv', default='/opt/ml/input/data/train/final_train.csv', type=str)
    parser.add_argument('--model_name', default='resnet', type=str)
    parser.add_argument('--epochs', type=int, default=50, help='number of training epochs')
    parser.add_argument('--train_ratio', type=float, default=0.9)
    parser.add_argument('--batch_size', type=int, default=32)

    args = parser.parse_args()
    
    train_models(args)