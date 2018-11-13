from __future__ import division
import torch as tc
import torch.nn as nn
import torch.nn.functional as F

import wargs
from tools.utils import *
from models.nn_utils import MaskSoftmax, MyLogSoftmax

class Label_Smooth_NLLLoss(nn.Module):
    '''
    With label smoothing, KL-divergence between q_{smoothed ground truth prob.}(w)
    and p_{prob. computed by model}(w) is minimized.
    '''
    def __init__(self, label_smoothing, tgt_vocab_size, padding_idx=PAD):
        assert 0.0 < label_smoothing <= 1.0
        super(Label_Smooth_NLLLoss, self).__init__()
        smoothing_value = label_smoothing / (tgt_vocab_size - 2)
        one_hot = tc.full((tgt_vocab_size, ), smoothing_value)
        one_hot[padding_idx] = 0.
        self.register_buffer('one_hot', one_hot.unsqueeze(0))
        self.confidence = 1.0 - label_smoothing
        self.padding_idx = padding_idx

    def forward(self, output, target):
        '''
        output (FloatTensor): batch_size*max_seq_len, n_classes
        target (LongTensor): batch_size*max_seq_len
        '''
        model_prob = self.one_hot.repeat(target.size(0), 1)
        model_prob.scatter_(1, target.unsqueeze(1), self.confidence)
        model_prob.masked_fill_((target == self.padding_idx).unsqueeze(1), 0)

        return F.kl_div(output, model_prob, reduction='sum')
        #return F.kl_div(output, model_prob, size_average=False)

