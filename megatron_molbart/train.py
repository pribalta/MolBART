from molbart.tokeniser import MolEncTokeniser
from molbart.util import DEFAULT_CHEM_TOKEN_START
from molbart.util import REGEX
from molbart.util import DEFAULT_VOCAB_PATH
from megatron import print_rank_0, get_tensorboard_writer
from megatron.initialize import initialize_megatron
from megatron.model import get_params_for_weight_decay_optimization
from megatron.learning_rates import AnnealingLR
from megatron import mpu
from megatron.utils import report_memory
from megatron.utils import reduce_losses
from megatron.training import evaluate
from megatron import get_timers
from apex.optimizers import FusedAdam as Adam
from torch.optim import AdamW
from megatron_bart import MegatronBART
from molbart.decoder import DecodeSampler
import deepspeed
from csv_data import MoleculeDataLoader
from megatron import get_args
import numpy as np
import pickle
import torch
from molbart.models.pre_train import BARTModel
import random
from deepspeed.utils import RepeatingLoader
import os
import argparse
import pandas as pd
import sys
from torch.utils.tensorboard import SummaryWriter
tokenizer = MolEncTokeniser.from_vocab_file(DEFAULT_VOCAB_PATH, REGEX,
        DEFAULT_CHEM_TOKEN_START)
num_batches_processed = 0
epochs = 0


class RepeatingLoader:

    def __init__(self, loader):
        """Wraps an iterator to allow for infinite iteration. This is especially useful
        for DataLoader types that we wish to automatically restart upon completion.
        Args:
            loader (iterator): The data loader to repeat.
        """

        self.loader = loader
        self.data_iter = iter(self.loader)

    def __iter__(self):
        return self

    def __next__(self):
        global epochs
        global num_batches_processed
        try:
            batch = next(self.data_iter)
        except StopIteration:
            self.data_iter = iter(self.loader)
            batch = next(self.data_iter)
            if torch.distributed.get_rank() == 0:
                epochs += 1
                num_batches_processed = 0
        return batch


def build_model_default(args):
    VOCAB_SIZE = len(tokenizer)
    MAX_SEQ_LEN = 512
    pad_token_idx = tokenizer.vocab[tokenizer.pad_token]
    sampler = DecodeSampler(tokenizer, MAX_SEQ_LEN)

    model = BARTModel(
        sampler,
        pad_token_idx,
        VOCAB_SIZE,
        args.hidden_size,
        args.num_layers,
        args.num_attention_heads,
        args.hidden_size * 4,
        0.1,
        0.1,
        'gelu',
        10000,
        MAX_SEQ_LEN,
        dropout=0.1,
        )
    return model


def build_model(args):

    VOCAB_SIZE = len(tokenizer)
    MAX_SEQ_LEN = 512
    pad_token_idx = tokenizer.vocab[tokenizer.pad_token]
    sampler = DecodeSampler(tokenizer, MAX_SEQ_LEN)

    model = MegatronBART(
        sampler,
        pad_token_idx,
        VOCAB_SIZE,
        args.hidden_size,
        args.num_layers,
        args.num_attention_heads,
        args.hidden_size * 4,
        MAX_SEQ_LEN,
        dropout=0.1,
        )

    def count_parameters(model):
        return sum(p.numel() for p in model.parameters()
                   if p.requires_grad)

    print_rank_0('Number of parameters in MegatronBART: '
                 + str(count_parameters(model)))
    return model


def get_optimizer(model, args):
    param_groups = get_params_for_weight_decay_optimization(model)
    for param_group in param_groups:
        for param in param_group['params']:
            if not hasattr(param, 'model_parallel'):
                param.model_parallel = False
    optimizer = AdamW(param_groups, lr=args.lr,
                      weight_decay=args.weight_decay,
                      betas=(args.adam_beta1, args.adam_beta2))
    return optimizer


def get_learning_rate_scheduler(optimizer, args):

    # Add linear learning rate scheduler.

    lr_scheduler = AnnealingLR(
        optimizer,
        start_lr=args.lr,
        warmup_iter=args.warmup * args.train_iters,
        total_iters=args.train_iters,
        decay_style=args.lr_decay_style,
        min_lr=args.min_lr,
        last_iter=0,
        use_checkpoint_lr_scheduler=False,
        override_lr_scheduler=False,
        )

    return lr_scheduler


def setup_model_and_optimizer(args):
    """Setup model and optimizer."""

    model = build_model(args)
    optimizer = get_optimizer(model, args)
    lr_scheduler = get_learning_rate_scheduler(optimizer, args)

    print_rank_0('DeepSpeed is enabled.')

    # (mpu if args.pipe_parallel_size == 0 else None)
    localrankmpi = int(os.getenv('LOCAL_RANK', '0'))
    rankmpi = int(os.getenv('RANK', '0'))
    args.rank = rankmpi
    args.local_rank = localrankmpi
    (model, optimizer, _, lr_scheduler) = deepspeed.initialize(
        model=model,
        optimizer=optimizer,
        args=args,
        lr_scheduler=lr_scheduler,
        mpu=(mpu if args.pipe_parallel_size == 0 else None),
        dist_init_required=False,
        )

    return (model, optimizer, lr_scheduler)


