import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import pandas as pd
import numpy as np
import math
import re
import random
from itertools import chain
from functools import partial, reduce
from collections import Counter
from nltk.tree import Tree
from nltk.tokenize import sent_tokenize
from torch.distributions.bernoulli import Bernoulli
import requests
from bs4 import BeautifulSoup
from time import sleep

def rand_projections(embedding_dim, num_samples=50, SD=1.):
    """This function generates `num_samples` random samples from the latent space's unit sphere.
        Args:
            embedding_dim (int): embedding dimensionality
            num_samples (int): number of random projection samples
        Return:
            torch.Tensor: tensor of size (num_samples, embedding_dim)
    """
    projections = [w / np.sqrt((w**2).sum())  # L2 normalization
                   for w in np.random.normal(0., SD, (num_samples, embedding_dim))]#size=(num_samples, embedding_dim))]
    projections = np.asarray(projections)
    return torch.from_numpy(projections).type(torch.FloatTensor)


def _sliced_wasserstein_distance(encoded_samples,
                                 distribution_samples,
                                 num_projections=50,
                                 p=2):
    """ Sliced Wasserstein Distance between encoded samples and drawn distribution samples.
        Args:
            encoded_samples (toch.Tensor): tensor of encoded training samples
            distribution_samples (torch.Tensor): tensor of drawn distribution training samples
            num_projections (int): number of projections to approximate sliced wasserstein distance
            p (int): power of distance metric
        Return:
            torch.Tensor: tensor of wasserstrain distances of size (num_projections, 1)
    """
    # derive latent space dimension size from random samples drawn from latent prior distribution
    embedding_dim = distribution_samples.size(1)
    # generate random projections in latent space
    projections = rand_projections(embedding_dim, num_projections)
    # calculate projections through the encoded samples
    encoded_projections = encoded_samples.matmul(projections.transpose(0, 1))
    # calculate projections through the prior distribution random samples
    distribution_projections = (distribution_samples.matmul(projections.transpose(0, 1)))
    # calculate the sliced wasserstein distance by
    # sorting the samples per random projection and
    # calculating the difference between the
    # encoded samples and drawn random samples
    # per random projection
    wasserstein_distance = (torch.sort(encoded_projections.transpose(0, 1), dim=1)[0] -
                            torch.sort(distribution_projections.transpose(0, 1), dim=1)[0])
    # distance between latent space prior and encoded distributions
    # power of 2 by default for Wasserstein-2
    wasserstein_distance = torch.pow(wasserstein_distance, p)
    # approximate mean wasserstein_distance for each projection
    return wasserstein_distance.mean()


def sliced_wasserstein_distance(encoded_samples,
                                num_projections=50,
                                p=2,
                                device='cpu',
                                SD=1.):
    """ Sliced Wasserstein Distance between encoded samples and drawn distribution samples.
        Args:
            encoded_samples (toch.Tensor): tensor of encoded training samples
            distribution_samples (torch.Tensor): tensor of drawn distribution training samples
            num_projections (int): number of projections to approximate sliced wasserstein distance
            p (int): power of distance metric
            device (torch.device): torch device (default 'cpu')
        Return:
            torch.Tensor: tensor of wasserstrain distances of size (num_projections, 1)
    """
    # draw random samples from latent space prior distribution
    z = torch.normal(0., SD, encoded_samples.shape) #randn(encoded_samples.shape)
    # approximate mean wasserstein_distance between encoded and prior distributions
    # for each random projection
    swd = _sliced_wasserstein_distance(encoded_samples, z,
                                       num_projections, p)
    return swd

def get_n_branches(x):
    if type(x) is int:
        return x * 2 - 2
    return len(x) * 2 - 2

def define_padded_vectors(leaves, pad):
    for leaf in leaves:
        for l in leaf:
            if ((l == 0.).long().float().mean() == 1.).item():
                l[pad] = 1.
    return leaves

def print_tree(x, transform=lambda x: x, attr='terminal'):
    nx = [None]
    def fx(x, nx):
        if x['left'] == {}:
            if attr is not None:
                nx[0] = transform(x[attr])
            else:
                nx[0] = transform(x)
        else:
            nx[0] = [None]
            nx += [[None]]
            fx(x['left'], nx[0])
            fx(x['right'], nx[1])
    fx(x, nx)
    nx = Tree.fromstring(str(nx).replace('(','{').replace(')','}').replace('[','(').replace(']',')').replace('),',')'))
    nx.pretty_print() #.replace('[','(').replace(']',')')

