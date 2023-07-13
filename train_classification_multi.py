# Basic library
import os
import random
import copy
import numpy as np
import pandas as pd
import tqdm
import argparse
from datetime import timedelta

# Pytorch Related Library
import torch
from torchvision import transforms
from torch.optim import lr_scheduler
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel
import torchmetrics

# Scikit-Learn Library
from sklearn.model_selection import train_test_split

# Monai Library
from monai.config import print_config
from monai.data import DataLoader, DistributedSampler
from monai.metrics import ROCAUCMetric
from monai.transforms import (
    Activations,
    EnsureChannelFirst,
    AsDiscrete,
    Compose,
    LoadImage,
    Resize,
    RandZoom,
    ScaleIntensity)

# defining the input arguments for the script.
parser = argparse.ArgumentParser(description='Parameters ')
parser.add_argument('--random_state', default=0, type=int, help='random seed')
parser.add_argument('--local_rank', type=int)
parser.add_argument('--image_dir', default='/red/ruogu.fang/UKB/data/Eye/21015_fundus_left_1/', type=str,
                    help='random seed')
parser.add_argument('--csv_dir', default='/red/ruogu.fang/leem.s/NSF-SCH/data/age.csv', type=str, help='random seed')

parser.add_argument('--eye_code', default='_21015_0_0.png', type=str, help='random seed')
parser.add_argument('--label_code', default='21003-0.0', type=str, help='random seed')

parser.add_argument('--working_dir', default='ViT_age', type=str, help='random seed')
parser.add_argument('--model_name', default='ViT_age', type=str, help='random seed')
parser.add_argument('--base_model', default='google/vit-base-patch16-224-in21k', type=str,
                    help='the string of model from hugging-face library')

parser.add_argument('--lr', default=1e-4, type=float, help='learning rate of the training')
parser.add_argument('--epoch', default=100, type=int, help='maximum number of epoch')

# set argument as input variables
args = parser.parse_args()

# initialize a process group, every GPU runs in a process
dist.init_process_group(backend="nccl", init_method="env://", timeout=timedelta(minutes=10))

# set random seed
random_state = args.random_state

np.random.seed(random_state)
random.seed(random_state)
torch.manual_seed(random_state)
os.environ["PYTHONHASHSEED"] = str(random_state)

# setting the directories for training
image_dir = args.image_dir
csv_dir = args.csv_dir
eye_code = args.eye_code
label_code = args.label_code

# extract the patient eid with both images and label csv files
file_list = os.listdir(image_dir)
eid_list = [s.replace(eye_code, '') for s in file_list]

# read the labels from the csv file & process it.
csv_df = pd.read_csv(csv_dir)
convert_dict = {'eid': str}
csv_df = csv_df.astype(convert_dict)

label_df = csv_df[csv_df['eid'].isin(eid_list)]
label_df = label_df.dropna(subset=[label_code])

label_df['image'] = label_df['eid'] + eye_code
label_df['path'] = image_dir + label_df['image']

# Defining the input & output for the data
X = label_df['path'].values.tolist()
y = label_df[label_code].values.tolist()

# Defining the train, val, test split
X_train, X_remain, y_train, y_remain = train_test_split(X, y, train_size=0.8, random_state=random_state)
X_val, X_test, y_val, y_test = train_test_split(X_remain, y_remain, train_size=0.5, random_state=random_state)


# Classification dataset definition
class ClassificationDataset(torch.utils.data.Dataset):
    def __init__(self, image_files, labels, transforms):
        self.image_files = image_files
        self.labels = labels
        self.transforms = transforms

    def __len__(self):
        return len(self.image_files)

    def __getitem__(self, index):
        return self.transforms(self.image_files[index]), self.labels[index]


class EarlyStopping:
    def __init__(self, tolerance=5, min_delta=0):

        self.tolerance = tolerance
        self.min_delta = min_delta
        self.counter = 0
        self.early_stop = False

    def __call__(self, train_loss, validation_loss):
        if (validation_loss - train_loss) > self.min_delta:
            self.counter += 1
            if self.counter >= self.tolerance:
                self.early_stop = True


image_size = 224

train_transforms = Compose(
    [
        LoadImage(image_only=True),
        EnsureChannelFirst(),
        Resize((224, 224)),
        ScaleIntensity(),
        # RandRotate(range_x=np.pi / 12, prob=0.5, keep_size=True),
    ]
)

val_transforms = Compose(
    [
        LoadImage(image_only=True),
        EnsureChannelFirst(),
        Resize((224, 224)),
        ScaleIntensity()
    ]
)

# Dataset & Dataloader
train_ds = ClassificationDataset(X_train, y_train, train_transforms)
train_sampler = DistributedSampler(dataset=train_ds, even_divisible=True, shuffle=True)
train_loader = DataLoader(train_ds, batch_size=32, shuffle=False, pin_memory=True, sampler=train_sampler)

