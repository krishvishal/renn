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
from gins.models import ising as ising_models
from torch.nn.init import xavier_uniform_

from gins.utils.utils import corr, l2, l1, get_scores
from functools import partial

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

def bp_infer(ising, msg_iters, solver, optmz_alpha=False):
    '''Do belief propagation with given solver'''
    messages = torch.zeros(ising.n**2, ising.n**2, 2).fill_(0.5).cuda()
    unary_marginals_lbp, binary_marginals_lbp = ising.lbp_marginals(messages)
    
    if optmz_alpha:
        optimizer = torch.optim.Adam([ising.alpha_wgt], lr=0.005)

    for i in range(msg_iters):
        if solver is 'lbp':
            messages = ising.lbp_update(1, messages).detach()
            unary_marginals_lbp_new, binary_marginals_lbp_new = ising.lbp_marginals(messages)
        elif solver is 'alphabp':
            new_messages = ising.alphabp_update(1, messages)
            unary_marginals_lbp_new, binary_marginals_lbp_new = ising.alphabp_marginals(new_messages)
            if optmz_alpha:
                optimizer.zero_grad()
                loss = ising.free_energy_mf(unary_marginals_lbp_new)
                loss.backward()
                optimizer.step()
                for group in optimizer.param_groups:
                    group['params'][0].data.clamp_(-1, 1.5)


            messages = new_messages.detach()

        

        delta_unary = l2(unary_marginals_lbp_new, unary_marginals_lbp) 
        delta_binary = l2(binary_marginals_lbp_new[:, 1, 1], binary_marginals_lbp[:, 1, 1])
        delta = delta_unary + delta_binary
        if delta < args.eps:
            break

        unary_marginals_lbp = unary_marginals_lbp_new.detach()
        binary_marginals_lbp = binary_marginals_lbp_new.detach()

    log_Z_lbp = -ising.bethe_energy(unary_marginals_lbp, binary_marginals_lbp)

    return log_Z_lbp, unary_marginals_lbp, binary_marginals_lbp


def mean_field_infer(ising, args):
    """Run mean field algorithm. """ 

    unary_marginals_mf = torch.zeros(ising.n**2).fill_(0.5).cuda()
    binary_marginals_mf = ising.mf_binary_marginals(unary_marginals_mf)
    
    for i in range(args.msg_iters):
        unary_marginals_mf_new = ising.mf_update(1, unary_marginals_mf)
        binary_marginals_mf_new = ising.mf_binary_marginals(unary_marginals_mf_new)
        delta_unary = l2(unary_marginals_mf_new, unary_marginals_mf) 
        delta_binary = l2(binary_marginals_mf_new[:, 1, 1], binary_marginals_mf[:, 1, 1])
        delta = delta_unary + delta_binary
        if delta < args.eps:
            break
        
        unary_marginals_mf = unary_marginals_mf_new.detach()
        binary_marginals_mf = binary_marginals_mf_new.detach()

    log_Z_mf = -ising.bethe_energy(unary_marginals_mf, binary_marginals_mf)
    log_Z_mf_energy = -ising.free_energy_mf(unary_marginals_mf)

    return (log_Z_mf_energy, unary_marginals_mf, binary_marginals_mf)


def bethe_net_infer(ising, args):
    # inference network
    device = args.device
    encoder = ising_models.TransformerInferenceNetwork(args.n, args.state_dim, args.num_layers)
    encoder.to(device)
    encoder.device = device
    optimizer = torch.optim.Adam(encoder.parameters(), lr=args.lr)
    unary_marginals_enc = torch.zeros(ising.n ** 2).fill_(0.5).to(device)
    binary_marginals_enc = torch.zeros([len(ising.binary_idx), 2, 2]).fill_(0.25).to(device)
        
    for i in range(args.enc_iters):
        optimizer.zero_grad()
        unary_marginals_enc_new, binary_marginals_enc_new = encoder(ising.binary_idx)
        bethe_enc = ising.bethe_energy(unary_marginals_enc_new, binary_marginals_enc_new)
        agreement_loss = encoder.agreement_penalty(ising.binary_idx, unary_marginals_enc_new,
                                                   binary_marginals_enc_new)
        (bethe_enc + args.agreement_pen*agreement_loss).backward()
      
        optimizer.step()
        delta_unary = l2(unary_marginals_enc_new, unary_marginals_enc) 
        delta_binary = l2(binary_marginals_enc_new[:, 1, 1], binary_marginals_enc[:, 1, 1])
        delta = delta_unary + delta_binary
        if delta < args.eps:
            break
    
        unary_marginals_enc = unary_marginals_enc_new.detach()
        binary_marginals_enc = binary_marginals_enc_new.detach()
      
    log_Z_enc = -ising.bethe_energy(unary_marginals_enc, binary_marginals_enc)  

    return (log_Z_enc, unary_marginals_enc, binary_marginals_enc)