def pad(x, y, pad=None, force_continue=False):
    if type(x) in [list, tuple] and type(y) in [list, tuple]:
        if type(x) is tuple:
            x = list(x)
        if type(y) is tuple:
            y = list(y)
        assert len(x) == len(y)
        assert type(x[0]) in [list,torch.Tensor]
        if type(x[0][0]) not in [list, torch.Tensor]:
             pad = None
        else:
             pad = [None]
        for ix, (_x,_y) in enumerate(zip(x, y)):
            if len(_y) < len(_x):
                diff = len(_x) - len(_y)
                if type(_y) is torch.Tensor:
                    y[ix] = torch.cat([_y, torch.LongTensor([0]*diff)])
                else:
                    y[ix] = _y + ([pad] * diff)
            elif len(_x) < len(_y):
                diff = len(_y) - len(_x)
                if type(_x) is torch.Tensor:
                    #print(_x.shape, _y.shape)
                    p = torch.zeros((diff,_x.shape[1]))
                    x[ix] = torch.cat([_x, p])  #torch.FloatTensor([0]*diff)])
                else:
                    x[ix] = _x + ([pad] * diff)
            #print(type(x[0][0]), type(y[0][0]))
            if type(x[0][0]) not in [list, torch.Tensor] or type(y[0][0]) is int or force_continue:
                continue
            for _ix, (_x_, _y_) in enumerate(zip(x[ix], y[ix])):
                if len(_y_) < len(_x_):
                    diff = len(_x_) - len(_y_)
                    if type(_y_) is torch.Tensor:
                        y[ix][_ix] = torch.cat([_y_, torch.LongTensor([0]*diff)])
                    else:
                        y[ix][_ix] = _y_ + (pad * diff)
                elif len(_x_) < len(_y_):
                    diff = len(_y_) - len(_x_)
                    x[ix][_ix] = _x_ + (pad * diff)
    elif y.shape[0] < x.shape[0]:
        diff = x.shape[0] - y.shape[0]
        if len(y.shape) == 1:
            y = F.pad(y, (0, diff), value=PAD)
        elif len(y.shape) == 2:
            y = F.pad(y, (0, 0, 0, diff))
            if pad is not None:
                y[diff + 1:,pad] = 1.
        else:
            y = F.pad(y, (0, 0, 0, 0, 0, diff))
            if pad is not None:
                y[diff + 1:,:,pad] = 1.
    elif x.shape[0] < y.shape[0]:
        diff = y.shape[0] - x.shape[0]
        if len(x.shape) == 1:
            x = F.pad(x, (0, diff), value=PAD)
        elif len(x.shape) == 2:
            x = F.pad(x, (0, 0, 0, diff))
            if pad is not None:
                x[diff+1:,pad] = 1.
        else:
            x = F.pad(x, (0, 0, 0, 0, 0, diff))
            if pad is not None:
                x[diff+1:,:,pad] = 1.
    return x, y

def get_vocab(d, l=0, no_pad=False):
    if no_pad and l == 0:
        V = { '<pad>': 0 } # overriding this option for now
    elif l == 0:
        V = { '<pad>': 0 }
    else:
        V = { '<pad>': 0, '<unk>': 1 }
    lV = len(V)
    if type(d[0][0]) is list:
        vocab = chain(*chain(*d))
    else:
        vocab = chain(*d)
    V.update({w:(ix + lV) for ix,w in enumerate([w for w,f in Counter(vocab).items() if f > l])})
    rV = {ix:w for w,ix in V.items()}
    return V, rV

def vocab_encoder(v, x):
    if '<unk>' in v:
        if type(x[0]) is str:
            out = [v['<unk>'] if w not in v else v[w] for w in x]
            out = torch.LongTensor(out)
        else:
            out = [
                [v['<unk>'] if c not in v else v[c] for c in w]
                for w in x
            ]
            out = [torch.LongTensor(o) for o in out]
    else:
        if type(x[0]) is str:
            out = [v[w] for w in x]
            out = torch.LongTensor(out)
        else:
            out = [
                [v[c] for c in w]
                for w in x
            ]
            out = [torch.LongTensor(o) for o in out]
    return out

