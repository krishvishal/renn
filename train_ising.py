import sys
import os
import argparse
import json
import random
import shutil
import copy
import pickle
import torch
from torch import cuda
import numpy as np
import time
import logging
import pandas as pd
from torch.nn.init import xavier_uniform_
from functools import partial
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
from rens.models import ising as ising_models
from rens.utils.utils import corr, l2, l1, get_scores, binary2unary_marginals
from rens.models.inference_ising import bp_infer, p2cbp_infer, mean_field_infer, bethe_net_infer, kikuchi_net_infer

# Model options
parser = argparse.ArgumentParser()
parser.add_argument('--n', default=5, type=int, help="ising grid size")
parser.add_argument('--exp_iters', default=5, type=int, help="how many times to run the experiment")
parser.add_argument('--msg_iters', default=200, type=int, help="max number of inference steps")
parser.add_argument('--enc_iters', default=200, type=int, help="max number of encoder grad steps")
parser.add_argument('--eps', default=1e-5, type=float, help="threshold for stopping inference/sgd")
parser.add_argument('--num_layers', default=1, type=int)
parser.add_argument('--state_dim', default=200, type=int)
parser.add_argument('--lr', default=0.001, type=float)
parser.add_argument('--agreement_pen', default=10, type=float, help='')
parser.add_argument('--gpu', default=0, type=int, help='which gpu to use')
parser.add_argument('--seed', default=3435, type=int, help='random seed')
parser.add_argument('--optmz_alpha', action='store_true', help='whether to optimize alphas in alpha bp')
parser.add_argument('--damp', default=0.9, type=float, help='')
parser.add_argument('--unary_std', default=1.0, type=float, help='')

parser.add_argument('--graph_type', default='grid', type=str, help='the graph type of ising model')
parser.add_argument('--data_dir', default='data', type=str, help='dataset dir')
parser.add_argument('--data_regain', action='store_true', help='if to regenerate dataset')
parser.add_argument('--train_size', default=20, type=int, help='the size of training samples')
parser.add_argument('--valid_size', default=10, type=int, help='the size of valid samples')
parser.add_argument('--test_size', default=10, type=int, help='the size of testing samples')
parser.add_argument('--batch_size', default=5, type=int, help='the size of batch samples')
parser.add_argument('--train_iters', default=5, type=int, help='the number of iterations to train')
parser.add_argument('--infer', default='ve', type=str, help='the inference method to use')


class IsingDataset(Dataset):
    """Wrapper for Ising dataset"""
    def __init__(self, dataset, device='cpu'):

        self.len = dataset.size(0)
        self.dataset = dataset.to(device)
        self.device = device

    def __getitem__(self, index):
        return self.dataset[index]

    def __len__(self):
        return self.len
    

def generate_dataset(args):
    """
    Get the training, validate, and testing dataset
    """
    ising = ising_models.Ising(args.n, args.unary_std)
    if not os.path.exists(args.data_dir):
        os.makedirs(args.data_dir)
        
    dataset_dir = os.path.join(args.data_dir, args.graph_type + str(args.n) + '.pkl')
    
    if args.data_regain or not os.path.exists(dataset_dir):
        # generate all required datset
        train_data = ising.sample(args.train_size)
        valid_data = ising.sample(args.valid_size)
        test_data = ising.sample(args.test_size)
        data_dict = {'train': train_data, 'valid': valid_data, 'test': test_data}
        with open(dataset_dir, 'wb') as handle:
            pickle.dump(data_dict, handle)
        
    else:
        # load dataset
        with open(dataset_dir, 'rb') as handle:
            data_dict = pickle.load(handle)

    
    args.dataset = {'train': IsingDataset(data_dict['train'][0], args.device),\
                    'valid': IsingDataset(data_dict['valid'][0], args.device), \
                    'test': IsingDataset(data_dict['test'][0], args.device)}
    args.true_nll = {'train': - torch.mean(data_dict['train'][1]), \
                     'valid': - torch.mean(data_dict['valid'][1]), \
                     'test': - torch.mean(data_dict['test'][1])}
    return args
    

    

def run_marginal_exp(args, seed=3435, verbose=True):
    '''compare the marginals produced by mean field, loopy bp, and inference network'''
    np.random.seed(seed)
    torch.manual_seed(seed)
    
    ising = ising_models.Ising(args.n, args.unary_std)
    

    if args.gpu >= 0:
        ising.cuda()
      
        ising.mask = ising.mask.cuda()
        # number of neighbors - 1?
        ising.degree = ising.degree.cuda()  

    # exact computation on ising
    unary_marginals, binary_marginals = ising.marginals()
    
    log_Z = ising.log_partition_ve()
    
    
    # prepare dataset
    train_data_loader = DataLoader(dataset=args.dataset['train'],
                                   batch_size=args.batch_size,
                                   shuffle=True,
                                   drop_last=False)
    test_data_loader = DataLoader(dataset=args.dataset['test'],
                                  batch_size=args.batch_size,
                                  shuffle=True,
                                  drop_last=False)
    
    optimizer = torch.optim.Adam(ising.parameters(), lr=args.lr)
    

    # training with exact inference (variable elimination)
    if args.infer == 've':
        inference_method = ising.log_partition_ve
    elif args.infer == 'lbp':
        inference_method = partial(bp_infer, ising=ising, args=args, solver='lbp')
    else:
        print("Your assigned inference method is not available.")
        os.exit(1)

    best_nll = float('inf')
    for _ in range(args.train_iters):
        train_avg_nll = ising.trainer(train_data_loader, inference_method, 1, optimizer)
        test_avg_nll = ising.test_nll(test_data_loader, inference_method)
        if best_nll > test_avg_nll:
            best_nll = test_avg_nll
        print("train_avg_nll:{:8.5f} | test_avg_nll: {:8.5f} | best_nll: {:8.5f} | true_nll {:8.5f}".format(train_avg_nll, test_avg_nll, best_nll, args.true_nll['test']))

    
    return best_nll

    
if __name__ == '__main__':
    args = parser.parse_args()
    # run multiple number of experiments, and collect the stats of performance.
    # args.method = ['mf', 'bp', 'gbp', 'bethe', 'kikuchi']

    args.method = ['mf', 'bp', 'dbp', 'abp']
    args.method = ['mf', 'bp','bethe', 'kikuchi']

    args.device = 'cuda:0'

    # generate the dataset
    args = generate_dataset(args)
    
    results = {key: {'l1':[], 'corr':[]} for key in args.method}

    for k in range(args.exp_iters):
        d = run_marginal_exp(args, k+10)
        for key, value in d.items():
            for crt, score in value.items():
                results[key][crt].append(score)

    for key, value in results.items():
        for crt, score in value.items():
            results[key][crt] = {'mu': np.array(score).mean().round(decimals=6), \
                                 'std': np.std(np.array(score)).round(decimals=6)}

    print('Average results: \n {}'.format(pd.DataFrame.from_dict(results, orient='index')))

  
