from __future__ import division
from __future__ import absolute_import

import os
import math
import numpy
import torch as tc

import wargs
from tools.utils import *
from tools.dictionary import Dictionary

import sys
import tools.text_encoder as text_encoder
import tools.tokenizer as tokenizer
from collections import defaultdict
from tools.inputs import *

# English tokens
EN_SPACE_TOK = 3
# Chinese tokens
ZH_SPACE_TOK = 16

# 32768
def get_or_generate_vocab(data_file, vocab_file, vocab_size=2**15):
    """Inner implementation for vocab generators.
    Args:
    vocab_filename: relative filename where vocab file is stored
    vocab_file: generated vocabulary file
    vocab_size: target size of the vocabulary constructed by SubwordTextEncoder
    Returns:
    A SubwordTextEncoder vocabulary object.
    """
    if os.path.exists(vocab_file):
      wlog('Load dictionary from file {}'.format(vocab_file))
      vocab = text_encoder.SubwordTextEncoder(vocab_file)
      return vocab

    token_counts = defaultdict(int)
    for item in genVcb(data_file):
      for tok in tokenizer.encode(text_encoder.native_to_unicode(item)):
          token_counts[tok] += 1

    vocab = text_encoder.SubwordTextEncoder.build_to_target_size(vocab_size, token_counts, 1, 1e3)
    vocab.store_to_file(vocab_file)
    wlog('Save vocabulary file into {}'.format(vocab_file))

    return vocab

"""Generate a vocabulary from the datasets in sources."""
def genVcb(filepath):
    wlog("Generating vocab from: {}".format(filepath))
    # Use Tokenizer to count the word occurrences.
    with open(filepath, 'r') as train_file:
        file_byte_budget = 1e6
        counter = 0
        countermax = int(os.path.getsize(filepath) / file_byte_budget / 2)
        for line in train_file:
            if counter < countermax:
                counter += 1
            else:
                if file_byte_budget <= 0: break
                line = line.strip()
                file_byte_budget -= len(line)
                counter = 0
                yield line

def extract_vocab(data_file, vocab_file, max_vcb_size=30000):

    if os.path.exists(vocab_file) is True:

        # If vocab file has been exist, we load word dictionary
        wlog('Load dictionary from file {}'.format(vocab_file))
        vocab = Dictionary()
        vocab.load_from_file(vocab_file)

    else:

        vocab = count_vocab(data_file, max_vcb_size)
        vocab.write_into_file(vocab_file)
        wlog('Save dictionary file into {}'.format(vocab_file))

    return vocab

def count_vocab(data_file, max_vcb_size):

    vocab = Dictionary()
    with open(data_file, 'r') as f:
        for sent in f.readlines():
            sent = sent.strip()
            for word in sent.split():
                vocab.add(word)

    # vocab.write_into_file('all.vocab')

    words_cnt = sum(vocab.freq.itervalues())
    new_vocab, new_words_cnt = vocab.keep_vocab_size(max_vcb_size)
    wlog('|Final vocabulary| / |Original vocabulary| = {} / {} = {:4.2f}%'
         .format(new_words_cnt, words_cnt, (new_words_cnt/words_cnt) * 100))

    return new_vocab