def shuffle_indices(ixs):
    random.shuffle(ixs)
    return ixs

def word_dropout(x, dropout=0.2, max_retries=5):
    if x.shape[0] < 3:
        dropout = 0.1
    retries = 0
    rw = Bernoulli(1. - dropout).sample(x.shape[:-1])

    while rw.sum().item() == 0. and retries < max_retries:
        rw = Bernoulli(1. - dropout).sample(x.shape[:-1])
        retries += 1
    if retries >= max_retries and rw.sum().item() == 0.:
        return x
    rw = rw.unsqueeze(2).repeat(1, 1, x.shape[-1])
    return rw * x

def split_terminal_leaves(x, cutoff1, cutoff2):
    a = F.gumbel_softmax(x[:, :, :cutoff1], hard=True)
    b = F.gumbel_softmax(x[:, :, cutoff1:cutoff1+cutoff2], hard=True)
    c = F.gumbel_softmax(x[:, :, cutoff1+cutoff2:], hard=True)
    return torch.cat([a, b, c], dim=-1)

def decode_leaf(rv, g, x, sz=100):
    return ''.join([
        rv[l] for l in 
        g.get_leaves(g(x, [sz], return_trees=True)[0]).softmax(-1).argmax(-1).tolist()
    ])

def weight_init(m):
    if hasattr(m, 'weight'):
        nn.init.xavier_normal_(m.weight)


def batch_indices(ixs, batch_size, n_batches):
    return [
        ixs[i*batch_size:i*batch_size+batch_size]
        for i in range(n_batches)
    ]

def expected_depth(x, mode='sum'):
    return torch.FloatTensor([
        (math.sqrt(s)) if mode != 'sum' else ((math.sqrt(s)) * (s)) # last sigmoid activation on a given branch should be < .5
        for s in x
    ])

def random_encodings(batch, embed, sd=1.):
    return torch.normal(0., sd, (batch, emb))

def inspect_parsed_sentence_helper(_x, aux, E, G, C, charE, selector1, ds, ix, CL=None, output_set=None, act=F.selu, sizes=None, selector2=None):
    if type(_x) is str:
        x = ds.vars[selector1]['encoder'](ds.vars[selector1]['preprocessor'](_x))
    else:
        x = ds.vars[selector1]['encoder'](_x)
    x = batch_data([x], 0, C.limit)
    if selector2 is not None:
        if aux is None:
            aux = ds.vars[selector2]['preprocessor'](_x)
        aux = [charE(w.unsqueeze(1)) for w in ds.vars[selector2]['encoder'](aux)]
        aux = batch_data([torch.cat(aux)], 0, C.limit, G.h)
    if sizes is None:
        sizes = [x.shape[0]]
    encoding = E(x, aux)
    tree = G(G.act(encoding.sum(0)), sizes=[C.limit], return_trees=True)[0]
    leaves = G.get_leaves(tree)
    leaves, _ = pad(define_padded_vectors(nn.utils.rnn.pad_sequence([leaves]), 0), torch.zeros((C.limit,1)))
    leaves = C(leaves, encoding, sizes)

    tree = attach_to_leaves(tree, leaves, ds.vars['text'], C.io, G, _x if type(_x) != str else ds.vars[selector1]['preprocessor'](_x))
    if CL != None and output_set != None:
        classification, attn_weights = CL(
            [G.get_states(tree).unsqueeze(1)],
            act(E.embed(output_set)).unsqueeze(1)
        )
        return (tree, attn_weights, classification)
    return (tree, None, None)

