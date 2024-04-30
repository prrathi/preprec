import numpy as np
import torch
import pdb
from model_utils import *
import random


class NewRec(torch.nn.Module):
    def __init__(self, user_num, item_num, args, second=False):
        super(NewRec, self).__init__()
        assert args.base_dim1 == 0 or args.input_units1 % args.base_dim1 == 0
        assert args.base_dim2 == 0 or args.input_units2 % args.base_dim2 == 0

        self.user_num = user_num
        self.item_num = item_num
        self.dev = args.device
        self.model = args.model
        self.no_emb = args.no_emb
        self.no_fixed_emb = not args.no_emb and args.no_fixed_emb
        self.num_heads = 1
        dataset = args.dataset if not second else args.dataset2
        self.prev_time = args.prev_time
        self.lag = args.lag
        self.time_embed = args.time_embed
        self.time_no_fixed_embed = args.time_no_fixed_embed
        self.time_embed_concat = args.time_embed_concat
        self.pause = args.pause
        self.use_week_eval = args.use_week_eval
        self.maxlen = args.maxlen

        # takes in item id, outputs pre-processed time-sensitive item popularity
        # pdb.set_trace()
        self.popularity_enc = PopularityEncoding(args, second)
        # modify item popularity within most recent week in testing
        self.use_week_eval = args.use_week_eval
        if self.use_week_eval:
            self.eval_popularity_enc = EvalPopularityEncoding(args)
        # takes in item popularity feature, outputs item embedding
        self.embed_layer = InitFeedForward(
            args.input_units1 + args.input_units2,
            args.hidden_units * 2,
            args.hidden_units,
        )
        if args.fs_emb:
            self.fs_layer = InitFeedForward(
                args.hidden_units,
                args.hidden_units * 2,
                args.hidden_units,
            )
        elif args.fs_emb_2:
            self.fs_layer = InitFeedForward3(args.hidden_units, args.hidden_units)
        self.fs_emb = args.fs_emb or args.fs_emb_2
        # trainable positional embeddings
        if self.no_fixed_emb:
            self.pos_emb = torch.nn.Embedding(args.maxlen, args.hidden_units)
        # fixed sinusoidal positional embedding
        elif not self.no_emb:
            self.position_enc = PositionalEncoding(args.hidden_units, args.maxlen)
        # relative time difference embeddings
        if self.time_embed:
            # trainable
            if self.time_no_fixed_embed:
                self.time_pos_emb = torch.nn.Embedding(args.maxlen+1, args.hidden_units)
            # fixed sinusoidal
            else:
                self.time_position_enc = ModPositionalEncoding(args.hidden_units, args.maxlen+1)

        # include svd cooccurrence-based item features
        self.itemgrp = args.itemgrp
        if self.itemgrp:
            self.comb = args.comb
            self.grp_feat = np.loadtxt(f"../data/{dataset}_{args.itemgrp_file}.txt")
            self.num_heads += 1

        # include user percentile trajectory, as additional MLP or attention
        self.traj_form = args.traj_form
        if self.traj_form != '':
            self.traj_dim = args.traj_dim
            self.traj_form = args.traj_form
            self.user_traj = np.loadtxt(f"../data/{dataset}_{args.traj_file}.txt")
            self.user_traj = np.concatenate(
                (np.zeros((self.traj_dim, self.user_traj.shape[1])), self.user_traj)
            )
            if self.traj_form == 'mlp':
                self.traj_layer = InitFeedForward(
                    self.traj_dim, args.hidden_units * 2, args.hidden_units
                )
            elif self.traj_form == 'attention':
                self.comb = args.comb
                self.traj_perc = args.traj_perc
                self.user_enc = UserActivityEncoding(
                    args.hidden_units, self.traj_perc, args.traj_enc_type
                )
                self.num_heads += 1

        # second head with gate at end if item cooccurrence or user trajectory used
        self.hidden_units = args.hidden_units * self.num_heads
        if self.num_heads == 2:
            self.gate = Gate(args.hidden_units)
        if self.num_heads == 2:
            self.embed_layer2 = InitFeedForward2(args.hidden_units, args.hidden_units)

        if args.triplet_loss:
            self.triplet_loss = torch.nn.TripletMarginLoss(margin=0.0, p=2)
        if args.cos_loss:
            self.cos_loss = (
                torch.nn.CosineEmbeddingLoss()
            )

        self.attention_layernorms = torch.nn.ModuleList()  # to be Q for self-attention
        self.attention_layers = torch.nn.ModuleList()
        self.forward_layernorms = torch.nn.ModuleList()
        self.forward_layers = torch.nn.ModuleList()

        self.last_layernorm = torch.nn.LayerNorm(self.hidden_units, eps=1e-8)

        for _ in range(args.num_blocks):
            new_attn_layernorm = torch.nn.LayerNorm(self.hidden_units, eps=1e-8)
            self.attention_layernorms.append(new_attn_layernorm)

            new_attn_layer = CausalMultiHeadAttention(
                self.hidden_units, self.num_heads, args.dropout_rate, self.dev
            )
            self.attention_layers.append(new_attn_layer)

            new_fwd_layernorm = torch.nn.LayerNorm(self.hidden_units, eps=1e-8)
            self.forward_layernorms.append(new_fwd_layernorm)

            new_fwd_layer = PointWiseFeedForward(self.hidden_units, args.dropout_rate)
            self.forward_layers.append(new_fwd_layer)

    def log2feats(self, users, log_seqs, time1_seqs, time2_seqs, time_embed):
        # obtain popularity-based feature vectors for sequence history, apply embedding layer, add positional encoding
        seqs = self.popularity_enc(log_seqs, time1_seqs, time2_seqs)
        seqs = self.embed_layer(seqs)
        if self.fs_emb:
            seqs = self.fs_layer(seqs) 
        if self.no_fixed_emb:
            positions = np.tile(
                np.array(range(log_seqs.shape[1])), [log_seqs.shape[0], 1]
            )
            seqs += self.pos_emb(torch.LongTensor(positions).to(self.dev))
        elif not self.no_emb:
            seqs += self.position_enc(seqs)

        # apply relative time encoding/embedding
        if self.time_embed:
            if self.time_no_fixed_embed:
                timeres = self.time_pos_emb(torch.LongTensor(time_embed).to(self.dev))
            else:
                timeres = self.time_position_enc(time_embed)
            if self.time_embed_concat:
                seqs = torch.stack((seqs, timeres), dim=2).view(seqs.shape[0], -1, seqs.shape[2])
            else:
                seqs += timeres 

        # apply item co-occurrence feature
        if self.itemgrp:
            grp_seqs = self.embed_layer2(torch.Tensor(self.grp_feat[log_seqs, :]).to(self.dev))
            seqs = torch.cat((seqs, grp_seqs), dim=-1)

        # apply user trajectory feature
        if self.traj_form == 'attention':
            try:
                user_percs = self.user_traj.T[
                    np.repeat(np.expand_dims(users - 1, -1), time1_seqs.shape[1], -1),
                    time1_seqs.astype(int) + self.traj_dim
                ]
            except:
                pdb.set_trace()
            user_percs = np.minimum(99, user_percs*self.traj_perc/100).astype(int)
            user_seqs = self.embed_layer2(self.user_enc(user_percs).squeeze(0).to(self.dev))
            seqs = torch.cat((seqs, user_seqs), dim=-1)

        # apply relative time concatenated
        if self.time_embed and self.time_embed_concat:
            timeline_mask = torch.repeat_interleave(torch.BoolTensor(log_seqs == 0), 2, dim=1).to(self.dev)
        else:
            timeline_mask = torch.BoolTensor(log_seqs == 0).to(self.dev)
        seqs *= ~timeline_mask.unsqueeze(-1)  # broadcast in last dim
        tl = seqs.shape[1]  # time dim len for enforce causality
        attention_mask = ~torch.tril(
            torch.ones((tl, tl), dtype=torch.bool, device=self.dev)
        )

        # run attention
        for i in range(len(self.attention_layers)):
            Q = self.attention_layernorms[i](seqs)
            mha_outputs = self.attention_layers[i](
                Q, seqs, time_mask=timeline_mask, attn_mask=attention_mask
            )
            seqs = Q + mha_outputs

            seqs = self.forward_layernorms[i](seqs)
            seqs = self.forward_layers[i](seqs)
            seqs *= ~timeline_mask.unsqueeze(-1)

        # final layer to get user feature at each sequence position
        log_feats = self.last_layernorm(seqs)  # (U, T, C) -> (U, -1, C)
        if self.num_heads == 2:
            log_feats = self.gate(log_feats[:, :, :self.hidden_units//self.num_heads], log_feats[:, :, self.hidden_units//self.num_heads:])

        if self.time_embed_concat:
            log_feats = log_feats[:, np.arange(2*self.maxlen, step=2)]
        return log_feats

    def user2feats(self, users, time1_seqs):
        user_transf = self.user_traj.T[
            np.repeat(np.expand_dims(users - 1, -1), time1_seqs.shape[1], -1),
            np.linspace(time1_seqs, time1_seqs + self.traj_dim, self.traj_dim).astype(
                int
            ),
        ]
        user_transf = np.reshape(np.moveaxis(user_transf, 0, 2), (-1, self.traj_dim))
        user_embed = self.traj_layer(torch.Tensor(user_transf).to(self.dev))
        return torch.reshape(user_embed, (time1_seqs.shape[0], time1_seqs.shape[1], -1))

    def forward(
        self,
        users,
        log_seqs,
        time1_seqs,
        time2_seqs,
        time_embed,
        pos_seqs,
        neg_seqs,
        pos_user,
        neg_user,
    ):  
        # for training
        # avoid information leakage with lag >= 1
        time1_seqs, time2_seqs = np.maximum(0, time1_seqs - 1 - self.lag//4), np.maximum(0, time2_seqs - self.lag)
        # obtain user feature at each position
        log_feats = self.log2feats(users, log_seqs, time1_seqs[:,:-1], time2_seqs[:,:-1], time_embed)
        if self.traj_form == 'mlp':
            user_feats = self.user2feats(users, time1_seqs[:,:-1])
            full_feats = 0.5 * log_feats + 0.5 * user_feats
        else:
            full_feats = log_feats
        # if regularization get last position positive and negative user representations across the batch
        pos_embed = log_feats[:, -1, :][pos_user]
        neg_embed = log_feats[:, -1, :][neg_user]

        # use previous or current interaction time (lag is also applied)
        if self.prev_time:
            mod_time1, mod_time2 = time1_seqs[:,:-1], time2_seqs[:,:-1]
        else:
            mod_time1, mod_time2 = time1_seqs[:,1:], time1_seqs[:,1:]
        # obtain popularity-based embeddings for positive and negative item sequences
        pos_embs = self.embed_layer(
            self.popularity_enc(pos_seqs, mod_time1, mod_time2)
        )
        neg_embs = self.embed_layer(
            self.popularity_enc(neg_seqs, mod_time1, mod_time2)
        )

        # combine embeddings with item cooccurrence based features 
        if self.itemgrp:
            if self.comb:
                grp_pos = torch.Tensor(self.grp_feat[pos_seqs, :]).to(self.dev)
                pos_embs = self.gate(pos_embs, grp_pos)
                grp_neg = torch.Tensor(self.grp_feat[neg_seqs, :]).to(self.dev)
                neg_embs = self.gate(neg_embs, grp_neg)
        # combine embeddings with user trajectory representation
        if self.traj_form == 'attention':
            if self.comb:
                pos_percs = self.user_traj.T[
                    np.repeat(np.expand_dims(users - 1, -1), time1_seqs.shape[1], -1),
                    time1_seqs + self.traj_dim
                ]
                pos_percs = np.minimum(99, pos_percs*self.traj_perc/100).astype(int)
                pos_percs = self.user_enc(pos_percs).squeeze(0).to(self.dev)
                pos_embs = self.gate(pos_embs, pos_percs)
                neg_percs = self.user_traj.T[
                    np.repeat(np.expand_dims(users - 1, -1), time1_seqs.shape[1], -1),
                    time1_seqs + self.traj_dim
                ]
                neg_percs = np.minimum(99, neg_percs*self.traj_perc/100).astype(int)
                neg_percs = self.user_enc(neg_percs).squeeze(0).to(self.dev)
                neg_embs = self.gate(neg_embs, neg_percs)
            else:
                pass

        pos_logits = (full_feats * pos_embs).sum(dim=-1)
        neg_logits = (full_feats * neg_embs).sum(dim=-1)

        return pos_logits, neg_logits, full_feats[:, -1, :], pos_embed, neg_embed

    def raw(
            self,
            log_seqs,
            time1_seqs,
            time2_seqs,
    ):
        # for training
        # avoid information leakage with lag >= 1
        time1_seqs, time2_seqs = np.maximum(0, time1_seqs - 1 - self.lag // 4), np.maximum(0, time2_seqs - self.lag)
        pop_enc = self.popularity_enc(log_seqs, time1_seqs, time2_seqs).cpu()
        return pop_enc.numpy()

    def user_score(self, log_seqs, time1_seqs, time2_seqs, time_embed, user):
        log_feats =  self.log2feats(user, log_seqs, time1_seqs, time2_seqs, time_embed)
        return log_feats[:, -1, :]

    def handle_inference(self):
        del self.popularity_enc
        return

    def predict(
        self, log_seqs, time1_seqs, time2_seqs, time_embed, item_indices, time1_pred, time2_pred, user
    ):  
        # for inference
        # obtain user feature at each position
        log_feats = self.log2feats(user, log_seqs, time1_seqs, time2_seqs, time_embed)
        if self.traj_form == 'mlp':
            user_feats = self.user2feats(user, time1_seqs)
            full_feats = 0.5 * log_feats + 0.5 * user_feats
        else:
            full_feats = log_feats
        final_feat = full_feats[:, -1, :]

        # apply most recent week popularity adjustment
        if self.use_week_eval:
            item_embs = self.embed_layer(
                self.eval_popularity_enc(
                    item_indices, time1_pred, time2_pred, user
                )
            )
        else:
            item_embs = self.embed_layer(
                self.popularity_enc(
                    item_indices, time1_pred, time2_pred
                )
            )

        # apply user trajectory
        if self.traj_form == 'attention' and self.comb:
            user_percs = self.user_traj.T[
                np.repeat(np.expand_dims(user - 1, -1), item_embs.shape[1], -1),
                time1_pred + self.traj_dim
            ]
            user_percs = np.minimum(99, user_percs*self.traj_perc/100).astype(int)
            user_percs = self.user_enc(user_percs).squeeze(0).to(self.dev)
            item_embs = self.gate(item_embs, user_percs)

        logits = item_embs.matmul(final_feat.unsqueeze(-1)).squeeze(-1)

        return logits

    def regloss(self, users, pos_users, neg_users, triplet_loss, cos_loss):
        if not triplet_loss and not cos_loss:
            return 0
        users, pos_users, neg_users = (
            users.to(self.dev),
            pos_users.to(self.dev),
            neg_users.to(self.dev),
        )
        loss = 0
        if triplet_loss:
            loss += self.triplet_loss(torch.unsqueeze(users, 1), pos_users, neg_users)
        if cos_loss:
            loss += self.cos_loss(
                torch.repeat_interleave(users, repeats=10, dim=0),
                torch.reshape(
                    pos_users,
                    (pos_users.shape[0] * pos_users.shape[1], pos_users.shape[2]),
                ),
                torch.Tensor([1]).to(self.dev),
            )
            loss += self.cos_loss(
                torch.repeat_interleave(users, repeats=10, dim=0),
                torch.reshape(
                    neg_users,
                    (neg_users.shape[0] * neg_users.shape[1], neg_users.shape[2]),
                ),
                torch.Tensor([-1]).to(self.dev),
            )
        return loss


class NewB4Rec(torch.nn.Module):
    def __init__(self, itemnum, compare_size, args):
        super(NewB4Rec, self).__init__()
        assert args.input_units1 % args.base_dim1 == 0
        assert args.input_units2 % args.base_dim2 == 0

        self.maxlen = args.maxlen
        self.item_num = itemnum
        self.dev = args.device
        self.no_fixed_emb = args.no_fixed_emb
        self.compare_size = compare_size

        self.popularity_enc = PopularityEncoding(args)
        self.embed_layer = InitFeedForward(
            args.input_units1 + args.input_units2,
            args.hidden_units * 2,
            args.hidden_units,
        )
        if self.no_fixed_emb:
            self.pos_emb = torch.nn.Embedding(args.maxlen, args.hidden_units)
        else:
            self.position_enc = PositionalEncoding(args.hidden_units, args.maxlen)
        self.logsoftmax = torch.nn.LogSoftmax(dim=1)

        if args.triplet_loss:
            self.triplet_loss = torch.nn.TripletMarginLoss(margin=0.0, p=2)
        if args.cos_loss:
            self.cos_loss = (
                torch.nn.CosineEmbeddingLoss()
            )  # torch.nn.CosineSimilarity()

        # multi-layers transformer blocks, deep network
        self.attention_layernorms = torch.nn.ModuleList()  # to be Q for self-attention
        self.attention_layers = torch.nn.ModuleList()
        self.forward_layernorms = torch.nn.ModuleList()
        self.forward_layers = torch.nn.ModuleList()

        for _ in range(args.num_blocks):
            new_attn_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.attention_layernorms.append(new_attn_layernorm)

            new_attn_layer = MultiHeadAttention(
                args.hidden_units, args.num_heads, args.dropout_rate
            )
            self.attention_layers.append(new_attn_layer)

            new_fwd_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.forward_layernorms.append(new_fwd_layernorm)

            new_fwd_layer = PointWiseFeedForward2(
                args.hidden_units, args.hidden_units * 4, args.dropout_rate
            )
            self.forward_layers.append(new_fwd_layer)

        self.out = torch.nn.Linear(args.hidden_units, args.hidden_units)

    def GELU(self, x):
        return (
            0.5
            * x
            * (1 + torch.tanh(np.sqrt(2 / np.pi) * (x + 0.044715 * torch.pow(x, 3))))
        )

    def log2feats(self, log_seqs, time1_seqs, time2_seqs):
        tensor_seqs = torch.LongTensor(log_seqs)
        mask = (
            (tensor_seqs > 0)
            .unsqueeze(1)
            .repeat(1, tensor_seqs.size(1), 1)
            .unsqueeze(1)
            .to(self.dev)
        )
        seqs = self.popularity_enc(log_seqs, time1_seqs, time2_seqs)
        seqs = self.embed_layer(seqs)
        if self.no_fixed_emb:
            positions = np.tile(
                np.array(range(log_seqs.shape[1])), [log_seqs.shape[0], 1]
            )
            seqs += self.pos_emb(torch.LongTensor(positions).to(self.dev))
        else:
            seqs = self.position_enc(seqs)
        for i in range(len(self.attention_layers)):
            # seqs = torch.transpose(seqs, 0, 1)
            Q = self.attention_layernorms[i](seqs)
            mha_outputs = self.attention_layers[i](Q, mask)
            seqs = Q + mha_outputs
            # seqs = torch.transpose(seqs, 0, 1)

            seqs = self.forward_layernorms[i](seqs)
            seqs = self.forward_layers[i](seqs)

        return self.out(seqs)

    def forward(self, seqs, time1_seqs, time2_seqs, candidates=None):
        final_feat = self.log2feats(seqs, time1_seqs, time2_seqs)  # B x T x V
        final_feat = self.GELU(final_feat)
        if candidates is not None:
            items = candidates
            t1 = np.repeat(time1_seqs.flatten()[-1], candidates.shape)
            t2 = np.repeat(time2_seqs.flatten()[-1], candidates.shape)
            items_, t1_, t2_ = (
                np.expand_dims(items, -1),
                np.expand_dims(t1, -1),
                np.expand_dims(t2, -1),
            )
            item_embs = self.embed_layer(self.popularity_enc(items_, t1_, t2_))
            return item_embs.squeeze(1).matmul(final_feat.squeeze(0).T)[:, -1]

        # randomly choose group to rank and obtain loss from, all items is too large, appending actual labels to end of random ones
        items = np.append(
            np.random.choice(
                np.arange(1, self.item_num + 1),
                size=(seqs.shape[0], seqs.shape[1], self.compare_size),
            ),
            np.expand_dims(seqs, axis=-1),
            axis=2,
        )
        t1 = np.tile(np.expand_dims(time1_seqs, -1), (1, 1, self.compare_size + 1))
        t2 = np.tile(np.expand_dims(time2_seqs, -1), (1, 1, self.compare_size + 1))
        items_, t1_, t2_ = (
            items.reshape((items.shape[0], items.shape[1] * items.shape[2])),
            t1.reshape((t1.shape[0], t1.shape[1] * t1.shape[2])),
            t2.reshape((t2.shape[0], t2.shape[1] * t2.shape[2])),
        )
        item_embs = self.embed_layer(self.popularity_enc(items_, t1_, t2_))
        item_embs = item_embs.reshape(
            (item_embs.shape[0], seqs.shape[1], -1, item_embs.shape[-1])
        )
        logits = item_embs.matmul(final_feat.unsqueeze(-1)).squeeze(-1)
        logits = self.logsoftmax(logits)
        logits = logits.view(-1, logits.size(-1))  # (B*T) x V

        return logits

    def predict(self, seqs, time1_seqs, time2_seqs, candidates):
        scores = self.forward(seqs, time1_seqs, time2_seqs, candidates)  # T x V
        return scores


# taken from https://github.com/guoyang9/BPR-pytorch/tree/master
class BPRMF(torch.nn.Module):
    def __init__(self, user_num, item_num, args):
        super(BPRMF, self).__init__()

        self.user_emb = torch.nn.Embedding(user_num + 1, args.hidden_units)
        self.item_emb = torch.nn.Embedding(item_num + 1, args.hidden_units)
        self.dev = args.device

    def forward(self, user, pos_item, neg_item):
        user = self.user_emb(torch.LongTensor(user).to(self.dev))
        item_i = self.item_emb(torch.LongTensor(pos_item).to(self.dev))
        item_j = self.item_emb(torch.LongTensor(neg_item).to(self.dev))

        prediction_i = item_i.matmul(user.unsqueeze(-1)).squeeze(-1)
        prediction_j = item_j.matmul(user.unsqueeze(-1)).squeeze(-1)
        return prediction_i, prediction_j

    def predict(self, user, item_indices):
        user = self.user_emb(torch.LongTensor(user).to(self.dev))
        items = self.item_emb(torch.LongTensor(item_indices).to(self.dev))
        logits = (user * items).sum(dim=-1)
        return logits


# taken from https://github.com/pmixer/SASRec.pytorch/blob/master/model.py
class SASRec(torch.nn.Module):
    def __init__(self, user_num, item_num, args):
        super(SASRec, self).__init__()

        self.user_num = user_num
        self.item_num = item_num
        self.dev = args.device

        self.item_emb = torch.nn.Embedding(
            self.item_num + 1, args.hidden_units, padding_idx=0
        )
        self.pos_emb = torch.nn.Embedding(args.maxlen, args.hidden_units)
        self.emb_dropout = torch.nn.Dropout(p=args.dropout_rate)

        self.attention_layernorms = torch.nn.ModuleList()  # to be Q for self-attention
        self.attention_layers = torch.nn.ModuleList()
        self.forward_layernorms = torch.nn.ModuleList()
        self.forward_layers = torch.nn.ModuleList()

        self.last_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)

        for _ in range(args.num_blocks):
            new_attn_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.attention_layernorms.append(new_attn_layernorm)

            new_attn_layer = CausalMultiHeadAttention(
                args.hidden_units, args.num_heads, args.dropout_rate, self.dev
            )
            self.attention_layers.append(new_attn_layer)

            new_fwd_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.forward_layernorms.append(new_fwd_layernorm)

            new_fwd_layer = PointWiseFeedForward(args.hidden_units, args.dropout_rate)
            self.forward_layers.append(new_fwd_layer)

    def log2feats(self, log_seqs):
        seqs = self.item_emb(torch.LongTensor(log_seqs).to(self.dev))
        seqs *= self.item_emb.embedding_dim**0.5
        positions = np.tile(np.array(range(log_seqs.shape[1])), [log_seqs.shape[0], 1])
        seqs += self.pos_emb(torch.LongTensor(positions).to(self.dev))
        seqs = self.emb_dropout(seqs)

        timeline_mask = torch.BoolTensor(log_seqs == 0).to(self.dev)
        seqs *= ~timeline_mask.unsqueeze(-1)  # broadcast in last dim

        tl = seqs.shape[1]  # time dim len for enforce causality
        attention_mask = ~torch.tril(
            torch.ones((tl, tl), dtype=torch.bool, device=self.dev)
        )

        for i in range(len(self.attention_layers)):
            Q = self.attention_layernorms[i](seqs)
            mha_outputs = self.attention_layers[i](
                Q, seqs, time_mask=timeline_mask, attn_mask=attention_mask
            )
            seqs = Q + mha_outputs

            seqs = self.forward_layernorms[i](seqs)
            seqs = self.forward_layers[i](seqs)
            seqs *= ~timeline_mask.unsqueeze(-1)

        log_feats = self.last_layernorm(seqs)  # (U, T, C) -> (U, -1, C)

        return log_feats

    def forward(self, log_seqs, pos_seqs, neg_seqs):  # for training
        log_feats = self.log2feats(log_seqs)  # user_ids hasn't been used yet

        pos_embs = self.item_emb(torch.LongTensor(pos_seqs).to(self.dev))
        neg_embs = self.item_emb(torch.LongTensor(neg_seqs).to(self.dev))

        pos_logits = (log_feats * pos_embs).sum(dim=-1)
        neg_logits = (log_feats * neg_embs).sum(dim=-1)

        return pos_logits, neg_logits

    def predict(self, log_seqs, item_indices):  # for inference
        log_feats = self.log2feats(log_seqs)  # user_ids hasn't been used yet
        final_feat = log_feats[:, -1, :]  # only use last QKV classifier, a waste

        item_embs = self.item_emb(
            torch.LongTensor(item_indices).to(self.dev)
        )  # (U, I, C)
        logits = item_embs.matmul(final_feat.unsqueeze(-1)).squeeze(-1)

        return logits


# adapted from https://github.com/jaywonchung/BERT4Rec-VAE-Pytorch/tree/master
class BERT4Rec(torch.nn.Module):
    def __init__(self, itemnum, args):
        super(BERT4Rec, self).__init__()
        self.maxlen = args.maxlen
        self.item_num = itemnum
        self.dev = args.device

        self.item_emb = torch.nn.Embedding(
            self.item_num + 1, args.hidden_units, padding_idx=0
        )
        self.pos_emb = torch.nn.Embedding(args.maxlen, args.hidden_units)
        self.emb_dropout = torch.nn.Dropout(p=args.dropout_rate)
        self.logsoftmax = torch.nn.LogSoftmax(dim=1)
        self.pause = args.pause

        # multi-layers transformer blocks, deep network
        self.attention_layernorms = torch.nn.ModuleList()  # to be Q for self-attention
        self.attention_layers = torch.nn.ModuleList()
        self.forward_layernorms = torch.nn.ModuleList()
        self.forward_layers = torch.nn.ModuleList()

        for _ in range(args.num_blocks):
            new_attn_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.attention_layernorms.append(new_attn_layernorm)

            new_attn_layer = MultiHeadAttention(
                args.hidden_units, args.num_heads, args.dropout_rate
            )
            self.attention_layers.append(new_attn_layer)

            new_fwd_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.forward_layernorms.append(new_fwd_layernorm)

            new_fwd_layer = PointWiseFeedForward2(
                args.hidden_units, args.hidden_units * 4, args.dropout_rate
            )
            self.forward_layers.append(new_fwd_layer)

        self.out = torch.nn.Linear(
            args.hidden_units, args.hidden_units) #, self.item_num+1

    def GELU(self, x):
        return (
            0.5
            * x
            * (1 + torch.tanh(np.sqrt(2 / np.pi) * (x + 0.044715 * torch.pow(x, 3))))
        )

    def log2feats(self, log_seqs):
        mask = (
            (log_seqs > 0)
            .unsqueeze(1)
            .repeat(1, log_seqs.size(1), 1)
            .unsqueeze(1)
            .to(self.dev)
        )

        # embedding the indexed sequence to sequence of vectors
        seqs = self.item_emb(log_seqs.to(self.dev))
        seqs *= self.item_emb.embedding_dim**0.5
        positions = np.tile(np.array(range(log_seqs.shape[1])), [log_seqs.shape[0], 1])
        seqs += self.pos_emb(torch.LongTensor(positions).to(self.dev))
        seqs = self.emb_dropout(seqs)

        for i in range(len(self.attention_layers)):
            # seqs = torch.transpose(seqs, 0, 1)
            Q = self.attention_layernorms[i](seqs)
            mha_outputs = self.attention_layers[i](Q, mask)
            seqs = Q + mha_outputs
            # seqs = torch.transpose(seqs, 0, 1)

            seqs = self.forward_layernorms[i](seqs)
            seqs = self.forward_layers[i](seqs)

        return self.out(seqs)

    def forward(self, seqs):
        final_feat = self.log2feats(seqs)
        # final_feat = self.GELU(final_feat)
        item_embs = self.item_emb(torch.arange(0, self.item_num + 1).to(self.dev))
        logits = item_embs.matmul(final_feat.unsqueeze(-1)).squeeze(-1)
        logits = logits.view(-1, logits.size(-1))  # (B*T) x V
        # logits = self.logsoftmax(logits)

        return logits

    def predict(self, seqs, candidates):
        scores = self.forward(seqs)  # T x V
        candidates = candidates.to(self.dev)
        scores = torch.reshape(scores, (seqs.shape[0], seqs.shape[1], -1))[:,-1,:]
        if len(candidates.shape) == 1:
            candidates = torch.unsqueeze(candidates, 0)
        scores = scores.gather(1, candidates)
        # else:
            # scores = scores[-1, :]
            # scores = scores.gather(0, candidates)

        return scores


class CL4SRec(torch.nn.Module):
    def __init__(self, item_num, args):
        super(CL4SRec, self).__init__()

        self.n_items = item_num
        self.dev = args.device
        self.batch_size = args.batch_size

        self.mask_default = self.mask_correlated_samples(batch_size=self.batch_size)
        self.item_emb = torch.nn.Embedding(
            self.n_items + 1, args.hidden_units, padding_idx=0
        )
        self.pos_emb = torch.nn.Embedding(args.maxlen, args.hidden_units)
        self.emb_dropout = torch.nn.Dropout(p=args.dropout_rate)
        self.ce = torch.nn.CrossEntropyLoss()

        self.attention_layernorms = torch.nn.ModuleList()  # to be Q for self-attention
        self.attention_layers = torch.nn.ModuleList()
        self.forward_layernorms = torch.nn.ModuleList()
        self.forward_layers = torch.nn.ModuleList()

        self.last_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)

        for _ in range(args.num_blocks):
            new_attn_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.attention_layernorms.append(new_attn_layernorm)

            new_attn_layer = CausalMultiHeadAttention(
                args.hidden_units, args.num_heads, args.dropout_rate, self.dev
            )
            self.attention_layers.append(new_attn_layer)

            new_fwd_layernorm = torch.nn.LayerNorm(args.hidden_units, eps=1e-8)
            self.forward_layernorms.append(new_fwd_layernorm)

            new_fwd_layer = PointWiseFeedForward(args.hidden_units, args.dropout_rate)
            self.forward_layers.append(new_fwd_layer)

    def log2feats(self, log_seqs, skip_dev=False):
        if not skip_dev:
            seqs = self.item_emb(torch.LongTensor(log_seqs).to(self.dev))
        else:
            seqs = self.item_emb(log_seqs)
        seqs *= self.item_emb.embedding_dim**0.5
        positions = np.tile(np.array(range(log_seqs.shape[1])), [log_seqs.shape[0], 1])
        seqs += self.pos_emb(torch.LongTensor(positions).to(self.dev))
        seqs = self.emb_dropout(seqs)

        if not skip_dev:
            timeline_mask = torch.BoolTensor(log_seqs == 0).to(self.dev)
        else:
            timeline_mask = (log_seqs == 0).bool().to(log_seqs.device)
        seqs *= ~timeline_mask.unsqueeze(-1)  # broadcast in last dim

        tl = seqs.shape[1]  # time dim len for enforce causality
        attention_mask = ~torch.tril(
            torch.ones((tl, tl), dtype=torch.bool, device=self.dev)
        )

        for i in range(len(self.attention_layers)):
            Q = self.attention_layernorms[i](seqs)
            mha_outputs = self.attention_layers[i](
                Q, seqs, time_mask=timeline_mask, attn_mask=attention_mask
            )
            seqs = Q + mha_outputs

            seqs = self.forward_layernorms[i](seqs)
            seqs = self.forward_layers[i](seqs)
            seqs *= ~timeline_mask.unsqueeze(-1)

        log_feats = self.last_layernorm(seqs)  # (U, T, C) -> (U, -1, C)

        return log_feats

    def item_crop(self, item_seq, item_seq_len, eta=0.6):
        num_left = math.floor(item_seq_len * eta)
        if item_seq_len.cpu() - num_left <= 1:
            return item_seq
        crop_begin = random.randint(1, item_seq_len.cpu() - num_left)
        croped_item_seq = np.zeros(item_seq.shape)
        croped_item_seq[-num_left:] = item_seq.cpu().detach().numpy()[-num_left - crop_begin:-crop_begin]
        return torch.tensor(croped_item_seq, dtype=torch.long, device=item_seq.device)

    def item_mask(self, item_seq, item_seq_len, gamma=0.3):
        num_mask = math.floor(item_seq_len * gamma)
        mask_index = np.random.randint(1, item_seq_len.cpu()+1, num_mask)
        # mask_index = random.sample(range(1, item_seq_len+1), k=num_mask)
        masked_item_seq = item_seq.cpu().detach().numpy().copy()
        masked_item_seq[-mask_index] = 0 # token 0 has been used for semantic masking
        return torch.tensor(masked_item_seq, dtype=torch.long, device=item_seq.device)

    def item_reorder(self, item_seq, item_seq_len, beta=0.6):
        num_reorder = math.floor(item_seq_len * beta)
        if item_seq_len.cpu() - num_reorder <= 1:
            return item_seq
        reorder_begin = np.random.randint(1, item_seq_len.cpu() - num_reorder)
        reordered_item_seq = item_seq.cpu().detach().numpy().copy()
        shuffle_index = np.arange(-reorder_begin - num_reorder, -reorder_begin)
        np.random.shuffle(shuffle_index)
        reordered_item_seq[shuffle_index] = reordered_item_seq[-reorder_begin - num_reorder:-reorder_begin]
        return torch.tensor(reordered_item_seq, dtype=torch.long, device=item_seq.device)

    def augment(self, log_seq_np, log_seq_len):
        log_seq = torch.LongTensor(log_seq_np).to(self.dev)
        aug_seq1 = torch.clone(log_seq)
        aug_seq2 = torch.clone(log_seq)
        for i in range(log_seq.shape[0]):
            switch = random.sample(range(3), k=2)
            if switch[0] == 0:
                aug_seq1[i]= self.item_crop(log_seq[i], log_seq_len[i])
            elif switch[0] == 1:
                aug_seq1[i] = self.item_mask(log_seq[i], log_seq_len[i])
            elif switch[0] == 2:
                aug_seq1[i] = self.item_reorder(log_seq[i], log_seq_len[i])
            if switch[1] == 0:
                aug_seq2[i] = self.item_crop(log_seq[i], log_seq_len[i])
            elif switch[1] == 1:
                aug_seq2[i] = self.item_mask(log_seq[i], log_seq_len[i])
            elif switch[1] == 2:
                aug_seq2[i] = self.item_reorder(log_seq[i], log_seq_len[i])
        return aug_seq1, aug_seq2

    def mask_correlated_samples(self, batch_size):
        N = 2 * batch_size
        mask = torch.ones((N, N), dtype=bool)
        mask = mask.fill_diagonal_(0)
        for i in range(batch_size):
            mask[i, batch_size + i] = 0
            mask[batch_size + i, i] = 0
        return mask

    def info_nce(self, z_i, z_j, batch_size, temp=1, sim='dot'):
        N = 2 * batch_size
        z = torch.cat((z_i, z_j), dim=0)
        if sim == 'cos':
            sim = nn.functional.cosine_similarity(z.unsqueeze(1), z.unsqueeze(0), dim=2) / temp
        elif sim == 'dot':
            sim = torch.mm(z, z.T) / temp

        sim_i_j = torch.diag(sim, batch_size)
        sim_j_i = torch.diag(sim, -batch_size)

        positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0).reshape(N, 1)
        if batch_size != self.batch_size:
            mask = self.mask_correlated_samples(batch_size)
        else:
            mask = self.mask_default
        negative_samples = sim[mask].reshape(N, -1)

        labels = torch.zeros(N).to(positive_samples.device).long()
        logits = torch.cat((positive_samples, negative_samples), dim=1)
        return self.ce(logits, labels)

    def forward(self, log_seqs, item_seq_len, pos_seqs, neg_seqs):  # for training
        log_feats = self.log2feats(log_seqs)  # user_ids hasn't been used yet
        aug_seqs1, aug_seqs2 = self.augment(log_seqs, item_seq_len)
        pos_embs = self.item_emb(torch.LongTensor(pos_seqs).to(self.dev))
        neg_embs = self.item_emb(torch.LongTensor(neg_seqs).to(self.dev))
        pos_logits = (log_feats * pos_embs).sum(dim=-1)
        neg_logits = (log_feats * neg_embs).sum(dim=-1)

        log_aug_feats1 = self.log2feats(aug_seqs1, skip_dev=True)[:, -1, :]
        log_aug_feats2 = self.log2feats(aug_seqs2, skip_dev=True)[:, -1, :]
        aug_loss = self.info_nce(log_aug_feats1, log_aug_feats2, self.batch_size)

        return pos_logits, neg_logits, aug_loss

    def predict(self, log_seqs, item_indices):  # for inference
        log_feats = self.log2feats(log_seqs)  # user_ids hasn't been used yet
        final_feat = log_feats[:, -1, :]  # only use last QKV classifier, a waste

        item_embs = self.item_emb(
            torch.LongTensor(item_indices).to(self.dev)
        )  # (U, I, C)
        logits = item_embs.matmul(final_feat.unsqueeze(-1)).squeeze(-1)

        return logits



# adapted from https://github.com/RuihongQiu/DuoRec/recbole/model/sequential_recommender/cl4srec.py
# class CL4SRec(torch.nn.Module):
#     def __init__(self, num_items, args):
#         super(CL4SRec, self).__init__()
#
#         # load parameters info
#         self.max_seq_length = args.maxlen + 1
#         self.n_items = num_items
#         self.n_layers = args.num_blocks
#         self.n_heads = args.num_heads
#         self.hidden_size = args.hidden_units
#         self.inner_size = args.hidden_units * 4
#         self.hidden_dropout_prob = args.dropout_rate
#         self.attn_dropout_prob = args.dropout_rate
#         self.hidden_act = "gelu"
#         self.layer_norm_eps = 1e-8
#
#         self.batch_size = args.batch_size
#         self.lmd = 0.1
#         self.tau = 1 # args.lmd_tau[1]
#         self.sim = "dot"
#
#         # define layers and loss
#         self.item_embedding = torch.nn.Embedding(self.n_items + 1, self.hidden_size, padding_idx=0)
#         self.position_embedding = torch.nn.Embedding(self.max_seq_length, self.hidden_size)
#         self.trm_encoder = RecboleTransformerEncoder(
#             n_layers=self.n_layers,
#             n_heads=self.n_heads,
#             hidden_size=self.hidden_size,
#             inner_size=self.inner_size,
#             hidden_dropout_prob=self.hidden_dropout_prob,
#             attn_dropout_prob=self.attn_dropout_prob,
#             hidden_act=self.hidden_act,
#             layer_norm_eps=self.layer_norm_eps
#         )
#
#         self.LayerNorm = torch.nn.LayerNorm(self.hidden_size, eps=self.layer_norm_eps)
#         self.dropout = torch.nn.Dropout(self.hidden_dropout_prob)
#         self.mask_default = self.mask_correlated_samples(batch_size=self.batch_size)
#         self.nce_fct = torch.nn.CrossEntropyLoss()
#         self.loss_fct = torch.nn.CrossEntropyLoss()
#         self.recbole_loss = args.recbole_loss
#         self.recbole_only_main = args.recbole_only_main
#
#     def gather_indexes(self, output, gather_index):
#         """Gathers the vectors at the specific positions over a minibatch"""
#         gather_index = gather_index.view(-1, 1, 1).expand(-1, -1, output.shape[-1])
#         output_tensor = output.gather(dim=1, index=gather_index)
#         return output_tensor.squeeze(1)
#
#     def get_attention_mask(self, item_seq):
#         """Generate left-to-right uni-directional attention mask for multi-head attention."""
#         attention_mask = (item_seq > 0).long()
#         extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)  # torch.int64
#         # mask for left-to-right unidirectional
#         max_len = attention_mask.size(-1)
#         attn_shape = (1, max_len, max_len)
#         subsequent_mask = torch.triu(torch.ones(attn_shape), diagonal=1)  # torch.uint8
#         subsequent_mask = (subsequent_mask == 0).unsqueeze(1)
#         subsequent_mask = subsequent_mask.long().to(item_seq.device)
#
#         extended_attention_mask = extended_attention_mask * subsequent_mask
#         extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
#         extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
#         return extended_attention_mask
#
#     def augment(self, item_seq, item_seq_len):
#         aug_seq1 = torch.clone(item_seq)
#         aug_len1 = torch.clone(item_seq_len)
#         aug_seq2 = torch.clone(item_seq)
#         aug_len2 = torch.clone(item_seq_len)
#         for i in range(item_seq.shape[0]):
#             switch = random.sample(range(3), k=2)
#             if switch[0] == 0:
#                 aug_seq1[i], aug_len1[i] = self.item_crop(item_seq[i], item_seq_len[i])
#             elif switch[0] == 1:
#                 aug_seq1[i], aug_len1[i] = self.item_mask(item_seq[i], item_seq_len[i])
#             elif switch[0] == 2:
#                 aug_seq1[i], aug_len1[i] = self.item_reorder(item_seq[i], item_seq_len[i])
#             if switch[1] == 0:
#                 aug_seq2[i], aug_len2[i] = self.item_crop(item_seq[i], item_seq_len[i])
#             elif switch[1] == 1:
#                 aug_seq2[i], aug_len2[i] = self.item_mask(item_seq[i], item_seq_len[i])
#             elif switch[1] == 2:
#                 aug_seq2[i], aug_len2[i] = self.item_reorder(item_seq[i], item_seq_len[i])
#         return aug_seq1, aug_len1, aug_seq2, aug_len2
#
#     def item_crop(self, item_seq, item_seq_len, eta=0.6):
#         num_left = math.floor(item_seq_len * eta)
#         crop_begin = random.randint(0, item_seq_len - num_left)
#         croped_item_seq = np.zeros(item_seq.shape[0])
#         if crop_begin + num_left < item_seq.shape[0]:
#             croped_item_seq[:num_left] = item_seq.cpu().detach().numpy()[crop_begin:crop_begin + num_left]
#         else:
#             croped_item_seq[:num_left] = item_seq.cpu().detach().numpy()[crop_begin:]
#         return torch.tensor(croped_item_seq, dtype=torch.long, device=item_seq.device), \
#             torch.tensor(num_left, dtype=torch.long, device=item_seq.device)
#
#     def item_mask(self, item_seq, item_seq_len, gamma=0.3):
#         num_mask = math.floor(item_seq_len * gamma)
#         mask_index = random.sample(range(item_seq_len), k=num_mask)
#         masked_item_seq = item_seq.cpu().detach().numpy().copy()
#         masked_item_seq[mask_index] = self.n_items  # token 0 has been used for semantic masking
#         return torch.tensor(masked_item_seq, dtype=torch.long, device=item_seq.device), item_seq_len
#
#     def item_reorder(self, item_seq, item_seq_len, beta=0.6):
#         num_reorder = math.floor(item_seq_len * beta)
#         reorder_begin = random.randint(0, item_seq_len - num_reorder)
#         reordered_item_seq = item_seq.cpu().detach().numpy().copy()
#         shuffle_index = list(range(reorder_begin, reorder_begin + num_reorder))
#         random.shuffle(shuffle_index)
#         reordered_item_seq[reorder_begin:reorder_begin + num_reorder] = reordered_item_seq[shuffle_index]
#         return torch.tensor(reordered_item_seq, dtype=torch.long, device=item_seq.device), item_seq_len
#
#     def forward(self, item_seq, item_seq_len):
#         position_ids = torch.arange(item_seq.size(1), dtype=torch.long, device=item_seq.device)
#         position_ids = position_ids.unsqueeze(0).expand_as(item_seq)
#         position_embedding = self.position_embedding(position_ids)
#
#         item_emb = self.item_embedding(item_seq)
#         input_emb = item_emb + position_embedding
#         input_emb = self.LayerNorm(input_emb)
#         input_emb = self.dropout(input_emb)
#
#         extended_attention_mask = self.get_attention_mask(item_seq)
#
#         trm_output = self.trm_encoder(input_emb, extended_attention_mask, output_all_encoded_layers=True)
#         output = trm_output[-1]
#         # output = self.gather_indexes(output, item_seq_len - 1)
#         return output  # [B H]
#
#     def calculate_loss(self, item_seqs, item_seq_len, pos_seqs, neg_seqs):
#         item_seq = item_seqs
#         item_seq_len = item_seq_len
#         seq_output = self.forward(item_seq, item_seq_len)
#         if self.recbole_loss == 'bpr':
#             pos_items_emb = self.item_embedding(pos_seqs)
#             neg_items_emb = self.item_embedding(neg_seqs)
#             pos_temp_score = (seq_output * pos_items_emb).sum(dim=-1) # [B S]
#             pos_score = torch.sum(pos_temp_score, dim=-1)  # [B]
#             neg_temp_score = (seq_output * neg_items_emb).sum(dim=-1) # [B S]
#             neg_score = torch.sum(neg_temp_score, dim=-1)  # [B]
#             loss = -torch.log(1e-10 + torch.sigmoid(pos_score - neg_score)).mean()
#         elif self.recbole_loss == 'ce':
#             logits = torch.matmul(seq_output, self.item_embedding.weight.transpose(-2, -1))
#             loss = self.loss_fct(logits.transpose(-2, -1), pos_seqs)
#
#         # NCE
#         aug_item_seq1, aug_len1, aug_item_seq2, aug_len2 = self.augment(item_seq, item_seq_len)
#         seq_output1 = self.forward(aug_item_seq1, aug_len1)
#         seq_output1 = self.gather_indexes(seq_output1, item_seq_len - 1)
#         seq_output2 = self.forward(aug_item_seq2, aug_len2)
#         seq_output2 = self.gather_indexes(seq_output2, item_seq_len - 1)
#
#         nce_logits, nce_labels = self.info_nce(seq_output1, seq_output2, temp=self.tau, batch_size=aug_len1.shape[0], sim=self.sim)
#
#         # with torch.no_grad():
#         #     alignment, uniformity = self.decompose(seq_output1, seq_output2, seq_output, batch_size=item_seq_len.shape[0])
#
#         nce_loss = self.nce_fct(nce_logits, nce_labels)
#         if self.recbole_only_main:
#             return loss
#         return loss + self.lmd * nce_loss # + alignment + uniformity
#
#     def decompose(self, z_i, z_j, origin_z, batch_size):
#         """
#         We do not sample negative examples explicitly.
#         Instead, given a positive pair, similar to (Chen et al., 2017), we treat the other 2(N − 1) augmented examples within a minibatch as negative examples.
#         """
#         N = 2 * batch_size
#
#         z = torch.cat((z_i, z_j), dim=0)
#
#         # pairwise l2 distace
#         sim = torch.cdist(z, z, p=2)
#
#         sim_i_j = torch.diag(sim, batch_size)
#         sim_j_i = torch.diag(sim, -batch_size)
#
#         positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0).reshape(N, 1)
#         alignment = positive_samples.mean()
#
#         # pairwise l2 distace
#         sim = torch.cdist(origin_z, origin_z, p=2)
#         mask = torch.ones((batch_size, batch_size), dtype=bool)
#         mask = mask.fill_diagonal_(0)
#         negative_samples = sim[mask].reshape(batch_size, -1)
#         uniformity = torch.log(torch.exp(-2 * negative_samples).mean())
#
#         return alignment, uniformity
#
#     def mask_correlated_samples(self, batch_size):
#         N = 2 * batch_size
#         mask = torch.ones((N, N), dtype=bool)
#         mask = mask.fill_diagonal_(0)
#         for i in range(batch_size):
#             mask[i, batch_size + i] = 0
#             mask[batch_size + i, i] = 0
#         return mask
#
#     def info_nce(self, z_i, z_j, temp, batch_size, sim='dot'):
#         """
#         We do not sample negative examples explicitly.
#         Instead, given a positive pair, similar to (Chen et al., 2017), we treat the other 2(N − 1) augmented examples within a minibatch as negative examples.
#         """
#         N = 2 * batch_size
#
#         z = torch.cat((z_i, z_j), dim=0)
#
#         if sim == 'cos':
#             sim = nn.functional.cosine_similarity(z.unsqueeze(1), z.unsqueeze(0), dim=2) / temp
#         elif sim == 'dot':
#             sim = torch.mm(z, z.T) / temp
#
#         sim_i_j = torch.diag(sim, batch_size)
#         sim_j_i = torch.diag(sim, -batch_size)
#
#         positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0).reshape(N, 1)
#         if batch_size != self.batch_size:
#             mask = self.mask_correlated_samples(batch_size)
#         else:
#             mask = self.mask_default
#         negative_samples = sim[mask].reshape(N, -1)
#
#         labels = torch.zeros(N).to(positive_samples.device).long()
#         logits = torch.cat((positive_samples, negative_samples), dim=1)
#         return logits, labels
#
#     def predict(self, item_seq, item_seq_len, test_item):
#         temp_output = self.forward(item_seq, item_seq_len)
#         seq_output = self.gather_indexes(temp_output, item_seq_len - 1)
#         test_item_emb = self.item_embedding(test_item)
#         scores = test_item_emb.matmul(seq_output.unsqueeze(-1)).squeeze(-1)
#         # scores = torch.mul(seq_output, test_item_emb).sum(dim=1)  # [B]
#         return scores
#
#
# class DuoRec(torch.nn.Module):
#     def __init__(self, num_items, args):
#         super(DuoRec, self).__init__()
#
#         # load parameters info
#         self.max_seq_length = args.maxlen + 1
#         self.n_items = num_items
#         self.n_layers = args.num_blocks
#         self.n_heads = args.num_heads
#         self.hidden_size = args.hidden_units
#         self.inner_size = args.hidden_units * 4
#         self.hidden_dropout_prob = args.dropout_rate
#         self.attn_dropout_prob = args.dropout_rate
#         self.hidden_act = "gelu"
#         self.layer_norm_eps = 1e-8
#
#         self.batch_size = args.batch_size
#         self.lmd = 0.1
#         self.lmd_sem = 0.1
#         self.tau = 1
#         self.sim = 'dot'
#
#         # define layers and loss
#         self.item_embedding = torch.nn.Embedding(self.n_items + 1, self.hidden_size, padding_idx=0)
#         self.position_embedding = torch.nn.Embedding(self.max_seq_length, self.hidden_size)
#         self.trm_encoder = RecboleTransformerEncoder(
#             n_layers=self.n_layers,
#             n_heads=self.n_heads,
#             hidden_size=self.hidden_size,
#             inner_size=self.inner_size,
#             hidden_dropout_prob=self.hidden_dropout_prob,
#             attn_dropout_prob=self.attn_dropout_prob,
#             hidden_act=self.hidden_act,
#             layer_norm_eps=self.layer_norm_eps
#         )
#
#         self.LayerNorm = torch.nn.LayerNorm(self.hidden_size, eps=self.layer_norm_eps)
#         self.dropout = torch.nn.Dropout(self.hidden_dropout_prob)
#         self.mask_default = self.mask_correlated_samples(batch_size=self.batch_size)
#         self.aug_nce_fct = torch.nn.CrossEntropyLoss()
#         self.sem_aug_nce_fct = torch.nn.CrossEntropyLoss()
#         self.recbole_loss = args.recbole_loss
#         self.loss_fct = torch.nn.CrossEntropyLoss()
#         self.recbole_only_main = args.recbole_only_main
#
#     def gather_indexes(self, output, gather_index):
#         """Gathers the vectors at the specific positions over a minibatch"""
#         gather_index = gather_index.view(-1, 1, 1).expand(-1, -1, output.shape[-1])
#         output_tensor = output.gather(dim=1, index=gather_index)
#         return output_tensor.squeeze(1)
#
#     def get_attention_mask(self, item_seq):
#         """Generate left-to-right uni-directional attention mask for multi-head attention."""
#         attention_mask = (item_seq > 0).long()
#         extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
#         # mask for left-to-right unidirectional
#         max_len = attention_mask.size(-1)
#         attn_shape = (1, max_len, max_len)
#         subsequent_mask = torch.triu(torch.ones(attn_shape), diagonal=1)
#         subsequent_mask = (subsequent_mask == 0).unsqueeze(1)
#         subsequent_mask = subsequent_mask.long().to(item_seq.device)
#
#         extended_attention_mask = extended_attention_mask * subsequent_mask
#         extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)
#         extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
#         return extended_attention_mask
#
#     def get_bi_attention_mask(self, item_seq):
#         """Generate bidirectional attention mask for multi-head attention."""
#         attention_mask = (item_seq > 0).long()
#         extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
#         extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype)
#         extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
#         return extended_attention_mask
#
#     def forward(self, item_seq, item_seq_len):
#         position_ids = torch.arange(item_seq.size(1), dtype=torch.long, device=item_seq.device)
#         position_ids = position_ids.unsqueeze(0).expand_as(item_seq)
#         position_embedding = self.position_embedding(position_ids)
#
#         item_emb = self.item_embedding(item_seq)
#         input_emb = item_emb + position_embedding
#         input_emb = self.LayerNorm(input_emb)
#         input_emb = self.dropout(input_emb)
#
#         extended_attention_mask = self.get_attention_mask(item_seq)
#
#         trm_output = self.trm_encoder(input_emb, extended_attention_mask, output_all_encoded_layers=True)
#         output = trm_output[-1]
#         # output = self.gather_indexes(output, item_seq_len - 1)
#         return output
#
#     def calculate_loss(self, item_seqs, item_seq_len, pos_seqs, neg_seqs):
#         item_seq = item_seqs
#         item_seq_len = item_seq_len
#         seq_output = self.forward(item_seq, item_seq_len) # [B S H]
#         if self.recbole_loss == 'bpr':
#             pos_items_emb = self.item_embedding(pos_seqs)
#             neg_items_emb = self.item_embedding(neg_seqs)
#             pos_temp_score = (seq_output * pos_items_emb).sum(dim=-1) # [B S]
#             pos_score = torch.sum(pos_temp_score, dim=-1)  # [B]
#             neg_temp_score = (seq_output * neg_items_emb).sum(dim=-1) # [B S]
#             neg_score = torch.sum(neg_temp_score, dim=-1)  # [B]
#             loss = -torch.log(1e-10 + torch.sigmoid(pos_score - neg_score)).mean()
#         elif self.recbole_loss == 'ce':
#             logits = torch.matmul(seq_output, self.item_embedding.weight.transpose(-2, -1))
#             loss = self.loss_fct(logits.transpose(-2, -1), pos_seqs)
#
#         # Unsupervised NCE
#         seq_output = self.gather_indexes(seq_output, item_seq_len - 1)
#         aug_seq_output = self.forward(item_seq, item_seq_len)
#         aug_seq_output = self.gather_indexes(aug_seq_output, item_seq_len - 1)
#         nce_logits, nce_labels = self.info_nce(seq_output, aug_seq_output, temp=self.tau,
#                                                batch_size=item_seq_len.shape[0], sim=self.sim)
#         if self.recbole_only_main:
#             return loss
#         loss += self.lmd * self.aug_nce_fct(nce_logits, nce_labels)
#         return loss
#
#     def decompose(self, z_i, z_j, origin_z, batch_size):
#         """
#         We do not sample negative examples explicitly.
#         Instead, given a positive pair, similar to (Chen et al., 2017), we treat the other 2(N − 1) augmented examples within a minibatch as negative examples.
#         """
#         N = 2 * batch_size
#
#         z = torch.cat((z_i, z_j), dim=0)
#
#         # pairwise l2 distace
#         sim = torch.cdist(z, z, p=2)
#
#         sim_i_j = torch.diag(sim, batch_size)
#         sim_j_i = torch.diag(sim, -batch_size)
#
#         positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0).reshape(N, 1)
#         alignment = positive_samples.mean()
#
#         # pairwise l2 distace
#         sim = torch.cdist(origin_z, origin_z, p=2)
#         mask = torch.ones((batch_size, batch_size), dtype=bool)
#         mask = mask.fill_diagonal_(0)
#         negative_samples = sim[mask].reshape(batch_size, -1)
#         uniformity = torch.log(torch.exp(-2 * negative_samples).mean())
#
#         return alignment, uniformity
#
#     def mask_correlated_samples(self, batch_size):
#         N = 2 * batch_size
#         mask = torch.ones((N, N), dtype=bool)
#         mask = mask.fill_diagonal_(0)
#         for i in range(batch_size):
#             mask[i, batch_size + i] = 0
#             mask[batch_size + i, i] = 0
#         return mask
#
#     def info_nce(self, z_i, z_j, temp, batch_size, sim='dot'):
#         """
#         We do not sample negative examples explicitly.
#         Instead, given a positive pair, similar to (Chen et al., 2017), we treat the other 2(N − 1) augmented examples within a minibatch as negative examples.
#         """
#         N = 2 * batch_size
#
#         z = torch.cat((z_i, z_j), dim=0)
#
#         if sim == 'cos':
#             sim = nn.functional.cosine_similarity(z.unsqueeze(1), z.unsqueeze(0), dim=2) / temp
#         elif sim == 'dot':
#             sim = torch.mm(z, z.T) / temp
#
#         sim_i_j = torch.diag(sim, batch_size)
#         sim_j_i = torch.diag(sim, -batch_size)
#
#         positive_samples = torch.cat((sim_i_j, sim_j_i), dim=0).reshape(N, 1)
#         if batch_size != self.batch_size:
#             mask = self.mask_correlated_samples(batch_size)
#         else:
#             mask = self.mask_default
#         negative_samples = sim[mask].reshape(N, -1)
#
#         labels = torch.zeros(N).to(positive_samples.device).long()
#         logits = torch.cat((positive_samples, negative_samples), dim=1)
#         return logits, labels
#
#     def predict(self, item_seq, item_seq_len, test_item):
#         temp_output = self.forward(item_seq, item_seq_len)
#         seq_output = self.gather_indexes(temp_output, item_seq_len - 1)
#         test_item_emb = self.item_embedding(test_item)
#         scores = test_item_emb.matmul(seq_output.unsqueeze(-1)).squeeze(-1)
#         # scores = torch.mul(seq_output, test_item_emb).sum(dim=1)  # [B]
#         return scores
