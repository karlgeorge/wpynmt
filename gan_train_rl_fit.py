import wargs
import const
import torch as tc
import math
from translate import Translator
from utils import *
from torch.autograd import Variable
from train import memory_efficient
from optimizer import Optim

from bleu import *
from train import mt_eval

def _to_Var(t):

    if isinstance(t, list):
        t = tc.Tensor(t)
    if isinstance(t, tc.Tensor):
        t = Variable(t, requires_grad=False)
    if wargs.gpu_id: t = t.cuda()
    return t

class Trainer:

    # src: (max_slen_batch, batch_size, emb)
    # gold: (max_tlen_batch, batch_size, emb)
    def __init__(self, nmtModel, sv, tv, optim, trg_dict_size, n_critic=1):
        self.nmtModel = nmtModel
        self.sv = sv
        self.tv = tv
        self.optim = optim
        self.trg_dict_size = trg_dict_size

        self.n_critic = 1#n_critic

        self.translator_sample = Translator(nmtModel, sv, tv, k=10, noise=False)
        self.translator = Translator(nmtModel, sv, tv, k=10)

        self.optim_G = Optim(
            'adadelta', 1.0, wargs.max_grad_norm,
            learning_rate_decay=wargs.learning_rate_decay,
            start_decay_from=wargs.start_decay_from,
            last_valid_bleu=wargs.last_valid_bleu
        )

        self.optim_RL = Optim(
            'adadelta', 1.0, wargs.max_grad_norm,
            learning_rate_decay=wargs.learning_rate_decay,
            start_decay_from=wargs.start_decay_from,
            last_valid_bleu=wargs.last_valid_bleu
        )

        self.tao = 1
        self.softmax = tc.nn.Softmax()
        self.lamda = 5
        self.eps = 1e-20

        #self.optim.init_optimizer(self.nmtModel.parameters())
        #self.optim_G.init_optimizer(self.nmtModel.parameters())
        #self.optim_RL.init_optimizer(self.nmtModel.parameters())

    # p1: (max_tlen_batch, batch_size, vocab_size)
    def distance(self, p1, p2, y_masks, type='JS', y_gold=None):

        if p2.size(0) > p1.size(0):

            p2 = p2[:(p1.size(0) + 1)]

        if type == 'JS':

            #D_kl = tc.mean(tc.sum((tc.log(p1) - tc.log(p2)) * p1, dim=-1).squeeze(), dim=0)
            M = (p1 + p2) / 2.
            D_kl1 = tc.sum((tc.log(p1) - tc.log(M)) * p1, dim=-1).squeeze()
            D_kl2 = tc.sum((tc.log(p2) - tc.log(M)) * p2, dim=-1).squeeze()
            JS = 0.5 * D_kl1 + 0.5 * D_kl2

            dist = tc.sum(JS * y_masks)
            del JS

        elif type == 'KL':

            KL = tc.sum((tc.log(p1 + self.eps) - tc.log(p2 + self.eps)) * p1, dim=-1).squeeze()
            # (L, B)
            dist = tc.sum(KL * y_masks)

            W_KL = KL / y_masks.sum(0).expand_as(KL)
            #print W_KL.data
            W_dist = tc.sum(W_KL * y_masks)
            #print W_dist.data[0], y_masks.size(1)

        elif type == 'KL-sent':

            #print p1[0]
            #print p2[0]
            #print '-----------------------------'
            p1 = tc.gather(p1, 2, y_gold[:, :, None])[:, :, 0]
            p2 = tc.gather(p2, 2, y_gold[:, :, None])[:, :, 0]
            # p1 (max_tlen_batch, batch_size)
            #print (p2 < 1) == False

            dist = tc.sum((y_masks * tc.log(p1) - y_masks * tc.log(p2)) * p1).squeeze()
            # KL: (1, batch_size)

        return dist / y_masks.size(1), W_dist / y_masks.size(1)

    def save_model(self, eid, bid):

        model_state_dict = self.nmtModel.state_dict()
        model_state_dict = {k: v for k, v in model_state_dict.items() if 'classifier' not in k}
        class_state_dict = self.nmtModel.classifier.state_dict()
        model_dict = {
            'model': model_state_dict,
            'class': class_state_dict,
            'epoch': eid,
            'batch': bid,
            'optim': self.optim
        }

        if wargs.save_one_model:
            model_file = '{}.pt'.format(wargs.model_prefix)
        else:
            model_file = '{}_e{}_upd{}.pt'.format(wargs.model_prefix, eid, bid)
        tc.save(model_dict, model_file)

    def hyps_padding_dist(self, B, hyps_L, y_maxL, p_y_hyp):

        hyps_dist = [None] * B
        for bid in range(B):

            hyp_L = hyps_L[bid]
            one_p_y_hyp = p_y_hyp[:, bid, :]

            if hyp_L < y_maxL:
                pad = tc.ones(y_maxL - hyp_L) / self.trg_dict_size
                pad = pad[:, None].expand((pad.size(0), one_p_y_hyp.size(-1)))
                if wargs.gpu_id and not pad.is_cuda: pad = pad.cuda()
                #print one_p_y_hyp.size(0), pad.size(0)
                one_p_y_hyp.data[hyp_L:] = pad

            hyps_dist[bid] = one_p_y_hyp

        hyps_dist = tc.stack(tuple(hyps_dist), dim=1)

        return hyps_dist

    def gumbel_sampling(self, B, y_maxL, output):

        if output.is_cuda: output = output.cpu()
        # output (L * B, V)
        if output.dim() == 3: output = output.view(-1, output.size(-1))
        g = get_gumbel(output.size(0), self.trg_dict_size)
        hyps = tc.max(g + output, 1)[1]
        # hyps (L*B, 1)
        hyps = hyps.view(y_maxL, B)
        hyps[0] = const.BOS * tc.ones(B).long()   # first words are <s>
        # hyps (L, B)
        c1 = tc.clamp((hyps.data - const.EOS), min=0, max=self.trg_dict_size)
        c2 = tc.clamp((const.EOS - hyps.data), min=0, max=self.trg_dict_size)
        _hyps = c1 + c2
        _hyps = tc.cat([_hyps, tc.zeros(B).long().unsqueeze(0)], 0)
        _hyps = tc.min(_hyps, 0)[1]
        #_hyps = tc.max(0 - _hyps, 0)[1]
        # idx: (1, B)
        hyps_L = _hyps.view(-1).tolist()
        hyps_mask = tc.zeros(y_maxL, B)
        for bid in range(B): hyps_mask[:, bid][:hyps_L[bid]] = 1.
        hyps_mask = Variable(hyps_mask, requires_grad=False)

        if wargs.gpu_id and not hyps_mask.is_cuda: hyps_mask = hyps_mask.cuda()
        if wargs.gpu_id and not hyps.is_cuda: hyps = hyps.cuda()
        if wargs.gpu_id and not g.is_cuda: g = g.cuda()

        return g, hyps, hyps_mask, hyps_L

    def try_trans(self, srcs, ref):

        # (len, 1)
        #src = sent_filter(list(srcs[:, bid].data))
        x_filter = sent_filter(list(srcs))
        y_filter = sent_filter(list(ref))
        #wlog('\n[{:3}] {}'.format('Src', idx2sent(x_filter, self.sv)))
        #wlog('[{:3}] {}'.format('Ref', idx2sent(y_filter, self.tv)))

        onebest, onebest_ids, _ = self.translator_sample.trans_onesent(x_filter)

        #wlog('[{:3}] {}'.format('Out', onebest))

        # no EOS and BOS
        return onebest_ids


    def beamsearch_sampling(self, srcs, x_masks, ref, y_maxL):

        # y_masks: (trg_max_len, batch_size)
        B = srcs.size(1)
        hyps, hyps_L = [None] * B, [None] * B
        for bid in range(B):

            onebest_ids = self.try_trans(srcs[:, bid].data, ref[:, bid].data)

            if len(onebest_ids) == 0 or onebest_ids[0] != const.BOS:
                onebest_ids = [const.BOS] + onebest_ids
            if onebest_ids[-1] == const.EOS: onebest_ids = onebest_ids[:-1]

            hyp_L = len(onebest_ids)
            hyps_L[bid] = hyp_L

            onebest_ids = tc.Tensor(onebest_ids).long()

            if hyp_L < y_maxL:
                hyps[bid] = tc.cat(
                    tuple([onebest_ids, const.PAD * tc.ones(y_maxL - hyps_L[bid]).long()]), 0)
            else:
                hyps[bid] = onebest_ids[:y_maxL]

        hyps = tc.stack(tuple(hyps), dim=1)

        if wargs.gpu_id and not hyps.is_cuda: hyps = hyps.cuda()
        hyps = Variable(hyps, requires_grad=False)
        hyps_mask = hyps.ne(const.PAD).float()

        return hyps, hyps_mask, hyps_L

    def train(self, dh, train_data, k, valid_data=None, tests_data=None,
              merge=False, name='default', percentage=0.1):

        if k + 1 % 10 == 0 and valid_data and tests_data:
            wlog('Evaluation on dev ... ')
            mt_eval(valid_data, self.nmtModel, self.sv, self.tv,
                    0, 0, [self.optim, self.optim_RL, self.optim_G], tests_data)

        loss_val = 0.
        batch_count = len(train_data)
        self.nmtModel.train()

        self.optim.init_optimizer(self.nmtModel.parameters()) 
        self.optim_G.init_optimizer(self.nmtModel.parameters())
        self.optim_RL.init_optimizer(self.nmtModel.parameters())

        for eid in range(wargs.start_epoch, wargs.max_epochs + 1):

            #self.optim.init_optimizer(self.nmtModel.parameters())
            #self.optim_G.init_optimizer(self.nmtModel.parameters())
            #self.optim_RL.init_optimizer(self.nmtModel.parameters())

            size = int(percentage * batch_count)
            shuffled_batch_idx = tc.randperm(batch_count)
            test = [train_data[shuffled_batch_idx[k]] for k in range(size)]

            wlog('{}, Epo:{:>2}/{:>2} start, random {}/{}({:.2%}) calc BLEU ... '.format(
                name, eid, wargs.max_epochs, size, batch_count, percentage))
            param_1, param_2 = [], []
            for k in range(size):
                #_, srcs, trgs, _, srcs_m, trgs_m = train_data[shuffled_batch_idx[k]]
                bid = shuffled_batch_idx[k]
                if merge is False:
                    _, srcs, trgs, slens, srcs_m, trgs_m = train_data[bid]
                else:
                    _, srcs, trgs, slens, srcs_m, trgs_m = dh.merge_batch(train_data[bid])[0]

                hyps, hyps_mask, hyps_L = self.beamsearch_sampling(srcs, srcs_m, trgs, 100)

                param_1.append(LBtensor_to_Str(hyps[1:].cpu(), [l-1 for l in hyps_L]))
                param_2.append(LBtensor_to_Str(trgs[1:-1].cpu(),
                                               trgs_m[1:-1].cpu().data.numpy().sum(0).tolist()))

            start_bat_bleu = bleu('\n'.join(param_1), ['\n'.join(param_2)])
            wlog('BLEU on random training data: {}'.format(start_bat_bleu))
            if start_bat_bleu > 0.9:
                wlog('Better BLEU ... go to next data history ...')
                return

            for bid in range(batch_count):
                #self.optim.init_optimizer(self.nmtModel.parameters())
                #self.optim_G.init_optimizer(self.nmtModel.parameters())
                #self.optim_RL.init_optimizer(self.nmtModel.parameters())
                if merge is False:
                    _, srcs, trgs, slens, srcs_m, trgs_m = train_data[bid]
                else:
                    _, srcs, trgs, slens, srcs_m, trgs_m = dh.merge_batch(train_data[bid])[0]
                gold_feed, gold_feed_mask = trgs[:-1], trgs_m[:-1]
                B, y_maxL = srcs.size(1), gold_feed.size(0)
                N = trgs[1:].data.ne(const.PAD).sum()
                #print B, y_maxL

                trgs_list = LBtensor_to_StrList(gold_feed.cpu(), gold_feed_mask.cpu().data.numpy().sum(0).tolist())

                debug('Train Discrimitor .......... {}'.format(name))
                for j in range(1):#self.n_critic):

                    #self.nmtModel.zero_grad()
                    self.optim.zero_grad()

                    o1 = self.nmtModel(srcs, gold_feed, srcs_m, gold_feed_mask)
                    p_y_gold = self.nmtModel.classifier.logit_to_prob(o1)
                    # p_y_gold: (gold_max_len - 1, B, trg_dict_size)

                    #logit = self.nmtModel.classifier.get_a(o1)
                    #g, hyps, hyps_mask, hyps_L = self.gumbel_sampling(B, y_maxL, logit)
                    hyps, hyps_mask, hyps_L = self.beamsearch_sampling(srcs, srcs_m, trgs, y_maxL)

                    #print hyps
                    o_hyps = self.nmtModel(srcs, hyps, srcs_m, hyps_mask)
                    #print o_hyps
                    #p_y_hyp = self.nmtModel.classifier.logit_to_prob(o_hyps, g, self.tao)
                    p_y_hyp = self.nmtModel.classifier.logit_to_prob(o_hyps)
                    #print p_y_hyp
                    p_y_hyp = self.hyps_padding_dist(B, hyps_L, y_maxL, p_y_hyp)
                    #print 'aaaaaaaaaaaaaaaaaaaaaaaaa'
                    #print p_y_gold.size()
                    #print p_y_hyp.size()
                    #print hyps_mask.size()
                    #loss_D = -self.distance(p_y_gold, p_y_hyp, hyps_mask, type='KL')
                    loss_D, w_loss_D = self.distance(p_y_gold, p_y_hyp, hyps_mask, type='KL', y_gold=trgs[1:])

                    #loss_D.div(B).backward(retain_variables=True)
                    (1 * loss_D).div(B).backward()
                    self.optim.step()
                    debug('Discrimitor KL distance {}'.format(w_loss_D.data[0]))
                    del hyps, hyps_mask, o_hyps, p_y_hyp

                for i in range(4):

                    #self.nmtModel.zero_grad()
                    self.optim_G.zero_grad()

                    outputs = self.nmtModel(srcs, gold_feed, srcs_m, gold_feed_mask)
                    #print 'feed gold outputs ................'
                    #print outputs.data
                    batch_loss, grad_output, batch_correct_num = memory_efficient(
                        outputs, trgs[1:], trgs_m[1:], self.nmtModel.classifier)
                    #print batch_loss
                    #print grad_output
                    outputs.backward(grad_output)

                    #loss, correct_num = self.nmtModel.classifier(o1, trgs[1:], trgs_m[1:])
                    debug('Epo:{:>2}/{:>2}, Bat:[{}/{}], W-MLE:{:4.2f}, W-ppl:{:4.2f}, '
                         'S-MLE:{:4.2f}'.format(eid, wargs.max_epochs, bid, batch_count,
                                                batch_loss/N, math.exp(batch_loss/N), batch_loss/B))

                    #loss.div(batch_size).backward()
                    #(loss_G + loss).div(batch_size).backward()

                    self.optim_G.step()
                    #del loss, correct_num
                    #del o1, p_y_gold, p_y_hyp2, hyps_mask
                    del outputs, batch_correct_num

                debug('RL -> Gap of MLE and BLEU ... rho ... feed onebest .... ')
                for i in range(1):
                    if bid == batch_count - 1:
                        wlog('Ship rl operation')
                        continue
                    #self.nmtModel.zero_grad()
                    self.optim_RL.zero_grad()

                    #g, hyps, hyps_mask, hyps_L = self.gumbel_sampling(B, y_maxL, logit)
                    hyps, hyps_mask, hyps_L = self.beamsearch_sampling(srcs, srcs_m, trgs, y_maxL)

                    hyps_list = LBtensor_to_StrList(hyps.cpu(), hyps_L)
                    bleus = []
                    for hyp, ref in zip(hyps_list, trgs_list):
                        bleus.append(bleu(hyp, [ref]))

                    param_1, param_2 = [], []
                    for k in range(B):
                        param_1.append(LBtensor_to_Str(hyps[1:].cpu(), [l-1 for l in hyps_L]))
                        param_2.append(LBtensor_to_Str(trgs[1:-1].cpu(),
                                                       trgs_m[1:-1].cpu().data.numpy().sum(0).tolist()))
                    rl_bat_bleu = bleu('\n'.join(param_1), ['\n'.join(param_2)])

                    #print hyps
                    o_hyps = self.nmtModel(srcs, hyps, srcs_m, hyps_mask)
                    #print o_hyps
                    #p_y_hyp = self.nmtModel.classifier.logit_to_prob(o_hyps, g, self.tao)
                    p_y_hyp = self.nmtModel.classifier.logit_to_prob(o_hyps)
                    #print p_y_hyp
                    p_y_hyp = self.hyps_padding_dist(B, hyps_L, y_maxL, p_y_hyp)
                    #p_y_hyp = tc.gather(p_y_hyp, 2, hyps.unsqueeze(2).expand_as(p_y_hyp))[:, :, 0]
                    #print 'e',p_y_hyp.data
                    #print 'sdafsd'
                    p_y_hyp = tc.diag(p_y_hyp.view(-1, p_y_hyp.size(-1)).index_select(
                        1, hyps.view(-1))).view(y_maxL, B)
                    #log_p_y_hyp = tc.sum(tc.log(p_y_hyp) * hyps_mask, 0)
                    #print 'log ppppp.....................'
                    #print tc.log(p_y_hyp).data
                    #print 'mask...........j'
                    #print 'c',(tc.log(p_y_hyp ) * hyps_mask).data
                    #print 'bb',tc.sum(tc.log(p_y_hyp) * hyps_mask , 0)
                    p_y_hyp = ((p_y_hyp + self.eps).log() * hyps_mask).sum(0) / hyps_mask.sum(0)
                    p_y_hyp = p_y_hyp[None, :]

                    #p_y_hyp = tc.sum(p_y_hyp * hyps_mask , 0) / hyps_mask.sum(0)
                    #print 'b',p_y_hyp.data
                    bleus = _to_Var(bleus)

                    rl_avg_bleu = tc.mean(bleus).data[0]
                    bleus = self.softmax(self.lamda * bleus.unsqueeze(0))

                    #p_y_hyp = (p_y_hyp * self.lamda / 3).exp()
                    E_a, E_b = tc.mean(p_y_hyp), tc.mean(bleus)
                    E_a_2, E_b_2 = tc.mean(p_y_hyp * p_y_hyp), tc.mean(bleus * bleus)
                    rl_rho = tc.mean(p_y_hyp * bleus) - E_a * E_b
                    #print 'a',rl_rho.data[0]
                    D_a, D_b = E_a_2 - E_a * E_a, E_b_2 - E_b * E_b
                    rl_rho = rl_rho / tc.sqrt(D_a * D_b) + self.eps

                    p_y_hyp_T = p_y_hyp.t().expand(B, B)
                    p_y_hyp = p_y_hyp.expand(B, B)

                    bleus_T = bleus.t().expand(B, B)
                    bleus = bleus.expand(B, B)

                    bleus_sum = bleus_T + bleus + self.eps
                    p_y_hyp_sum = p_y_hyp_T + p_y_hyp - self.eps
                    #print 'p_y_hyp_sum......................'
                    #print p_y_hyp_sum.data
                    rl_loss = p_y_hyp / p_y_hyp_sum * tc.log(bleus_T / bleus_sum) + \
                            p_y_hyp_T / p_y_hyp_sum * tc.log(bleus / bleus_sum)
                    #print 'rl_loss......................'
                    #print rl_loss.data

                    rl_loss = tc.sum(-rl_loss * _to_Var(1 - tc.eye(B)))

                    (1 * rl_loss).backward()

                    # initializing parameters of interactive attention model
                    #for n, p in self.nmtModel.named_parameters():
                        #if name.startswith('decoder') and not name == 'decoder.trg_lookup_table.weight':
                        #param.data.normal_(0, 0.01)
                        #print n, p.data.mean(), p.grad.data.mean()

                    self.optim_RL.step()

                    debug('Mean BLEU: {}, rl_loss: {}, rl_rho: {}, Bat BLEU: {}'.format(
                        rl_avg_bleu, rl_loss.data[0], rl_rho.data[0], rl_bat_bleu))
                    #del rl_loss, correct_num
                    #del o1, p_y_gold, p_y_hyp2, hyps_mask
                    del hyps, hyps_mask, o_hyps, p_y_hyp, bleus

            wlog('Discrimitor KL distance {}'.format(w_loss_D.data[0]))
            wlog('Epo:{:>2}/{:>2} end, W-MLE:{:4.2f}, W-ppl:{:4.2f}, '
                 'S-MLE:{:4.2f}'.format(eid, wargs.max_epochs, batch_loss/N, math.exp(batch_loss/N), batch_loss/B))
            wlog('Mean BLEU: {}, rl_loss: {}, rl_rho: {}, Bat BLEU: {}'.format(
                rl_avg_bleu, rl_loss.data[0], rl_rho.data[0], rl_bat_bleu))
            del w_loss_D, batch_loss, rl_avg_bleu, rl_loss, rl_rho