def inspect_parsed_sentence(s, ds, E, G, C, charE, ix, selector, aux=None, CL=None, output_set=None, selector2=None, sizes=None, print_s=False):
    tree, weights, classification = inspect_parsed_sentence_helper(s, aux, E, G, C, charE, selector, ds, ix, CL, output_set, selector2=selector2, sizes=sizes)
    #print(weights[0].shape)
    weights = weights[0].squeeze(0).transpose(0,1)
    if output_set is not None and CL is not None:
        weights = { ix:w for ix,w in enumerate(weights.argmax(-1).tolist()) }
        #print(weights)
        subs = { ix: sub for ix,sub in enumerate(G.get_leaves_from_subtrees(tree, 'attachment', False)) }
        V = ds.vars['text']['vocab']
        sents = {(len(V) - V[w] - 1): { 'sent': w } for w in ['negative','neutral','positive']}
        subtrees = {}
        for ix, ws in weights.items():
            subtrees[sents[ix]['sent']] = set([subs[w] if type(subs[w]) is str else ' '.join(subs[w]) for w in ws])
    print()
    if print_s:
        print(s if type(s) is str else ' '.join(s))
    print_tree(tree, lambda x: x, attr='attachment')
    if output_set is not None and CL is not None:
        sent = sents[classification.softmax(-1).argmax(-1).item()]['sent']
        print(sent, subtrees[sent])

def parse_batch(X, E, G, sizes, use_vocab=True):
    encodings = E(X)
    trees = G(encodings, sizes=[s.item() for s in sizes], return_trees=True)
    leaves, encodings, states, depths = zip(*[
        (G.get_leaves(t), t['state'], G.get_states(t).unsqueeze(1), G.get_leaves(t, attr='depth').sum(0,keepdim=True))
        for t in trees
    ])
    if use_vocab:
        vocab = [F.gumbel_softmax(l, hard=True).sum(0,keepdim=True) for l in leaves]
    else:
        vocab = None
    return leaves, encodings, states, vocab, depths

def get_ps_from_webpage(url='https://en.wikipedia.org/wiki/Special:Random', tokenize_sents=True):
    page = requests.get(url)
    soup = BeautifulSoup(page.content, 'html.parser')
    ps = soup.find_all('p')
    if tokenize_sents:
        return list(chain(*[sent_tokenize(p.get_text()) for p in ps]))
    return [p.get_text() for p in ps]

def batch_data(x, pad, limit, emb=1):
    if x[0].dim() == 1:
        pad_vec = [torch.zeros(limit).long()]
        x = nn.utils.rnn.pad_sequence(list(x) + pad_vec, padding_value=pad)[:,:-1]
    else:
        pad_vec = [torch.zeros((limit, emb))]
        x = define_padded_vectors(nn.utils.rnn.pad_sequence(list(x) + pad_vec), pad)[:,:-1,:]
    return x


def attach_to_leaves(tree, leaves, var, io, G, source):
    leaves = [
        w if w not in var['reverse_vocab'] else var['reverse_vocab'][w].replace('[','{').replace(']','}')
        for w in leaves.squeeze(1).softmax(-1).argmax(-1).tolist()
    ]
    for leaf_ix, leaf in enumerate(leaves):
        if type(leaf) != str:
            a = leaf - io
            if a < len(source):
                leaves[leaf_ix] = source[a]
            else:
                leaves[leaf_ix] = str(a)

    G.attach_to_leaves(tree, leaves)
    return tree

