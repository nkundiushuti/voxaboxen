import os
import math
import numpy as np
import pandas as pd
import librosa
import warnings

from numpy.random import default_rng
from intervaltree import IntervalTree
from torch.utils.data import Dataset, DataLoader

import torch
import torchaudio
from torch.nn import functional as F

def normalize_sig_np(sig, eps=1e-8):
    sig = sig / (np.max(np.abs(sig))+eps)
    return sig
  
def crop_and_pad(wav, sr, dur_sec):
  # crops and pads waveform to be the expected number of samples; used after resampling to ensure proper size
  target_dur_samples = int(sr * dur_sec)
  wav = wav[:target_dur_samples]
  pad = target_dur_samples - wav.size(0)
  if pad > 0:
    wav = F.pad(wav, (0,pad), mode='reflect')
    
  return wav

class DetectionDataset(Dataset):
    def __init__(self, info_df, train, args, random_seed_shift = 0):
        self.info_df = info_df
        self.label_set = args.label_set
        if hasattr(args, 'unknown_label'):
          self.unknown_label = args.unknown_label
        else:
          self.unknown_label = None
        self.label_mapping = args.label_mapping
        self.n_classes = len(self.label_set)
        self.sr = args.sr
        self.clip_duration = args.clip_duration
        self.clip_hop = args.clip_hop
        assert (self.clip_hop*args.sr).is_integer()
        self.seed = args.seed + random_seed_shift
        self.amp_aug = args.amp_aug
        if self.amp_aug:
          self.amp_aug_low_r = args.amp_aug_low_r
          self.amp_aug_high_r = args.amp_aug_high_r
          assert (self.amp_aug_low_r >= 0) #and (self.amp_aug_high_r <= 1) and 
          assert (self.amp_aug_low_r <= self.amp_aug_high_r)

        self.scale_factor = args.scale_factor
        self.prediction_scale_factor = args.prediction_scale_factor
        self.scale_factor_raw_to_prediction = self.scale_factor*self.prediction_scale_factor
        self.rng = default_rng(seed=self.seed)
        self.train=train
        
        if self.train:
          self.omit_empty_clip_prob = args.omit_empty_clip_prob
          self.clip_start_offset = self.rng.integers(0, np.floor(self.clip_hop*self.sr)) / self.sr
        else:
          self.omit_empty_clip_prob = 0
          self.clip_start_offset = 0
        
        # make metadata
        self.make_metadata()

    def augment_amplitude(self, signal):
        if not self.amp_aug:
            return signal
        else:
            r = self.rng.uniform(self.amp_aug_low_r, self.amp_aug_high_r)
            aug_signal = r*signal
            return aug_signal

    def process_selection_table(self, selection_table_fp):
        selection_table = pd.read_csv(selection_table_fp, sep = '\t')
        tree = IntervalTree()

        for ii, row in selection_table.iterrows():
            start = row['Begin Time (s)']
            end = row['End Time (s)']
            label = row['Annotation']
            
            if end<=start:
              continue
            
            if label in self.label_mapping:
              label = self.label_mapping[label]
            else:
              continue
            
            if label == self.unknown_label:
              label_idx = -1
            else:
              label_idx = self.label_set.index(label)
            tree.addi(start, end, label_idx)

        return tree

    def make_metadata(self):
        selection_table_dict = dict()
        metadata = []

        for ii, row in self.info_df.iterrows():
            fn = row['fn']
            audio_fp = row['audio_fp']
            duration = librosa.get_duration(filename=audio_fp)
            selection_table_fp = row['selection_table_fp']

            selection_table = self.process_selection_table(selection_table_fp)
            selection_table_dict[fn] = selection_table

            num_clips = max(0, int(np.floor((duration - self.clip_duration - self.clip_start_offset) // self.clip_hop)))

            for tt in range(num_clips):
                start = tt*self.clip_hop + self.clip_start_offset
                end = start + self.clip_duration

                ivs = selection_table[start:end]
                # if no annotated intervals, skip with specified probability
                if not ivs:
                  if self.omit_empty_clip_prob > self.rng.uniform():
                      continue

                metadata.append([fn, audio_fp, start, end])

        self.selection_table_dict = selection_table_dict
        self.metadata = metadata

    def get_pos_intervals(self, fn, start, end):
        tree = self.selection_table_dict[fn]

        intervals = tree[start:end]
        intervals = [(max(iv.begin, start)-start, min(iv.end, end)-start, iv.data) for iv in intervals]

        return intervals
      
    def get_class_proportions(self):
        counts = np.zeros((self.n_classes,))
        
        for k in self.selection_table_dict:
          st = self.selection_table_dict[k]
          for interval in st:
            annot = interval.data
            if annot == -1:
              continue
            else:
              counts[annot] += 1
        
        total_count = np.sum(counts)
        proportions = counts / total_count
          
        return proportions
          

    def get_annotation(self, pos_intervals, audio):
        
        raw_seq_len = audio.shape[0]
        seq_len = int(math.ceil(raw_seq_len / self.scale_factor_raw_to_prediction))
        regression_anno = np.zeros((seq_len,))
        class_anno = np.zeros((seq_len, self.n_classes)) 

        anno_sr = int(self.sr // self.scale_factor_raw_to_prediction)
        
        anchor_annos = [np.zeros(seq_len,)]
                
        mask = [np.ones(seq_len)]

        for iv in pos_intervals:
            start, end, class_idx = iv
            dur = end-start
            
            start_idx = int(math.floor(start*anno_sr))
            start_idx = max(min(start_idx, seq_len-1), 0)
            dur_samples = np.ceil(dur * anno_sr)
            
            anchor_anno = get_anchor_anno(start_idx, dur_samples, seq_len)
            anchor_annos.append(anchor_anno)
            regression_anno[start_idx] = dur

            if class_idx != -1:
              class_anno[start_idx, class_idx] = 1.
            else:
              class_anno[start_idx, :] = 1./self.n_classes # if unknown, enforce uncertainty
        
        anchor_annos = np.stack(anchor_annos)
        anchor_annos = np.amax(anchor_annos, axis = 0)

        return anchor_annos, regression_anno, class_anno # shapes [time_steps, ], [time_steps, ], [time_steps, n_classes]

    def __getitem__(self, index):
        fn, audio_fp, start, end = self.metadata[index]
        audio, file_sr = librosa.load(audio_fp, sr=None, offset=start, duration=self.clip_duration, mono=True)
        audio = audio-np.mean(audio)
        if self.amp_aug and self.train:
            audio = self.augment_amplitude(audio)
        audio = torch.from_numpy(audio)
        if file_sr != self.sr:
          audio = torchaudio.functional.resample(audio, file_sr, self.sr) 
          audio = crop_and_pad(audio, self.sr, self.clip_duration)
        
        pos_intervals = self.get_pos_intervals(fn, start, end)
        anchor_anno, regression_anno, class_anno = self.get_annotation(pos_intervals, audio)

        return audio, torch.from_numpy(anchor_anno), torch.from_numpy(regression_anno), torch.from_numpy(class_anno)

    def __len__(self):
        return len(self.metadata)
      
      
def get_train_dataloader(args, random_seed_shift = 0):
  train_info_fp = args.train_info_fp
  train_info_df = pd.read_csv(train_info_fp)
  
  train_dataset = DetectionDataset(train_info_df, True, args, random_seed_shift = random_seed_shift)
  
  if args.mixup:
    effective_batch_size = args.batch_size*2 # double batch size because half will be discarded before being passed to model
  else:
    effective_batch_size = args.batch_size
  
  
  train_dataloader = DataLoader(train_dataset,
                                batch_size=args.batch_size, 
                                shuffle=True,
                                num_workers=args.num_workers,
                                pin_memory=True, 
                                drop_last = True)
  
  return train_dataloader

  
class SingleClipDataset(Dataset):
    def __init__(self, audio_fp, clip_hop, args, annot_fp = None):
        # waveform (samples,)
        super().__init__()
        self.duration = librosa.get_duration(filename=audio_fp)
        self.num_clips = max(0, int(np.floor((self.duration - args.clip_duration) // clip_hop)))
        self.audio_fp = audio_fp
        # self.waveform, self.file_sr = librosa.load(audio_fp, sr=None, mono=True)
        # self.clip_hop_samples_file_sr = int(clip_hop * self.file_sr)
        self.clip_hop = clip_hop
        # self.clip_duration_samples_file_sr = int(args.clip_duration * self.file_sr)
        self.clip_duration = args.clip_duration
        self.annot_fp = annot_fp # attribute that is accessed elsewhere
        self.sr = args.sr
        
    def __len__(self):
        return self.num_clips

    def __getitem__(self, idx):
        """ Map int idx to dict of torch tensors """
        start = idx * self.clip_hop
        
        audio, file_sr = librosa.load(self.audio_fp, sr=None, offset=start, duration=self.clip_duration, mono=True)
        
#         start_sample = idx * self.clip_hop_samples_file_sr
#         audio = self.waveform[start_sample:start_sample+self.clip_duration_samples_file_sr]
        audio = audio-np.mean(audio)
        audio = torch.from_numpy(audio)
        if file_sr != self.sr:
          audio = torchaudio.functional.resample(audio, file_sr, self.sr) 
          audio = crop_and_pad(audio, self.sr, self.clip_duration)
        return audio
      
def get_single_clip_data(audio_fp, clip_hop, args, annot_fp = None):
    return DataLoader(
      SingleClipDataset(audio_fp, clip_hop, args, annot_fp = annot_fp),
      batch_size = args.batch_size,
      shuffle=False,
      num_workers=args.num_workers,
      pin_memory=True,
      drop_last=False,
    )

def get_val_dataloader(args):
  val_info_fp = args.val_info_fp  
  val_info_df = pd.read_csv(val_info_fp)
  
  val_dataloaders = {}
  
  for i in range(len(val_info_df)):
    fn = val_info_df.iloc[i]['fn']
    audio_fp = val_info_df.iloc[i]['audio_fp']
    annot_fp = val_info_df.iloc[i]['selection_table_fp']
    val_dataloaders[fn] = get_single_clip_data(audio_fp, args.clip_duration/2, args, annot_fp = annot_fp)
    
  return val_dataloaders

def get_test_dataloader(args):
  test_info_fp = args.test_info_fp
  test_info_df = pd.read_csv(test_info_fp)
  
  test_dataloaders = {}
  
  for i in range(len(test_info_df)):
    fn = test_info_df.iloc[i]['fn']
    audio_fp = test_info_df.iloc[i]['audio_fp']
    annot_fp = test_info_df.iloc[i]['selection_table_fp']
    test_dataloaders[fn] = get_single_clip_data(audio_fp, args.clip_duration/2, args, annot_fp = annot_fp)
    
  return test_dataloaders
  
def get_anchor_anno(start_idx, dur_samples, seq_len):
  # start times plus gaussian blur
  std = dur_samples / 6
  x = (np.arange(seq_len) - start_idx) ** 2
  x = x / (2 * std**2)
  x = np.exp(-x)
  return x
  
def get_unknown_mask(start_idx, dur_samples, seq_len):
  unknown_radius = 1 + int(dur_samples / 6)
  x = np.ones(seq_len)
  x[start_idx - unknown_radius:start_idx + unknown_radius] = 0
  return x