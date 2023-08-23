import os
import time
import torch
import argparse
import pdb 
from scipy.spatial import distance_matrix

from parse import parse
from model import SASRec, NewRec, NewB4Rec, BERT4Rec, BPRMF
from utils import *

def train_test(args, sampler, num_batch, model, dataset, epoch_start_idx, write):
    f = open(os.path.join(write, 'log.txt'), 'w')
    adam_optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.98))
    
    T = 0.0
    t0 = time.time()

    if args.triplet_loss or args.cos_loss:
        user_feat = np.loadtxt(f'../data/{args.dataset}_{args.reg_file}.txt')

    for epoch in range(epoch_start_idx, args.num_epochs + 1):
        if args.inference_only: break # just to decrease identition
        if args.model == 'sasrec':
            bce_criterion = torch.nn.BCEWithLogitsLoss()
            for step in range(num_batch):
                u, seq, pos, neg = sampler.next_batch()
                u, seq, pos, neg = np.array(u), np.array(seq), np.array(pos), np.array(neg)
                pos_logits, neg_logits = model(seq, pos, neg)
                pos_labels, neg_labels = torch.ones(pos_logits.shape, device=args.device), torch.zeros(neg_logits.shape, device=args.device)
                adam_optimizer.zero_grad()
                indices = np.where(pos != 0)
                loss = bce_criterion(pos_logits[indices], pos_labels[indices])
                loss += bce_criterion(neg_logits[indices], neg_labels[indices])
                for param in model.item_emb.parameters(): loss += args.l2_emb * torch.norm(param)
                loss.backward()
                adam_optimizer.step()
                print("loss in epoch {} iteration {}: {}".format(epoch, step, loss.item()))

        elif args.model == 'bert4rec':
            ce = torch.nn.CrossEntropyLoss(ignore_index=0)
            for step in range(num_batch):
                seqs, labels = sampler.next_batch()
                seqs, labels = torch.LongTensor(seqs), torch.LongTensor(labels).to(args.device).view(-1)
                logits = model(seqs)
                adam_optimizer.zero_grad()
                loss = ce(logits, labels)
                loss.backward()
                adam_optimizer.step()
                print("loss in epoch {} iteration {}: {}".format(epoch, step, loss.item()))

        elif args.model == 'newrec':
            bce_criterion = torch.nn.BCEWithLogitsLoss()
            for step in range(num_batch):
                u, seq, time1, time2, pos, neg = sampler.next_batch() 
                u, seq, time1, time2, pos, neg = np.array(u), np.array(seq), np.array(time1), np.array(time2), np.array(pos), np.array(neg)
                if args.triplet_loss or args.cos_loss:
                    # find closest and furthest user pairs within sample for regularization
                    batch_dist = distance_matrix(user_feat.T[u-1], user_feat.T[u-1])
                    pos_user = np.argpartition(batch_dist, args.reg_num)[:,:args.reg_num]
                    neg_user = np.argpartition(-batch_dist, args.reg_num)[:,:args.reg_num]
                else:
                    pos_user = np.array([])
                    neg_user = np.array([])
                pos_logits, neg_logits, embed, pos_embed, neg_embed = model(u, seq, time1, time2, pos, neg, pos_user, neg_user)
                pos_labels, neg_labels = torch.ones(pos_logits.shape, device=args.device), torch.zeros(neg_logits.shape, device=args.device)
                adam_optimizer.zero_grad()
                loss = 0
                if args.only_reg:
                    bceloss = 0
                else:
                    indices = np.where(pos != 0)
                    loss += bce_criterion(pos_logits[indices], pos_labels[indices])
                    loss += bce_criterion(neg_logits[indices], neg_labels[indices])
                    bceloss = loss.item()
                loss += args.reg_coef * model.regloss(embed, pos_embed, neg_embed, args.triplet_loss, args.cos_loss)
                loss.backward()
                adam_optimizer.step()
                print("loss in epoch {} iteration {}: bce {} reg {}".format(epoch, step, bceloss, loss.item()-bceloss)) 

        elif args.model == 'newb4rec':
            ce = torch.nn.CrossEntropyLoss(ignore_index=0)
            for step in range(num_batch):
                seqs, labels, t1, t2 = sampler.next_batch()
                seqs, labels, t1, t2 = np.array(seqs), torch.LongTensor(labels).to(args.device).view(-1), np.array(t1), np.array(t2)
                logits = model(seqs, t1, t2)
                adam_optimizer.zero_grad()
                # labels are all shifted to last entry
                loss = ce(logits[labels != 0], torch.full(labels[labels != 0].shape, logits.shape[1] - 1).to(args.device))
                loss.backward()
                adam_optimizer.step()
                print("loss in epoch {} iteration {}: {}".format(epoch, step, loss.item()))

        elif args.model == 'bprmf':
            for step in range(num_batch):
                u, pos, neg = sampler.next_batch() 
                u, pos, neg = np.array(u), np.array(pos), np.array(neg)
                pos_logits, neg_logits = model(u, pos, neg)
                adam_optimizer.zero_grad()
                indices = np.where(pos != 0)
                loss = - (pos_logits[indices] - neg_logits[indices]).sigmoid().log().sum()
                loss.backward()
                adam_optimizer.step()
                print("loss in epoch {} iteration {}: {}".format(epoch, step, loss.item())) 
    
        if epoch % 20 == 0:
            model.eval()
            t1 = time.time() - t0
            T += t1
            print('Evaluating', end='')
            t_valid = evaluate(model, dataset, args)
            print(f"epoch:{epoch}, time: {T} (NDCG@{args.topk[0]}: {t_valid[0][0]}, HR@{args.topk[0]}: {t_valid[0][1]})")
    
            f.write(str(t_valid[0][0]) + ' ' + str(t_valid[0][1]) + '\n')
            f.flush()
            t0 = time.time()
            model.train()

            fname = '{}.epoch={}.lr={}.layer={}.head={}.hidden={}.maxlen={}.pth'
            fname = fname.format(args.model, epoch, args.lr, args.num_blocks, args.num_heads, args.hidden_units, args.maxlen)
            torch.save(model.state_dict(), os.path.join(write, fname))

    if args.inference_only or not args.train_only:
        model.eval()
        t_test = evaluate(model, dataset, args) 
        for i, k in enumerate(args.topk):
            print(f"{args.mode} (NDCG@{k}: {t_test[i][0]}, HR@{k}: {t_test[i][1]})")
    
    if not args.inference_only:
        fname = '{}.epoch={}.lr={}.layer={}.head={}.hidden={}.maxlen={}.pth'
        fname = fname.format(args.model, args.num_epochs, args.lr, args.num_blocks, args.num_heads, args.hidden_units, args.maxlen)
        torch.save(model.state_dict(), os.path.join(write, fname))
    
    f.close()
    sampler.close()
    print("Done")