class Dataset:
    def __init__(self, config):
        self.config = config
        self.vars = {}
        self.sample_size = self.config['sample_size']
        self.unify = config.get('unify')
        self.flatten = config.get('flatten') is True
        self.reserve = config.get('reserve')
        anchor = self.config['anchor']
        self.anchor = anchor
        anchor_preprocessor = self.config['vars'][anchor]['preprocessor']
        limit = self.config['limit']
        df = self.config['df']
        self.len_filter = (lambda x: len(anchor_preprocessor(x)) <= limit)
        if type(df) is str and df == 'random_wiki':
            self.citation_filter = (lambda x: not re.search(r'^(\[\d+\])+$', x))
            text = []
            counter = 0
            while len(text) < self.sample_size:
                text += list(filter(lambda x: self.len_filter(x) and self.citation_filter(x) and len(anchor_preprocessor(x)) > 0, get_ps_from_webpage())) #self.citation_filter(x)
                if counter % 10 == 0:
                    print(f'{round(len(text)/self.sample_size, 4) * 100} % of required samples')
                counter += 1
                sleep(1 if 'sleep' not in config else config['sleep'])
            #text = text[:self.sample_size]
            df = pd.DataFrame({anchor: text})
        else:
            df = df[df[anchor].map(self.len_filter)].sample(self.sample_size).reset_index(drop=True)
        if self.unify or self.flatten:
            master = {}
            if self.flatten:
                data = list(chain(*[
                    w
                    for k,v in self.config['vars'].items()
                    for w in df[k].map(v['preprocessor']).tolist()
                ]))
            else:
                data = list(chain(*[
                    [
                        [w for w in s if not (k in self.reserve and w in self.reserve[k])]
                        for s in df[k].map(v['preprocessor']).tolist()
                    ]
                    for k,v in self.config['vars'].items()
                    if k in self.unify
                ]))
            master_vocab, reverse_master_vocab = get_vocab(
                pd.Series(data),
                self.config['vars'][anchor]['limit'],
                self.config['vars'][anchor]['pad']
            )
            master_encoder = partial(vocab_encoder, master_vocab)
            if self.flatten:
                self.vars[anchor] = {}
                self.vars[anchor]['text'] = data
                self.vars[anchor]['vocab'], self.vars[anchor]['reverse_vocab'] = (master_vocab, reverse_master_vocab)
                self.vars[anchor]['encoder'] = master_encoder
                self.vars[anchor]['vectors'] = list(map(self.vars[anchor]['encoder'], self.vars[anchor]['text']))
                self.vars[anchor]['preprocessor'] = anchor_preprocessor
                if 'extra_fxns' in self.config['vars'][anchor]:
                    self.vars[anchor]['extra_fxns'] = self.config['vars'][anchor]['extra_fxns']
                    for fxn_name, (ref, fxn) in self.config['vars'][anchor]['extra_fxns'].items():
                        self.vars[anchor][fxn_name] = list(map(partial(fxn, self.vars[anchor]), self.vars[anchor][ref]))
             
        for k,v in self.config['vars'].items():
            if self.flatten:
                break
            self.vars[k] = {}
            if k not in df.columns:
                data = df[v['source']].map(v['preprocessor'])
            else:
                data = df[k].map(v['preprocessor'])
            self.vars[k]['preprocessor'] = v['preprocessor']
            self.vars[k]['text'] = data
            if k in self.unify:
                self.vars[k]['vocab'], self.vars[k]['reverse_vocab'] = (master_vocab, reverse_master_vocab)
                self.vars[k]['encoder'] = master_encoder
            else:
                self.vars[k]['vocab'], self.vars[k]['reverse_vocab'] = get_vocab(data, v['limit'], v['pad'])
                self.vars[k]['encoder'] = partial(vocab_encoder, self.vars[k]['vocab'])
            self.vars[k]['vectors'] = list(map(self.vars[k]['encoder'], self.vars[k]['text']))
            if 'extra_fxns' in v:
                self.vars[k]['extra_fxns'] = v['extra_fxns']
                for fxn_name, (ref, fxn) in v['extra_fxns'].items():
                    self.vars[k][fxn_name] = list(map(partial(fxn, self.vars[k]), self.vars[k][ref]))

    def preprocess_new_observations(self, var, x):
        out = {}
        preprocessor = self.vars[var]['preprocessor']
        encoder = self.vars[var]['encoder']
        #extra_fxns = self.vars[var]['extra_fxns']
        if self.flatten:
            out['text'] = list(chain(*[[list(w) for w in preprocessor(s)] for s in x]))
        else:
            out['text'] = list(map(preprocessor, x))
        out['vectors'] = list(map(encoder, out['text']))
        if 'extra_fxns' in self.vars[var]:
            for fxn_name, (ref, fxn) in self.vars[var]['extra_fxns'].items():
                out[fxn_name] = list(map(partial(fxn, self.vars[var]), out[ref]))
        return out

    def get_random_wiki(self):
        text = []
        counter = 0
        while len(text) < self.sample_size:
            text += list(filter(lambda x: self.len_filter(x) and self.citation_filter(x), get_ps_from_webpage()))
            if counter % 10 == 0:
                    print(f'{round(len(text)/self.sample_size, 4) * 100} % of required samples')
            counter += 1
            sleep(1 if 'sleep' not in self.config else self.config['sleep'])
        df = pd.DataFrame({self.anchor: text})
        return self.preprocess_new_observations(self.anchor, df[self.anchor].tolist())       