class Classifier(nn.Module):

    def __init__(self, input_size, output_size, trg_word_emb=None, label_smoothing=0.,
                 emb_loss=False, bow_loss=False):

        super(Classifier, self).__init__()
        if emb_loss is True:
            assert trg_word_emb is not None, 'embedding loss needs target embedding'
            self.trg_word_emb = trg_word_emb.we.weight
            #self.trg_word_emb = trg_word_emb.we
            self.euclidean_dist = nn.PairwiseDistance(p=2, eps=1e-06, keepdim=True)
        self.emb_loss = emb_loss
        if bow_loss is True:
            wlog('using the bag of words loss')
            self.sigmoid = nn.Sigmoid()
            #self.softmax = MaskSoftmax()
        self.bow_loss = bow_loss

        self.map_vocab = nn.Linear(input_size, output_size, bias=True)
        if trg_word_emb is not None:
            assert input_size == wargs.d_trg_emb
            wlog('copying weights of target word embedding into classifier')
            self.map_vocab.weight = trg_word_emb.we.weight
        self.log_prob = MyLogSoftmax(wargs.self_norm_alpha)

        assert 0. <= label_smoothing <= 1., 'label smoothing value should be in [0, 1]'
        wlog('NLL loss with label_smoothing: {}'.format(label_smoothing))
        if label_smoothing == 0.:
            # If label smoothing value is set to zero, the loss is equivalent to NLLLoss
            weight = tc.ones(output_size)
            weight[PAD] = 0   # do not predict padding, same with ingore_index
            self.criterion = nn.NLLLoss(weight, ignore_index=PAD, reduction='sum')
            #self.criterion = nn.NLLLoss(weight, ignore_index=PAD, size_average=False)
        elif label_smoothing > 0.:
            # All non-true labels are uniformly set to low-confidence.
            self.criterion = Label_Smooth_NLLLoss(label_smoothing, output_size)

        self.output_size = output_size
        self.softmax = MaskSoftmax()

    def pred_map(self, logit, noise=None):

        logit = self.map_vocab(logit)

        if noise is not None:
            logit.data.add_( -tc.log(-tc.log(tc.Tensor(
                logit.size()).cuda().uniform_(0, 1) + epsilon) + epsilon) ) / noise

        return logit

    def logit_to_prob(self, logit, gumbel=None, tao=None):

        # (L, B)
        d1, d2, _ = logit.size()
        logit = self.pred_map(logit)
        if gumbel is None:
            p = self.softmax(logit)
        else:
            #print 'logit ..............'
            #print tc.max((logit < 1e+10) == False)
            #print 'gumbel ..............'
            #print tc.max((gumbel < 1e+10) == False)
            #print 'aaa ..............'
            #aaa = (gumbel.add(logit)) / tao
            #print tc.max((aaa < 1e+10) == False)
            p = self.softmax((gumbel.add(logit)) / tao)
        p = p.view(d1, d2, self.output_size)

        return p

    def nll_loss(self, pred_2d, pred_3d, gold, gold_mask, bow=None, bow_mask=None, epo_idx=None):

        #print pred_2d.size(), pred_3d.size(), gold.size(), gold_mask.size(), bow.size(), bow_mask.size()
        batch_size, max_L, bow_L = pred_3d.size(0), pred_3d.size(1), bow.size(1)
        log_norm, prob, ll = self.log_prob(pred_2d)
        abs_logZ = (log_norm * gold_mask[:, None]).abs().sum()
        ll = ll * gold_mask[:, None]
        ce_loss = self.criterion(ll, gold)

        # embedding loss
        if self.emb_loss is True:
            V, E = self.trg_word_emb.size(0), self.trg_word_emb.size(1)
            #bow_emb = self.trg_word_emb
            bow_emb = self.trg_word_emb[bow]
            gold_emb = self.trg_word_emb[gold]
            #print prob.size()
            #print bow_emb.size()
            #print gold_emb.size()
            bow_emb = bow_emb * bow_mask[:, :, None]
            gold_emb = gold_emb * gold_mask[:, None]
            bow_emb = bow_emb[:,None,:,:].expand((-1, max_L, -1, -1)).contiguous().view(-1, E)
            gold_emb = gold_emb.reshape(batch_size, max_L, gold_emb.size(-1))[:,:,None,:].expand(
                (-1, -1, bow_L, -1)).contiguous().view(-1, E)
            dist = F.pairwise_distance(bow_emb, gold_emb, p=2, keepdim=True)
            dist = dist.reshape(batch_size, max_L, bow_L).sum(-1).view(-1)
            '''
            gold_emb = gold_emb.reshape(batch_size, max_L, gold_emb.size(-1))
            dist = tc.zeros(batch_size, max_L, requires_grad=True)
            if wargs.gpu_id: dist = dist.cuda()    # push into GPU
            for batch_idx in range(batch_size):
                for len_idx in range(max_L):
                    one_gold_emb = gold_emb[batch_idx, len_idx][None, :].expand((V, -1))
                    #one_dist = F.pairwise_distance(bow_emb, one_gold_emb, p=2, keepdim=True)
                    one_dist = self.euclidean_dist(bow_emb, one_gold_emb)
                    dist[batch_idx, len_idx] = one_dist.sum()
            '''

            if gold_mask is not None: dist = dist.view(-1) * gold_mask
            #print 'dist ', dist[:, None].size()
            #print dist
            pred_p_t = tc.gather(prob, dim=1, index=gold[:, None])
            #print 'pred_p_t ',  pred_p_t.size()
            #print pred_p_t
            if gold_mask is not None: pred_p_t = pred_p_t * gold_mask[:, None]
            #print 'pred_p_t ',  pred_p_t.size()
            #print pred_p_t
            loss = ce_loss + ( pred_p_t * dist[:, None] ).sum()
            #loss = ( loss_emb.view(-1) * gold_mask ).sum()
            #print loss
        elif self.bow_loss is True:
            gold_mask_3d = gold_mask.reshape(batch_size, max_L)[:,:,None]
            bow_prob = self.sigmoid((pred_3d * gold_mask_3d).sum(1))
            #bow_prob = self.softmax((pred_3d * gold_mask_3d).sum(1), gold_mask_3d)
            assert epo_idx is not None
            epo_idx = int(epo_idx[0, 0])
            bow_ll = tc.log(bow_prob + 1e-20)[:, None, :].expand(
                -1, bow_L, -1).contiguous().view(-1, bow_prob.size(-1))
            bow_ll = bow_ll * bow_mask.view(-1)[:, None]
            lambd = schedule_bow_lambda(epo_idx)
            loss = ( ce_loss / gold_mask.sum().item() ) + \
                    lambd * ( self.criterion(bow_ll, bow.view(-1)) / bow_mask.sum().item() )
        else:
            loss = ce_loss

        return loss, ce_loss, abs_logZ

    def forward(self, feed, gold=None, gold_mask=None, noise=None,
                bow=None, bow_mask=None, epo=None):

        # (batch_size, max_tlen_batch - 1, out_size)
        pred = self.pred_map(feed, noise)
        # decoding, if gold is None and gold_mask is None:
        if gold is None: return -self.log_prob(pred)[-1] if wargs.self_norm_alpha is None else -pred

        pred_vocab_3d = pred
        assert pred_vocab_3d.dim() == 3, 'error'
        if pred_vocab_3d.dim() == 3: pred = pred_vocab_3d.view(-1, pred_vocab_3d.size(-1))

        if gold.dim() == 2: gold, gold_mask = gold.view(-1), gold_mask.view(-1)
        # negative likelihood log
        loss, ce_loss, abs_logZ = self.nll_loss(pred, pred_vocab_3d, gold, gold_mask, bow, bow_mask, epo)

        # (max_tlen_batch - 1, batch_size, trg_vocab_size)
        ok_ytoks = (pred.max(dim=-1)[1]).eq(gold).masked_select(gold.ne(PAD)).sum()

        # total loss,  ok prediction count in one minibatch
        return loss, ce_loss, ok_ytoks, abs_logZ

    '''
    Compute the loss in shards for efficiency
        outputs: the predict outputs from the model
        gold: correct target sentences in current batch
    '''
    def snip_back_prop(self, outputs, gold, gold_mask, bow, bow_mask,
                       epo, shard_size=100, norm='sents'):

        # (batch_size, max_tlen_batch - 1, out_size)
        batch_nll, batch_ok_ytoks, batch_abs_logZ = 0, 0, 0
        epo = tc.ones_like(gold, requires_grad=False) * epo
        normalization = gold_mask.sum().item() if norm == 'tokens' else outputs.size(0)
        shard_state = { 'feed': outputs, 'gold': gold, 'gold_mask': gold_mask, 'bow': bow,
                       'bow_mask':bow_mask, 'epo': epo }

        for shard in shards(shard_state, shard_size):
            loss, nll, ok_ytoks, abs_logZ = self(**shard)
            batch_nll += nll.item()
            batch_ok_ytoks += ok_ytoks.item()
            batch_abs_logZ += abs_logZ.item()
            loss.div(float(normalization)).backward(retain_graph=True)
            #loss.backward(retain_graph=True)

        return batch_nll, batch_ok_ytoks, batch_abs_logZ