def get_batch(data_iterator):
    """Generate a batch"""

    global num_batches_processed
    keys = [
        'encoder_input',
        'encoder_pad_mask',
        'decoder_input',
        'decoder_pad_mask',
        'target',
        'target_pad_mask'
        ]
    datatype = torch.int64
    data = next(data_iterator)
    data_b = mpu.broadcast_data(keys, data, datatype)

    # Unpack.

    encoder_tokens = data_b['encoder_input'].long()
    encoder_pad_mask = data_b['encoder_pad_mask'].bool()
    decoder_tokens = data_b['decoder_input'].long()
    decoder_pad_mask = data_b['decoder_pad_mask'].bool()
    target = data_b['target'].long()
    target_pad_mask = data_b['target_pad_mask'].long()
    target_smiles = data['target_smiles']
    num_batches_processed += 1

    return {
        'encoder_input': encoder_tokens,
        'encoder_pad_mask': encoder_pad_mask,
        'decoder_input': decoder_tokens,
        'decoder_pad_mask': decoder_pad_mask,
        'target': target,
        'target_pad_mask': target_pad_mask,
        'target_smiles': target_smiles
        }


def forward_step(data_iterator, model,validate=False):
    """Forward step."""

    timers = get_timers()

    # Get the batch.

    timers('batch generator').start()
    batch = get_batch(data_iterator)
    timers('batch generator').stop()

    # Forward model.

    tokens = batch['target']
    pad_mask = batch['target_pad_mask']
    target_smiles = batch['target_smiles']
    outputs = model(batch)
    token_output = outputs['token_output']
    loss = model.module._calc_loss(batch, outputs)
    acc = model.module._calc_char_acc(batch, outputs)
    reduced_loss = reduce_losses([loss])
    # Reduce loss for logging.
    if validate:
        perplexity = model.module._calc_perplexity(batch, outputs)
        (mol_strs, log_lhs) = model.module.sample_molecules(batch,
                sampling_alg=model.module.val_sampling_alg)
        metrics = model.module.sampler.calc_sampling_metrics(mol_strs,
                target_smiles)

        val_outputs = {
            'mask loss': reduced_loss[0],
            'acc': acc,
            'val_perplexity': perplexity,
            'val_molecular_accuracy': metrics['accuracy'],
            'val_invalid_smiles': metrics['invalid'],
            }
        return (loss, val_outputs)
    
    return (loss, {'mask loss': reduced_loss[0], 'acc': acc})


def backward_step(optimizer, model, loss):
    """Backward step."""

    timers = get_timers()

    # Backward pass.

    timers('backward-backward').start()
    model.backward(loss)
    timers('backward-backward').stop()
    timers('backward-allreduce').reset()


def eval_step(data_iterator, model):
    """Forward step."""

    timers = get_timers()

    # Get the batch.

    timers('batch generator').start()
    batch = next(data_iterator)
    timers('batch generator').stop()

    # Forward model.

    val_ouputs = model.module.validation_step(batch)
    invalid_smiles = val_ouputs['val_invalid_smiles']
    val_loss = val_outpus['val_loss']
    token_acc = val_outputs['val_token_acc']
    val_perplexity= val_outputs['val_perplexity']
    val_molecular_accuracy= val_outputs['val_molecular_accuracy']
    # Reduce loss for logging.

    reduced_invalid_smiles = reduce_losses([invalid_smiles])
    
    return {'val_invalid_smiles': reduced_invalid_smiles[0], 'val_molecular_accuracy':val_molecular_accuracy}

def evaluate(forward_step_func, data_iterator, model, verbose=False):
    """Evaluation."""
    args = get_args()

    # Turn on evaluation mode which disables dropout.
    model.eval()

    total_loss_dict = {}

    with torch.no_grad():
        iteration = 0
        while iteration < args.eval_iters:
            iteration += 1
            if verbose and iteration % args.log_interval == 0:
                print_rank_0('Evaluating iter {}/{}'.format(iteration,
                                                            args.eval_iters))
            # Forward evaluation.
            _, loss_dict = forward_step_func(data_iterator, model,validate=True)

            # When contiguous memory optimizations are enabled, the buffers
            # allocated by the optimizations are deallocated during backward pass
            # in the absence of backward pass the buffers should be reset after each
            # forward pass
            if args.deepspeed and args.deepspeed_activation_checkpointing:
                deepspeed.checkpointing.reset()

            # Reduce across processes.
            for key in loss_dict:
                total_loss_dict[key] = total_loss_dict.get(key, 0.) + \
                    loss_dict[key]
    # Move model back to the train mode.
    model.train()

    for key in total_loss_dict:
        total_loss_dict[key] /= args.eval_iters

    return total_loss_dict