def wrap_data(data_dir, file_prefix, src_suffix, trg_prefix,
              src_vocab, trg_vocab, shuffle=True, sort_data=True, max_seq_len=50):

    srcF = open(os.path.join(data_dir, '{}.{}'.format(file_prefix, src_suffix)), 'r')
    num = len(srcF.readlines())
    srcF.close()
    point_every, number_every = int(math.ceil(num/100)), int(math.ceil(num/10))

    srcF = open(os.path.join(data_dir, '{}.{}'.format(file_prefix, src_suffix)), 'r')

    trgFs = []  # maybe have multi-references for valid, we open them together
    for fname in os.listdir(data_dir):
        if fname.startswith('{}.{}'.format(file_prefix, trg_prefix)):
            wlog('\t{}'.format(os.path.join(data_dir, fname)))
            trgFs.append(open(os.path.join(data_dir, fname), 'r'))
    wlog('NOTE: Target side has {} references.'.format(len(trgFs)))

    idx, ignore, longer = 0, 0, 0
    srcs, trgs, slens = [], [], []
    while True:

        src_sent = srcF.readline().strip()
        trg_refs = [trgF.readline().strip() for trgF in trgFs]

        if src_sent == '' and all([trg_ref == '' for trg_ref in trg_refs]):
            wlog('\nFinish to read bilingual corpus.')
            break

        if numpy.mod(idx + 1, point_every) == 0: wlog('.', False)
        if numpy.mod(idx + 1, number_every) == 0: wlog('{}'.format(idx + 1), False)
        idx += 1

        if src_sent == '' or any([trg_ref == '' for trg_ref in trg_refs]):
            wlog('Ignore abnormal blank sentence in line number {}'.format(idx))
            ignore += 1
            continue

        src_words = src_sent.split()
        src_len = len(src_words)
        trg_refs_words = [trg_ref.split() for trg_ref in trg_refs]
        if src_len <= max_seq_len and all([len(tws) <= max_seq_len for tws in trg_refs_words]):

            if wargs.word_piece is True:

                src_wids = src_vocab.encode(src_sent)
                trg_refs_wids = [trg_vocab.encode(trg_ref) for trg_ref in trg_refs]

                src_tensor = ids2Tensor(src_wids)
                trg_refs_tensor = [ids2Tensor(trg_ref_wids, bos_id=BOS, eos_id=EOS)
                                   for trg_ref_wids in trg_refs_wids]
            else:
                src_tensor = src_vocab.keys2idx(src_words, UNK_WORD)
                trg_refs_tensor = [trg_vocab.keys2idx(trg_ref_words, UNK_WORD,
                                                  bos_word=BOS_WORD, eos_word=EOS_WORD)
                                   for trg_ref_words in trg_refs_words]

            srcs.append(src_tensor)
            trgs.append(trg_refs_tensor)
            slens.append(src_len)
        else:
            longer += 1

    srcF.close()
    for trgF in trgFs: trgF.close()

    train_size = len(srcs)
    assert train_size == idx - ignore - longer, 'Wrong .. '
    wlog('Sentence-pairs count: {}(total) - {}(ignore) - {}(longer) = {}'.format(
        idx, ignore, longer, idx - ignore - longer))

    if shuffle is True:

        #assert len(trgFs) == 1, 'Unsupport to shuffle validation set.'
        wlog('Shuffling the whole dataset ... ', False)
        rand_idxs = tc.randperm(train_size).tolist()
        srcs = [srcs[k] for k in rand_idxs]
        trgs = [trgs[k] for k in rand_idxs]
        slens = [slens[k] for k in rand_idxs]

    final_srcs, final_trgs = srcs, trgs

    if sort_data is True:

        #assert len(trgFs) == 1, 'Unsupport to sort validation set in k batches.'
        final_srcs, final_trgs = [], []

        if wargs.sort_k_batches == 0:
            wlog('Sorting the whole dataset by ascending order of source length ... ', False)
            # sort the whole training data by ascending order of source length
            _, sorted_idx = tc.sort(tc.IntTensor(slens))
            final_srcs = [srcs[k] for k in sorted_idx]
            final_trgs = [trgs[k] for k in sorted_idx]
        else:
            wlog('Sorting for each {} batches ... '.format(wargs.sort_k_batches), False)

            k_batch = wargs.batch_size * wargs.sort_k_batches
            number = int(math.ceil(train_size / k_batch))

            for start in range(number):
                bsrcs = srcs[start * k_batch : (start + 1) * k_batch]
                btrgs = trgs[start * k_batch : (start + 1) * k_batch]
                bslens = slens[start * k_batch : (start + 1) * k_batch]
                _, sorted_idx = tc.sort(tc.IntTensor(bslens))
                final_srcs += [bsrcs[k] for k in sorted_idx]
                final_trgs += [btrgs[k] for k in sorted_idx]

    wlog('Done.')

    return final_srcs, final_trgs

def wrap_tst_data(src_data, src_vocab):

    srcs, slens = [], []
    srcF = open(src_data, 'r')
    idx = 0

    while True:

        src_sent = srcF.readline()
        if src_sent == '':
            wlog('\nFinish to read monolingual test dataset {}, count {}'.format(src_data, idx))
            break
        idx += 1

        if src_sent == '':
            wlog('Error. Ignore abnormal blank sentence in line number {}'.format(idx))
            sys.exit(0)

        src_sent = src_sent.strip()
        src_words = src_sent.split()
        src_len = len(src_words)
        if wargs.word_piece is True:
            src_wids = src_vocab.encode(src_sent)
            srcs.append(ids2Tensor(src_wids))
        else:
            srcs.append(src_vocab.keys2idx(src_words, UNK_WORD))

        slens.append(src_len)

    srcF.close()
    return srcs, slens