val_ds = ClassificationDataset(X_val, y_val, val_transforms)
val_sampler = DistributedSampler(dataset=val_ds, even_divisible=True, shuffle=True)
val_loader = DataLoader(val_ds, batch_size=32, shuffle=False, pin_memory=True, sampler=val_sampler)

test_ds = ClassificationDataset(X_test, y_test, val_transforms)
test_loader = DataLoader(test_ds, batch_size=32, shuffle=False)

# Defining the model
from transformers import ViTFeatureExtractor, ViTForImageClassification
model_name_or_path = args.base_model
feature_extractor = ViTFeatureExtractor.from_pretrained(model_name_or_path)

device = torch.device(f"cuda:{args.local_rank}")
torch.cuda.set_device(device)

model = ViTForImageClassification.from_pretrained(
    model_name_or_path,
    num_labels=len(label_df[label_code].unique()))

metric = torchmetrics.Accuracy(task="multiclass", num_classes=len(label_df[label_code].unique())).to(device)
model.metric = metric
model.to(device)

# Defining the variables for training
optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
scheduler = lr_scheduler.StepLR(optimizer, step_size=30, gamma=0.1)
loss_function = torch.nn.CrossEntropyLoss()
max_epochs = args.epoch
model = DistributedDataParallel(model, device_ids=[device])

# Training the model
def train_model(model=model,
                train_loader=train_loader,
                val_loader=val_loader,
                max_epochs=max_epochs,
                optimizer=optimizer,
                loss_function=loss_function,
                model_name=args.model_name,
                working_dir='./red/ruogu.fang/leem.s/NSF-SCH/code/savedmodel'):

    # defining the path for saving the model.
    if not os.path.exists(working_dir):
        os.makedirs(working_dir)

    # initialization of the variable for analysis
    best_loss = 0
    best_metric = -1
    best_metric_epoch = 0
    epoch_loss_values = []
    val_interval = 1

    for epoch in range(max_epochs):
        print("-" * 10, flush=True)
        print(f"[{dist.get_rank()}] " + "-" * 10 + f" epoch {epoch + 1}/{max_epochs}")

        # Turn on the training mode
        model.train()
        epoch_loss = 0
        step = 0
        train_sampler.set_epoch(epoch)

        for inputs, labels in train_loader:
            step += 1
            labels = labels.type(torch.Tensor)
            
            # transfer the data to gpu
            inputs, labels = inputs.to(device), labels.to(device)

            optimizer.zero_grad()
            outputs = model(inputs)[0]

            # compute the loss and back-propagate the gradient.
            loss = loss_function(outputs, labels.long())
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()

        # learning rate decay update
        scheduler.step()
        epoch_loss /= step
        epoch_loss_values.append(epoch_loss)
        print(f"[{dist.get_rank()}] " + f"epoch {epoch + 1}, average loss: {epoch_loss:.4f}")

        # validation mode
        if (epoch + 1) % val_interval == 0:
            model.eval()
            with torch.no_grad():
                for val_data in val_loader:
                    val_images, val_labels = (
                        val_data[0].to(device, non_blocking=True),
                        val_data[1].to(device, non_blocking=True),
                    )
                    outputs = model(val_images)[0]
                    val_loss = loss_function(outputs, val_labels.long())
                    result = metric(outputs, val_labels)

                    if dist.get_rank() == 0:  # print only for rank 0
                        print(f"Batch Accuracy: {result}")

                result = metric.compute()
                result = result.cpu().detach().numpy()
                print(f"Accuracy on all data: {result}, accelerator rank: {dist.get_rank()}")

                if epoch == 0:
                    best_loss = val_loss
                    best_model = model
                    best_metric_epoch = epoch
                    best_metric = result

                elif epoch != 0:
                    if val_loss < best_loss:
                        best_loss = val_loss
                        best_model = model
                        best_metric = result
                        best_metric_epoch = epoch
                        best_model_wts = copy.deepcopy(best_model.state_dict())
                        if dist.get_rank() == 0:
                            print("the best model has been updated")
                        # torch.save(best_model.state_dict(), os.path.join(working_dir, model_name+str(best_metric_epoch+1)+'.pth'))
                        # print("saved new best metric model")

                if dist.get_rank() == 0:
                    print(
                        f"current epoch: {epoch + 1}, current MAE: {result}",
                        f" best Accuracy: {best_metric}",
                        f" at epoch: {best_metric_epoch + 1}"
                    )

        metric.reset()

    print(f"[{dist.get_rank()}] " + f"train completed, epoch losses: {epoch_loss_values}")
    torch.save(best_model.state_dict(), os.path.join(working_dir, model_name + str(best_metric_epoch + 1) + '.pth'))
    best_model.load_state_dict(best_model_wts)
    return best_model


best_model = train_model(model=model, train_loader=train_loader, val_loader=val_loader, max_epochs=max_epochs,
                         optimizer=optimizer,
                         loss_function=loss_function, model_name=args.model_name, working_dir=args.working_dir)

dist.destroy_process_group()