def kikuchi_net_infer(ising, args):
    model = ising
    model.generate_region_graph()
    encoder = ising_models.GeneralizedInferenceNetwork(args.n, args.state_dim, args.num_layers, mlp_out_dim=2**4)
    optimizer = torch.optim.Adam(encoder.parameters(), lr=1e-3)
    unary_marginals_enc = torch.zeros(ising.n ** 2).fill_(0.5)
    binary_marginals_enc = torch.zeros([len(ising.binary_idx), 2, 2]).fill_(0.25)

    for i in range(args.enc_iters):
        optimizer.zero_grad()
        infer_beliefs, consist_error = encoder(model.region_graph)
        kikuchi_energy = encoder.kikuchi_energy(log_phis=model.log_phis,\
                                                infer_beliefs=infer_beliefs, \
                                                counts=model.region_graph.collect_region_count())
        loss = kikuchi_energy + args.agreement_pen * consist_error
        loss.backward()

        with torch.no_grad():
            # print(i,loss)
            unary_marginals_enc_new, binary_marginals_enc_new =\
                encoder.read_marginals(binary_idx=model.binary_idx,\
                                       infer_beliefs=infer_beliefs, \
                                       graph=model.region_graph)

            delta_unary = l2(unary_marginals_enc_new, unary_marginals_enc) 
            delta_binary = l2(binary_marginals_enc_new[:, 1, 1], binary_marginals_enc[:, 1, 1])
            delta = delta_unary + delta_binary
            if delta < args.eps:
                break

            unary_marginals_enc = unary_marginals_enc_new.detach()
            binary_marginals_enc = binary_marginals_enc_new.detach()

        optimizer.step()

    return (None, unary_marginals_enc, binary_marginals_enc)

    

def run_marginal_exp(args, seed=3435, verbose=True):
    '''compare the marginals produced by mean field, loopy bp, and inference network'''
    np.random.seed(seed)
    torch.manual_seed(seed)
    
    ising = ising_models.Ising(args.n)
    

    if args.gpu >= 0:
        ising.cuda()
      
        ising.mask = ising.mask.cuda()
        # number of neighbors - 1?
        ising.degree = ising.degree.cuda()  


    log_Z = ising.log_partition_ve()
    unary_marginals, binary_marginals = ising.marginals()
    p_get_scores = partial(get_scores, true_ub=(unary_marginals, binary_marginals))

    all_scores ={}
    if 'mf' in args.method:
        mrgnl_mf = mean_field_infer(ising, args)
        scores_mf = p_get_scores(test_ub=(mrgnl_mf[1], mrgnl_mf[2]))
        all_scores['mf'] = {'l1': scores_mf[0], 'corr': scores_mf[1]}
        print('Finish {} ...'.format('mf'))

    # loopy bp
    if 'bp' in args.method:
        mrgnl_bp = bp_infer(ising, args.msg_iters, 'lbp')
        scores_bp = p_get_scores(test_ub=(mrgnl_bp[1], mrgnl_bp[2]))
        all_scores['bp'] = {'l1': scores_bp[0], 'corr': scores_bp[1]}
        print('Finish {} ...'.format('bp'))


    # alhpa bp
    if 'alphabp' in args.method:
        mrgnl_abp = bp_infer(ising, args.msg_iters, 'alphabp', True)
        scores_abp = p_get_scores(test_ub=(mrgnl_abp[1], mrgnl_abp[2]))
        all_scores['abp'] = {'l1': scores_abp[0], 'corr': scores_abp[1]}
        print('Finish {} ...'.format('alpha bp'))

    # Bethe net
    if 'bethe' in args.method:
        mrgnl_bethe = bethe_net_infer(ising, args)
        scores_bethe = p_get_scores(test_ub=(mrgnl_bethe[1], mrgnl_bethe[2]))
        all_scores['bethe'] = {'l1': scores_bethe[0], 'corr': scores_bethe[1]}
        print('Finish {} ...'.format('bethe'))

    # Generalized net
    if 'kikuchi' in args.method:
        mrgnl_kikuchi = kikuchi_net_infer(ising, args)
        scores_kikuchi = p_get_scores(test_ub=(mrgnl_kikuchi[1].to(unary_marginals), mrgnl_kikuchi[2].to(unary_marginals)))
        all_scores['kikuchi'] = {'l1': scores_kikuchi[0], 'corr': scores_kikuchi[1]}
        print('Finish {} ...'.format('kikuchi'))
        

    if verbose:
        print("This round results:\n {}".format(pd.DataFrame(all_scores)))
    return all_scores

    
if __name__ == '__main__':
    args = parser.parse_args()
    # run multiple number of experiments, and collect the stats of performance.
    args.method = ['mf', 'bp', 'bethe', 'kikuchi']

    # args.method = ['kikuchi']

    args.device = 'cuda:0'
    
    results = {key: {'l1':[], 'corr':[]} for key in args.method}

    for k in range(args.exp_iters):
        d = run_marginal_exp(args, k+1)
        for key, value in d.items():
            for crt, score in value.items():
                results[key][crt].append(score)

    for key, value in results.items():
        for crt, score in value.items():
            results[key][crt] = np.array(score).mean()

    print('Average results: \n {}'.format(pd.DataFrame(results)))

  