if __name__ == "__main__":

    src = os.path.join(wargs.dir_data, '{}.{}'.format(wargs.train_prefix, wargs.train_src_suffix))
    trg = os.path.join(wargs.dir_data, '{}.{}'.format(wargs.train_prefix, wargs.train_trg_suffix))
    vocabs = {}
    if wargs.word_piece is True:
        wlog('\n[w/Subword] Preparing source vocabulary from {} ... '.format(src))
        src_vocab = get_or_generate_vocab(src, wargs.src_dict)
        wlog('\n[w/Subword] Preparing target vocabulary from {} ... '.format(trg))
        trg_vocab = get_or_generate_vocab(trg, wargs.trg_dict)
    else:
        wlog('\n[o/Subword] Preparing source vocabulary from {} ... '.format(src))
        src_vocab = extract_vocab(src, wargs.src_dict, wargs.src_dict_size)
        wlog('\n[o/Subword] Preparing target vocabulary from {} ... '.format(trg))
        trg_vocab = extract_vocab(trg, wargs.trg_dict, wargs.trg_dict_size)
    src_vocab_size, trg_vocab_size = src_vocab.size(), trg_vocab.size()
    wlog('Vocabulary size: |source|={}, |target|={}'.format(src_vocab_size, trg_vocab_size))
    vocabs['src'], vocabs['trg'] = src_vocab, trg_vocab

    wlog('\nPreparing training set from {} and {} ... '.format(src, trg))
    trains = {}
    train_src_tlst, train_trg_tlst = wrap_data(wargs.dir_data, wargs.train_prefix,
                                               wargs.train_src_suffix, wargs.train_trg_suffix,
                                               src_vocab, trg_vocab, max_seq_len=wargs.max_seq_len)
    assert len(train_trg_tlst[0]) == 1, 'Require only one reference in training dataset.'
    '''
    list [torch.LongTensor (sentence), torch.LongTensor, torch.LongTensor, ...]
    no padding
    '''

    batch_train = Input(train_src_tlst, train_trg_tlst, wargs.batch_size)
    wlog('Sentence-pairs count in training data: {}'.format(len(train_src_tlst)))

    batch_valid = None
    if wargs.val_prefix is not None:
        val_src_file = '{}{}.{}'.format(wargs.val_tst_dir, wargs.val_prefix, wargs.val_src_suffix)
        val_trg_file = '{}{}.{}'.format(wargs.val_tst_dir, wargs.val_prefix, wargs.val_ref_suffix)
        wlog('\nPreparing validation set from {} and {} ... '.format(val_src_file, val_trg_file))
        valid_src_tlst, valid_trg_tlst = wrap_data(wargs.val_tst_dir, wargs.val_prefix,
                                                   wargs.val_src_suffix, wargs.val_ref_suffix,
                                                   src_vocab, trg_vocab,
                                                   shuffle=False, sort_data=False,
                                                   max_seq_len=wargs.dev_max_seq_len)
        batch_valid = Input(valid_src_tlst, valid_trg_tlst, 1, volatile=True, batch_sort=False)

    batch_tests = None
    if wargs.tests_prefix is not None:
        assert isinstance(wargs.tests_prefix, list), 'Test files should be list.'
        init_dir(wargs.dir_tests)
        batch_tests = {}
        for prefix in wargs.tests_prefix:
            init_dir(wargs.dir_tests + '/' + prefix)
            test_file = '{}{}.{}'.format(wargs.val_tst_dir, prefix, wargs.val_src_suffix)
            wlog('\nPreparing test set from {} ... '.format(test_file))
            test_src_tlst, _ = wrap_tst_data(test_file, src_vocab)
            batch_tests[prefix] = Input(test_src_tlst, None, 1, volatile=True)

    inputs = {}
    inputs['vocab'] = vocabs
    inputs['train'] = batch_train
    inputs['valid'] = batch_valid
    inputs['tests'] = batch_tests

    wlog('Saving data to {} ... '.format(wargs.inputs_data), False)
    tc.save(inputs, wargs.inputs_data)
    wlog('\n## Finish to Prepare Dataset ! ##\n')












