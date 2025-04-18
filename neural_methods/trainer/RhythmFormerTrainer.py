"""Trainer for RhythmFormer."""
import os
import numpy as np
import torch
import torch.optim as optim
import random
from tqdm import tqdm
from evaluation.post_process import calculate_hr, read_fold
from evaluation.metrics import calculate_metrics
from neural_methods.model.RhythmFormer import RhythmFormer
from neural_methods.trainer.BaseTrainer import BaseTrainer
from neural_methods.loss.TorchLossComputer import RhythmFormer_Loss

class RhythmFormerTrainer(BaseTrainer):

    def __init__(self, config, data_loader):
        super().__init__()
        self.device = torch.device(config.DEVICE)
        self.max_epoch_num = config.TRAIN.EPOCHS
        self.model_dir = config.MODEL.MODEL_DIR
        self.model_file_name = config.TRAIN.MODEL_FILE_NAME
        self.batch_size = config.TRAIN.BATCH_SIZE
        self.num_of_gpu = config.NUM_OF_GPU_TRAIN
        self.chunk_len = config.TRAIN.DATA.PREPROCESS.CHUNK_LENGTH
        self.config = config
        self.min_valid_loss = None
        self.best_epoch = 0
        self.diff_flag = 0
        self.data_dict = {}
        self.dataset = config.TRAIN.DATA.DATASET
        self.fold = {}
        if config.TRAIN.DATA.DATASET == "VIPL-HR":
            self.fold = read_fold()
        if config.TRAIN.DATA.PREPROCESS.LABEL_TYPE == "DiffNormalized":
            self.diff_flag = 1
        if config.TOOLBOX_MODE == "train_and_test":
            if self.chunk_len!=160 or config.TRAIN.DATA.FS!=30:
                topk = 4*(self.chunk_len//config.TRAIN.DATA.FS*2)
                self.model = RhythmFormer(topks=(topk,topk,topk)).to(self.device)
            else:
                self.model = RhythmFormer().to(self.device)
            self.model = torch.nn.DataParallel(self.model, device_ids=list(range(config.NUM_OF_GPU_TRAIN)))
            self.num_train_batches = len(data_loader["train"])
            self.criterion = RhythmFormer_Loss()
            self.optimizer = optim.AdamW(
                self.model.parameters(), lr=config.TRAIN.LR, weight_decay=0)
            # See more details on the OneCycleLR scheduler here: https://pytorch.org/docs/stable/generated/torch.optim.lr_scheduler.OneCycleLR.html
            self.scheduler = torch.optim.lr_scheduler.OneCycleLR(
                self.optimizer, max_lr=config.TRAIN.LR, epochs=config.TRAIN.EPOCHS, steps_per_epoch=self.num_train_batches)
        elif config.TOOLBOX_MODE == "only_test":
            if self.chunk_len!=160 or config.TRAIN.DATA.FS!=30:
                topk = 4*(self.chunk_len//config.TRAIN.DATA.FS*2)
                self.model = RhythmFormer(topks=(topk,topk//2,topk//4)).to(self.device)
            else:
                self.model = RhythmFormer().to(self.device)
            self.model = torch.nn.DataParallel(self.model, device_ids=list(range(config.NUM_OF_GPU_TRAIN)))
        else:
            raise ValueError("EfficientPhys trainer initialized in incorrect toolbox mode!")
    

    def train(self, data_loader):
        """Training routine for model"""
        if data_loader["train"] is None:
            raise ValueError("No data for train")
        
        for epoch in range(self.max_epoch_num):
            print('')
            print(f"====Training Epoch: {epoch}====")
            self.model.train()

            # Model Training
            tbar = tqdm(data_loader["train"], ncols=80)
            for idx, batch in enumerate(tbar):
                tbar.set_description("Train epoch %s" % epoch)
                data, labels = batch[0].float(), batch[1].float()
                data.requires_grad = True
                data.retain_grad = True
                N, D, C, H, W = data.shape
                if self.config.TRAIN.AUG :
                    if self.dataset == "VIPL-HR":
                        data,labels = self.vipl_data_augmentation(data,labels,batch[2],batch[3])
                    else:
                        data,labels = self.data_augmentation(data,labels,batch[2],batch[3])

                data = data.to(self.device)
                labels = labels.to(self.device)

                self.optimizer.zero_grad()
                pred_ppg = self.model(data)
                pred_ppg = (pred_ppg-torch.mean(pred_ppg, axis=-1).view(-1, 1))/torch.std(pred_ppg, axis=-1).view(-1, 1)    # normalize

                loss = 0.0
                for ib in range(N):
                    loss = loss + self.criterion(pred_ppg[ib], labels[ib], epoch , self.config.TRAIN.DATA.FS , self.diff_flag)
                loss = loss / N
                loss.backward()
                self.optimizer.step()
                self.scheduler.step()
                tbar.set_postfix(loss=loss.item())
            self.save_model(epoch)
            if not self.config.TEST.USE_LAST_EPOCH: 
                valid_loss = self.valid(data_loader)
                print('validation loss: ', valid_loss)
                if self.min_valid_loss is None:
                    self.min_valid_loss = valid_loss
                    self.best_epoch = epoch
                    print("Update best model! Best epoch: {}".format(self.best_epoch))
                elif (valid_loss < self.min_valid_loss):
                    self.min_valid_loss = valid_loss
                    self.best_epoch = epoch
                    print("Update best model! Best epoch: {}".format(self.best_epoch))
        if not self.config.TEST.USE_LAST_EPOCH: 
            print("best trained epoch: {}, min_val_loss: {}".format(self.best_epoch, self.min_valid_loss))  


    def valid(self, data_loader):
        """ Model evaluation on the validation dataset."""
        if data_loader["valid"] is None:
            raise ValueError("No data for valid")
        print('')
        print("===Validating===")
        valid_loss = []
        self.model.eval()
        valid_step = 0
        with torch.no_grad():
            vbar = tqdm(data_loader["valid"], ncols=80)
            for valid_idx, valid_batch in enumerate(vbar):
                vbar.set_description("Validation")
                data_valid, labels_valid = valid_batch[0].to(self.device), valid_batch[1].to(self.device)
                N, D, C, H, W = data_valid.shape
                pred_ppg_valid = self.model(data_valid)
                pred_ppg_valid = (pred_ppg_valid-torch.mean(pred_ppg_valid, axis=-1).view(-1, 1))/torch.std(pred_ppg_valid, axis=-1).view(-1, 1)    # normalize
                for ib in range(N):
                    loss = self.criterion(pred_ppg_valid[ib], labels_valid[ib], self.config.TRAIN.EPOCHS , self.config.VALID.DATA.FS , self.diff_flag)
                    valid_loss.append(loss.item())
                    valid_step += 1
                    vbar.set_postfix(loss=loss.item())
        return np.mean(np.asarray(valid_loss))


    def test(self, data_loader):
        """ Model evaluation on the testing dataset."""
        if data_loader["test"] is None:
            raise ValueError("No data for test")

        print('')
        print("===Testing===")
        if self.config.TOOLBOX_MODE == "only_test":
            if not os.path.exists(self.config.INFERENCE.MODEL_PATH):
                raise ValueError("Inference model path error! Please check INFERENCE.MODEL_PATH in your yaml.")
            self.model.load_state_dict(torch.load(self.config.INFERENCE.MODEL_PATH))
            print("Testing uses pretrained model!")
        else:
            if self.config.TEST.USE_LAST_EPOCH:
                last_epoch_model_path = os.path.join(
                self.model_dir, self.model_file_name + '_Epoch' + str(self.max_epoch_num - 1) + '.pth')
                print("Testing uses last epoch as non-pretrained model!")
                print(last_epoch_model_path)
                self.model.load_state_dict(torch.load(last_epoch_model_path))
            else:
                best_model_path = os.path.join(
                    self.model_dir, self.model_file_name + '_Epoch' + str(self.best_epoch) + '.pth')
                print("Testing uses best epoch selected using model selection as non-pretrained model!")
                print(best_model_path)
                self.model.load_state_dict(torch.load(best_model_path))

        self.model = self.model.to(self.config.DEVICE)
        self.model.eval()
        with torch.no_grad():
            predictions = dict()
            labels = dict()
            for _, test_batch in enumerate(data_loader['test']):
                batch_size = test_batch[0].shape[0]
                chunk_len = self.chunk_len
                data_test, labels_test = test_batch[0].to(self.config.DEVICE), test_batch[1].to(self.config.DEVICE)
                pred_ppg_test = self.model(data_test)
                pred_ppg_test = (pred_ppg_test-torch.mean(pred_ppg_test, axis=-1).view(-1, 1))/torch.std(pred_ppg_test, axis=-1).view(-1, 1)    # normalize
                labels_test = labels_test.view(-1, 1)
                pred_ppg_test = pred_ppg_test.view( -1 , 1)
                for ib in range(batch_size):
                    subj_index = test_batch[2][ib]
                    sort_index = int(test_batch[3][ib])
                    if subj_index not in predictions.keys():
                        predictions[subj_index] = dict()
                        labels[subj_index] = dict()
                    predictions[subj_index][sort_index] = pred_ppg_test[ib * chunk_len:(ib + 1) * chunk_len]
                    labels[subj_index][sort_index] = labels_test[ib * chunk_len:(ib + 1) * chunk_len]
            print(' ')
            calculate_metrics(predictions, labels, self.config)


    def save_model(self, index):
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)
        model_path = os.path.join(
            self.model_dir, self.model_file_name + '_Epoch' + str(index) + '.pth')
        torch.save(self.model.state_dict(), model_path)
        print('Saved Model Path: ', model_path)


    def data_augmentation(self,data,labels,index1,index2):
        N, D, C, H, W = data.shape
        data_aug = np.zeros((N, D, C, H, W))
        labels_aug = np.zeros((N, D))
        rand1_vals = np.random.random(N)
        rand2_vals = np.random.random(N)
        for idx in range(N):
            index = index1[idx] + index2[idx]
            rand1 = rand1_vals[idx]
            rand2 = rand2_vals[idx]
            if rand1 < 0.5 :
                if index in self.data_dict:
                    gt_hr_fft = self.data_dict[index]
                else:
                    gt_hr_fft, _  = calculate_hr(labels[idx], labels[idx] , diff_flag = self.diff_flag , fs=self.config.VALID.DATA.FS)
                    self.data_dict[index] = gt_hr_fft
                if gt_hr_fft > 90:
                    rand3 = random.randint(0, D//2-1)
                    even_indices = torch.arange(0, D, 2)
                    odd_indices = even_indices + 1
                    data_aug[:, even_indices, :, :, :] = data[:, rand3 + even_indices// 2, :, :, :]
                    labels_aug[:, even_indices] = labels[:, rand3 + even_indices // 2]
                    data_aug[:, odd_indices, :, :, :] = (data[:, rand3 + odd_indices // 2, :, :, :] + data[:, rand3 + (odd_indices // 2) + 1, :, :, :]) / 2
                    labels_aug[:, odd_indices] = (labels[:, rand3 + odd_indices // 2] + labels[:, rand3 + (odd_indices // 2) + 1]) / 2
                elif gt_hr_fft < 75 :
                    data_aug[:, :D//2, :, :, :] = data[:, ::2, :, :, :]
                    labels_aug[:, :D//2] = labels[:, ::2]
                    data_aug[:, D//2:, :, :, :] = data_aug[:, :D//2, :, :, :]
                    labels_aug[:, D//2:] = labels_aug[:, :D//2]
                else :
                    data_aug[idx] = data[idx]
                    labels_aug[idx] = labels[idx]                                      
            else :
                data_aug[idx] = data[idx]
                labels_aug[idx] = labels[idx]
        data_aug = torch.tensor(data_aug).float()
        labels_aug = torch.tensor(labels_aug).float()
        if rand2 < 0.5:
            data_aug = torch.flip(data_aug, dims=[4])
        return data_aug, labels_aug
    

    # Nearly half of the frames in VIPL-HR have a frame rate lower than 20. For these already upsampled samples, no further upsampling is performed.
    def vipl_data_augmentation(self,data,labels,index1,index2):
        N, D, C, H, W = data.shape
        data_aug = np.zeros((N, D, C, H, W))
        labels_aug = np.zeros((N, D))
        rand1_vals = np.random.random(N)
        rand2_vals = np.random.random(N)
        for idx in range(N):
            index = index1[idx] + index2[idx]
            rand1 = rand1_vals[idx]
            rand2 = rand2_vals[idx]
            if rand1 < 0.5 :
                if index in self.data_dict:
                    gt_hr_fft = self.data_dict[index]
                else:
                    gt_hr_fft, _  = calculate_hr(labels[idx], labels[idx] , diff_flag = self.diff_flag , fs=self.config.VALID.DATA.FS)
                    self.data_dict[index] = gt_hr_fft
                if gt_hr_fft > 90 and index1[idx] in self.fold:
                    rand3 = random.randint(0, D//2-1)
                    even_indices = torch.arange(0, D, 2)
                    odd_indices = even_indices + 1
                    data_aug[:, even_indices, :, :, :] = data[:, rand3 + even_indices// 2, :, :, :]
                    labels_aug[:, even_indices] = labels[:, rand3 + even_indices // 2]
                    data_aug[:, odd_indices, :, :, :] = (data[:, rand3 + odd_indices // 2, :, :, :] + data[:, rand3 + (odd_indices // 2) + 1, :, :, :]) / 2
                    labels_aug[:, odd_indices] = (labels[:, rand3 + odd_indices // 2] + labels[:, rand3 + (odd_indices // 2) + 1]) / 2
                elif gt_hr_fft < 70 :
                    data_aug[:, :D//2, :, :, :] = data[:, ::2, :, :, :]
                    labels_aug[:, :D//2] = labels[:, ::2]
                    data_aug[:, D//2:, :, :, :] = data_aug[:, :D//2, :, :, :]
                    labels_aug[:, D//2:] = labels_aug[:, :D//2]
                else :
                    data_aug[idx] = data[idx]
                    labels_aug[idx] = labels[idx]                                      
            else :
                data_aug[idx] = data[idx]
                labels_aug[idx] = labels[idx]
        data_aug = torch.tensor(data_aug).float()
        labels_aug = torch.tensor(labels_aug).float()
        if rand2 < 0.5:
            data_aug = torch.flip(data_aug, dims=[4])
        return data_aug, labels_aug