def train_step(
    forward_step_func,
    data_iterator,
    model,
    optimizer,
    lr_scheduler,
    pipe_parallel_size,
    ):
    """Single training step."""

    timers = get_timers()

    # Forward model for one step.

    timers('forward').start()
    (loss, loss_reduced) = forward_step_func(data_iterator, model)
    timers('forward').stop()

    # Calculate gradients, reduce across processes, and clip.

    timers('backward').start()
    backward_step(optimizer, model, loss)
    timers('backward').stop()

    # Update parameters.

    timers('optimizer').start()
    model.step()
    timers('optimizer').stop()

    return loss_reduced


def save_ds_checkpoint(iteration, model, args):
    """Save a model checkpoint."""

    sd = {}
    sd['iteration'] = iteration

    # rng states.

    if not args.no_save_rng:
        sd['random_rng_state'] = random.getstate()
        sd['np_rng_state'] = np.random.get_state()
        sd['torch_rng_state'] = torch.get_rng_state()
        sd['cuda_rng_state'] = torch.cuda.get_rng_state()
        sd['rng_tracker_states'] = \
            mpu.get_cuda_rng_tracker().get_states()

    model.save_checkpoint(args.save, client_state=sd)


def train(
    forward_step_func,
    model,
    optimizer,
    lr_scheduler,
    train_data_iterator,
    trainloader,
    val_data_iterator,
    pipe_parallel_size,
    args,
    ):
    """Train the model function."""

    global num_batches_processed
    writer = get_tensorboard_writer()
    timers = get_timers()
    model.train()
    iteration = 0
    timers('interval time').start()
    report_memory_flag = True
    while iteration < args.train_iters:
        loss = train_step(
            forward_step_func,
            train_data_iterator,
            model,
            optimizer,
            lr_scheduler,
            pipe_parallel_size,
            )

        iteration += 1
        print_rank_0('Iteration: ' + str(iteration) + '/'
                     + str(args.train_iters) + ', Loss: '
                     + str(loss['mask loss'].item()) + ', Acc: '
                     + str(loss['acc']) + ', Num batches: '
                     + str(num_batches_processed) + '/'
                     + str(len(trainloader.loader)) + ', Epoch: '
                     + str(epochs))
        if torch.distributed.get_rank() == 0:
            writer.add_scalar('training mask loss',loss['mask loss'], iteration)
            writer.add_scalar('training acc',loss['acc'], iteration)
        # Checkpointing
        if iteration % args.save_interval == 0:
            save_ds_checkpoint(iteration, model, args)
        if iteration % args.eval_interval == 0:
            loss_dict_val= evaluate(forward_step_func, val_data_iterator, model)
            if torch.distributed.get_rank() == 0:
                writer.add_scalar('validation mask loss',loss_dict_val['mask loss'], iteration)
                writer.add_scalar('validation acc',loss_dict_val['acc'], iteration)
                writer.add_scalar('validation perplexity',loss_dict_val['val_perplexity'], iteration)
                writer.add_scalar('validation molecular accuracy',loss_dict_val['val_molecular_accuracy'], iteration)
                writer.add_scalar('Invalid smiles',loss_dict_val['val_invalid_smiles'], iteration)
    return iteration



def run_training(ckpt_dir='megatron_molbart_checkpoint'):
    deepspeed.init_distributed()
    initialize_megatron()
    args = get_args()
    print_rank_0('Loading dataset(s) ...')
    path = os.path.dirname(os.path.realpath(__file__))
    # loader = MoleculeDataLoader(path + '/test_data/chembl_subset.csv',
    #                             batch_size=256, num_workers=32)
    loader = MoleculeDataLoader(args.dataset_path,
                                batch_size=args.batch_size, num_workers=32)
    (train_dataloader, val_dataloader) = loader.get_data()
    print_rank_0('Setting up model ...')
    (model, optimizer, lr_scheduler) = setup_model_and_optimizer(args)
    if ckpt_dir is not None:
        model.load_checkpoint(args.save)
    print_rank_0('Starting training ...')
    train_dataloader = RepeatingLoader(train_dataloader)
    val_dataloader = RepeatingLoader(val_dataloader)

    train(
        forward_step,
        model,
        optimizer,
        lr_scheduler,
        iter(train_dataloader),
        train_dataloader,
        iter(val_dataloader),
        args.pipe_parallel_size,
        args,
        )


def load_model():
    initialize_megatron()
    args = get_args()
    (model, optimizer, lr_scheduler) = setup_model_and_optimizer(args)
    ckpt = model.load_checkpoint(args.save)


if __name__ == '__main__':
    writer = SummaryWriter()
    run_training()