def filter_shard_state(state):
    for k, v in state.items():
        if v is not None:
            if isinstance(v, tc.Tensor) and v.requires_grad:
                with tc.enable_grad(): v = tc.tensor(v.data, requires_grad=True)
            yield k, v

def shards(state, shard_size, eval=False):
    '''
    Args:
        state: A dictionary which corresponds to the output of
               *LossCompute.make_shard_state(). The values for
               those keys are Tensor-like or None.
        shard_size: The maximum size of the shards yielded by the model.
        eval: If True, only yield the state, nothing else.
              Otherwise, yield shards.
    Yields:
        Each yielded shard is a dict.
    Side effect:
        After the last shard, this function does back-propagation.
    '''
    if eval:
        yield state
    else:
        # non_none: the subdict of the state dictionary where the values are not None.
        non_none = dict(filter_shard_state(state))

        # Now, the iteration: state is a dictionary of sequences of tensor-like but we
        # want a sequence of dictionaries of tensors. First, unzip the dictionary into
        # a sequence of keys and a sequence of tensor-like sequences.
        keys, values = zip(*((k, tc.split(v, shard_size)) for k, v in non_none.items()))

        # Now, yield a dictionary for each shard. The keys are always
        # the same. values is a sequence of length #keys where each
        # element is a sequence of length #shards. We want to iterate
        # over the shards, not over the keys: therefore, the values need
        # to be re-zipped by shard and then each shard can be paired with the keys.
        for shard_tensors in zip(*values):
            # each slice: return (('feed', 'gold', ...), (feed0, gold0, ...))
            yield dict(zip(keys, shard_tensors))

        '''
        for k, v in non_none.items():
            print '-------------------------'
            print type(k)
            print k
            print isinstance(v, tc.Tensor)
            print v.size()
            print v.grad
        '''
        # Assumed backprop'd
        variables = ((state[k], v.grad.data) for k, v in non_none.items()
                     if isinstance(v, tc.Tensor) and v.grad is not None)
        inputs, grads = zip(*variables)
        tc.autograd.backward(inputs, grads)


