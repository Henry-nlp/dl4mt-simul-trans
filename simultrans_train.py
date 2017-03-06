"""
Simultaneous Machine Translateion: Training with Policy Gradient

"""
import argparse
import os
import cPickle as pkl

from bleu import *
from nmt_uni import *
from policy import Controller as Policy
from utils import Progbar, Monitor
from data_iterator import check_length, iterate

from simultrans_model_clean import simultaneous_decoding
from simultrans_model_clean import _seqs2words, _bpe2words, _padding

import time

numpy.random.seed(19920206)
timer = time.time


# run training function:: >>>
def run_simultrans(model,
                   options_file=None,
                   config=None,
                   id=None,
                   remote=False):

    WORK = config['workspace']

    # check hidden folders
    paths = ['.policy', '.pretrained', '.log', '.config', '.images', '.translate']
    for p in paths:
        p = WORK + p
        if not os.path.exists(p):
            os.mkdir(p)

    if id is not None:
        fcon = WORK + '.config/{}.conf'.format(id)
        if os.path.exists(fcon):
            print 'load config files'
            policy, config = pkl.load(open(fcon, 'r'))

    # ============================================================================== #
    # load model model_options
    # ============================================================================== #
    _model = model.split('/')[-1]

    if options_file is not None:
        with open(options_file, 'rb') as f:
            options = pkl.load(f)
    else:
        with open('%s.pkl' % model, 'rb') as f:
            options = pkl.load(f)

    print 'merge configuration into options'
    for w in config:
        if (w in options) and (config[w] is not None):
            options[w] = config[w]

    print 'load options...'
    for w, p in sorted(options.items(), key=lambda x: x[0]):
        print '{}: {}'.format(w, p)

    # load detail settings from option file:
    dictionary, dictionary_target = options['dictionaries']

    # load source dictionary and invert
    with open(dictionary, 'rb') as f:
        word_dict = pkl.load(f)
    word_idict = dict()
    for kk, vv in word_dict.iteritems():
        word_idict[vv] = kk
    word_idict[0] = '<eos>'
    word_idict[1] = 'UNK'

    # load target dictionary and invert
    with open(dictionary_target, 'rb') as f:
        word_dict_trg = pkl.load(f)
    word_idict_trg = dict()
    for kk, vv in word_dict_trg.iteritems():
        word_idict_trg[vv] = kk
    word_idict_trg[0] = '<eos>'
    word_idict_trg[1] = 'UNK'

    options['pre'] = config['pre']

    # ========================================================================= #
    # Build a Simultaneous Translator
    # ========================================================================= #

    # allocate model parameters
    params = init_params(options)
    params = load_params(model, params)
    tparams = init_tparams(params)

    # print 'build the model for computing cost (full source sentence).'
    trng, use_noise, \
    _x, _x_mask, _y, _y_mask, \
    opt_ret, \
    cost, f_cost = build_model(tparams, options)
    print 'done'

    # functions for sampler
    f_sim_ctx, f_sim_init, f_sim_next = build_simultaneous_sampler(tparams, options, trng)

    # function for finetune the underlying model
    if options['finetune']:
        ff_init, ff_cost, ff_update = build_simultaneous_model(tparams, options, rl=True)
        funcs = [f_sim_ctx, f_sim_init, f_sim_next, f_cost, ff_init, ff_cost, ff_update]

    else:
        funcs = [f_sim_ctx, f_sim_init, f_sim_next, f_cost]


    # check the ID:
    options['base'] = _model
    agent     = Policy(trng, options,
                       n_in=options['readout_dim'] + 1 if options['coverage'] else options['readout_dim'],
                       n_out=3 if config['forget'] else 2,
                       recurrent=options['recurrent'], id=id)

    # make the dataset ready for training & validation
    trainIter = TextIterator(options['datasets'][0], options['datasets'][1],
                             options['dictionaries'][0], options['dictionaries'][1],
                             n_words_source=options['n_words_src'], n_words_target=options['n_words'],
                             batch_size=config['batchsize'],
                             maxlen=options['maxlen'])

    train_num = trainIter.num

    validIter = TextIterator(options['valid_datasets'][0], options['valid_datasets'][1],
                             options['dictionaries'][0], options['dictionaries'][1],
                             n_words_source=options['n_words_src'], n_words_target=options['n_words'],
                             batch_size=64, cache=10,
                             maxlen=1000000)

    valid_num = validIter.num
    print 'training set {} lines / validation set {} lines'.format(train_num, valid_num)
    print 'use the reward function {}'.format(chr(config['Rtype'] + 65))

    # ================================================================================= #
    # Main Loop: Run
    # ================================================================================= #
    print 'Start Simultaneous Translator...'
    probar = Progbar(train_num / config['batchsize'], with_history=False)
    monitor = None
    if remote:
        monitor = Monitor(root='http://localhost:9000')

    # freqs
    save_freq     = 200
    sample_freq   = 10
    valid_freq    = 200
    valid_size    = 200
    display_freq  = 50
    finetune_freq = 5

    history, last_it = agent.load()
    action_space = ['W', 'C', 'F']
    Log_avg = {}
    time0 = timer()

    pipe = OrderedDict()
    for key in ['x', 'x_mask', 'y', 'y_mask', 'c_mask']:
        pipe[key] = []

    def _translate(src, trg, samples=None, train=False, greedy=False):
        ret = simultaneous_decoding(
            funcs, agent, options,
            src, trg, word_idict_trg,
            samples, greedy, train)
        return ret

    for it, (srcs, trgs) in enumerate(trainIter):  # only one sentence each iteration
        if it < last_it:  # go over the scanned lines.
            continue

        # for validation
        # doing the whole validation!!
        reference = []
        system    = []

        if it % valid_freq == (valid_freq-1):
            print 'start validation'

            collections = [[], [], [], [], []]
            probar_v = Progbar(valid_num / 64 + 1)
            for ij, (srcs, trgs) in enumerate(validIter):

                statistics = _translate(srcs, trgs, samples=1, train=False, greedy=True)

                quality, delay, reward = zip(*statistics['track'])
                reference += statistics['Ref']
                system    += statistics['Sys']

                # compute the average consective waiting length
                def _consective(action):
                    waits = []
                    temp = 0
                    for a in action:
                        if a == 0:
                            temp += 1
                        elif temp > 0:
                            waits += [temp]
                            temp = 0

                    if temp > 0:
                        waits += [temp]

                    mean = numpy.mean(waits)
                    gec = numpy.max(waits)  # numpy.prod(waits) ** (1./len(waits))
                    return mean, gec

                def _max_length(action):
                    _cur = 0
                    _end = 0
                    _max = 0
                    for it, a in enumerate(action):
                        if a == 0:
                            _cur += 1
                        elif a == 2:
                            _end += 1

                        temp = _cur - _end
                        if temp > _max:
                            _max = temp
                    return _max

                maxlen = [_max_length(action) for action in statistics['action']]
                means, gecs = zip(*(_consective(action) for action in statistics['action']))

                collections[0] += quality
                collections[1] += delay
                collections[2] += means
                collections[3] += gecs
                collections[4] += maxlen

                values = [('quality', numpy.mean(quality)), ('delay', numpy.mean(delay)),
                          ('wait_mean', numpy.mean(means)), ('wait_max', numpy.mean(gecs)),
                          ('max_len', numpy.mean(maxlen))]
                probar_v.update(ij + 1, values=values)

            validIter.reset()
            valid_bleu, valid_delay, valid_wait, valid_wait_gec, valid_mx = [numpy.mean(a) for a in collections]
            print 'Iter = {}: AVG BLEU = {}, DELAY = {}, WAIT(MEAN) = {}, WAIT(MAX) = {}, MaxLen={}'.format(
                it, valid_bleu, valid_delay, valid_wait, valid_wait_gec, valid_mx)

            print 'Compute the Corpus BLEU={} (greedy)'.format(corpus_bleu(reference, system))

            with open(WORK + '.translate/test.txt', 'w') as fout:
                for sys in system:
                    fout.write('{}\n'.format(' '.join(sys)))

            with open(WORK + '.translate/ref.txt', 'w') as fout:
                for ref in reference:
                    fout.write('{}\n'.format(' '.join(ref[0])))

            history += [collections]

        if options['upper']:
            print 'done'
            import sys; sys.exit(-1)

        # training set sentence tuning
        new_srcs, new_trgs = [], []
        for src, trg in zip(srcs, trgs):
            if len(src) <= options['s0']:
                continue  # ignore when the source sentence is less than sidx. we don't use the policy\
            else:
                new_srcs += [src]
                new_trgs += [trg]

        if len(new_srcs) == 0:
            continue

        srcs, trgs = new_srcs, new_trgs
        statistics, info = _translate(srcs, trgs, train=True)

        if it % sample_freq == 0:

            print 'source: ', _bpe2words(_seqs2words([srcs[0]], word_idict))[0]
            print 'target: ', _bpe2words(_seqs2words([trgs[0]], word_idict_trg))[0]

            # obtain the translation results
            samples = _bpe2words(_seqs2words(statistics['sample'], word_idict_trg))

            # obtain the delay (normalized)
            # delays = _action2delay(srcs[0], statistics['action'])

            c  = 0
            for j in xrange(len(samples)):

                if statistics['secs'][j][0] == 0:
                    if c < (config['sample']/2.):
                        c += 1
                        continue

                    print '---ID: {}'.format(agent.id)
                    print 'sample: ', samples[j]
                    print 'action: ', ','.join(
                        ['{}'.format(action_space[t])
                         for t in statistics['action'][j]])

                    print 'quality:', statistics['track'][j][0]
                    print 'delay:',   statistics['track'][j][1]
                    print 'reward:',  statistics['track'][j][2]
                    break

        values = [(w, info[w]) for w in info]
        probar.update(it + 1, values=values)

        # NaN detector
        for w in info:
            if numpy.isnan(info[w]) or numpy.isinf(info[w]):
                raise RuntimeError, 'NaN/INF is detected!! {} : ID={}'.format(w, id)

        # remote display
        if remote:
            logs = {'R': info['R'], 'Q': info['Q'],
                    'D': info['D'], 'P': float(info['P'])}
            if 'a_cost' in info:
                logs['A'] = info['a_cost']

            print logs
            for w in logs:
                Log_avg[w] = Log_avg.get(w, 0) + logs[w]

            if it % display_freq == (display_freq - 1):
                for w in Log_avg:
                    Log_avg[w] /= display_freq

                monitor.display(it + 1, Log_avg)
                Log_avg = dict()

        # save the history & model
        history += [info]
        if it % save_freq == 0:
            agent.save(history, it)


if __name__ == "__main__":
    from config import rl_config
    config = rl_config()

    run_simultrans(config['model'],
                   options_file=config['option'],
                   config=config,
                   id=None,
                   remote=False)




