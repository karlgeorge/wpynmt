import wargs
import torch as tc
from torch import cuda
from inputs import Input
from utils import init_dir, wlog
from optimizer import Optim
from train import *
import const

import torch.backends.cudnn as cudnn
cudnn.benchmark = True
cudnn.enabled = True

from model_rnnsearch_ia import *
from translate import Translator

def main():

    # Check if CUDA is available
    if cuda.is_available():
        wlog('CUDA is available, specify device by gpu_id argument (i.e. gpu_id=[3])')
    else:
        wlog('Warning: CUDA is not available, train CPU')

    if wargs.gpu_id: cuda.set_device(wargs.gpu_id[0])

    init_dir(wargs.dir_model)
    init_dir(wargs.dir_valid)
    init_dir(wargs.dir_tests)
    for prefix in wargs.tests_prefix: init_dir(wargs.dir_tests + '/' + prefix)

    wlog('Loading data ... ', 0)

    inputs = tc.load(wargs.inputs_data)

    vocab_data, train_data, valid_data = inputs['vocab'], inputs['train'], inputs['valid']

    train_src_tlst, train_trg_tlst = train_data['src'], train_data['trg']
    valid_src_tlst, valid_src_lens = valid_data['src'], valid_data['len']

    wlog('Sentence-pairs count in training data: {}'.format(len(train_src_tlst)))
    src_vocab_size, trg_vocab_size = vocab_data['src'].size(), vocab_data['trg'].size()
    wlog('Vocabulary size: |source|={}, |target|={}'.format(src_vocab_size, trg_vocab_size))

    batch_train = Input(train_src_tlst, train_trg_tlst, wargs.batch_size)
    batch_valid = Input(valid_src_tlst, None, 1, volatile=True)

    tests_data = None
    if inputs.has_key('tests'):
        tests_data = {}
        tests_tensor = inputs['tests']
        for prefix in tests_tensor.keys():
            tests_data[prefix] = Input(tests_tensor[prefix], None, 1, volatile=True)


    '''
    # lookup_table on cpu to save memory
    src_lookup_table = nn.Embedding(wargs.src_dict_size + 4,
                                    wargs.src_wemb_size, padding_idx=const.PAD).cpu()
    trg_lookup_table = nn.Embedding(wargs.trg_dict_size + 4,
                                    wargs.trg_wemb_size, padding_idx=const.PAD).cpu()

    wlog('Lookup table on CPU ... ')
    wlog(src_lookup_table)
    wlog(trg_lookup_table)
    '''

    sv = vocab_data['src'].idx2key
    tv = vocab_data['trg'].idx2key

    nmtModel = NMT()
    classifier = Classifier(wargs.out_size, trg_vocab_size)

    if wargs.pre_train:

        #pre_dict = tc.load(wargs.pre_train)
        pre_dict = tc.load(wargs.pre_train, map_location=lambda storage, loc: storage)
        pre_model_dict = pre_dict['model']
        pre_model_dict = {k: v for k, v in pre_model_dict.items() if 'classifier' not in k}

        # initializing parameters of interactive attention model
        for name, param in nmtModel.named_parameters():
            if name.startswith('decoder') and not name == 'decoder.trg_lookup_table.weight':
                #param.data.normal_(0, 0.01)
                wlog(name)
                init_params(param)
                pre_model_dict[name] = param

        #for k, v in pre_model_dict.items():
        #    print k

        nmtModel.load_state_dict(pre_model_dict)
        classifier.load_state_dict(pre_dict['class'])

        wlog('Loading pre-trained model from {} at epoch {} and batch {}'.format(
            wargs.pre_train, pre_dict['epoch'], pre_dict['batch']))

        #wlog('Loading optimizer from {}'.format(wargs.pre_train))
        #optim = pre_dict['optim']
        #wlog(optim)

        wargs.start_epoch = pre_dict['epoch']

    else:

        for p in nmtModel.parameters():
            #p.data.uniform_(-0.1, 0.1)
            init_params(p)

    optim = Optim(
        wargs.opt_mode, wargs.learning_rate, wargs.max_grad_norm,
        learning_rate_decay=wargs.learning_rate_decay,
        start_decay_from=wargs.start_decay_from,
        last_valid_bleu=wargs.last_valid_bleu
    )

    if wargs.gpu_id:
        wlog('Push model onto GPU ... ')
        nmtModel.cuda()
        classifier.cuda()
    else:
        wlog('Push model onto CPU ... ')
        nmtModel.cpu()
        classifier.cuda()

    nmtModel.classifier = classifier

    '''
    nmtModel.src_lookup_table = src_lookup_table
    nmtModel.trg_lookup_table = trg_lookup_table
    print nmtModel.src_lookup_table.weight.data.is_cuda

    nmtModel.classifier.init_weights(nmtModel.trg_lookup_table)
    '''

    wlog(nmtModel)
    pcnt1 = len([p for p in nmtModel.parameters()])
    pcnt2 = sum([p.nelement() for p in nmtModel.parameters()])
    wlog('Parameters number: {}/{}'.format(pcnt1, pcnt2))

    optim.init_optimizer(nmtModel.parameters())

    #tor = Translator(nmtModel, sv, tv)
    #tor.trans_tests(tests_data, pre_dict['epoch'], pre_dict['batch'])

    train(nmtModel, batch_train, batch_valid, tests_data, vocab_data, optim)

    if tests_data and wargs.final_test:

        bestModel = NMT()
        classifier = Classifier(wargs.out_size, trg_vocab_size)

        assert os.path.exists(wargs.best_model)
        model_dict = tc.load(wargs.best_model)

        best_model_dict = model_dict['model']
        best_model_dict = {k: v for k, v in best_model_dict.items() if 'classifier' not in k}

        bestModel.load_state_dict(best_model_dict)
        classifier.load_state_dict(model_dict['class'])

        if wargs.gpu_id:
            wlog('Push NMT model onto GPU ... ')
            bestModel.cuda()
            classifier.cuda()
        else:
            wlog('Push NMT model onto CPU ... ')
            bestModel.cpu()
            classifier.cpu()

        bestModel.classifier = classifier

        tor = Translator(bestModel, sv, tv)
        tor.trans_tests(tests_data, model_dict['epoch'], model_dict['batch'])





if __name__ == "__main__":

    main()














