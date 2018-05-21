import argparse
import os
import tensorflow as tf
import random
import numpy as np
import importlib
import sys
import time
from .utils import utils

from tensorflow.python import debug as tf_debug

sys.path.insert(0, os.path.abspath('.'))
tf.logging.set_verbosity(tf.logging.INFO)
tf.logging.info('test')

def add_arguments(parser):
    parser.register("type", "bool", lambda v: v.lower() == "true")

    parser.add_argument('--reset', type="bool", const=True, nargs="?", default=False)

    parser.add_argument('--dataset', type=str, default="aps")
    parser.add_argument('--model', type=str, default="ctc-attention")

    parser.add_argument("--num_units", type=int, default=32, help="Network size.")
    parser.add_argument("--num_encoder_layers", type=int, default=2,
                        help="Encoder depth, equal to num_layers if None.")
    parser.add_argument("--num_decoder_layers", type=int, default=2,
                        help="Decoder depth, equal to num_layers if None.")
    parser.add_argument("--random_seed", type=int, default=None,
                        help="Random seed (>0, set a specific seed).")
    parser.add_argument("--batch_size", type=int, default=128, help="Batch size.")
    parser.add_argument("--num_buckets", type=int, default=5,
                        help="Put data into similar-length buckets.")
    parser.add_argument("--max_train", type=int, default=0,
                        help="Limit on the size of training data (0: no limit).")

    parser.add_argument('--sample_rate', type=float, default=16000)
    parser.add_argument('--window_size_ms', type=float, default=30.0)
    parser.add_argument('--window_stride_ms', type=float, default=10.0)

    # optimizer
    parser.add_argument("--optimizer", type=str, default="adam", help="sgd | adam")
    parser.add_argument("--learning_rate", type=float, default=1e-3,
                        help="Learning rate. Adam: 0.001 | 0.0001")

    parser.add_argument(
        "--num_train_steps", type=int, default=12000, help="Num steps to train.")
    parser.add_argument("--colocate_gradients_with_ops", type="bool", nargs="?",
                        const=True,
                        default=True,
                        help=("Whether try colocating gradients with "
                              "corresponding op"))

    parser.add_argument("--summaries_dir", type=str, default="log")
    parser.add_argument("--out_dir", type=str, default=None,
                        help="Store log/model files.")

def create_hparams(flags):
    return tf.contrib.training.HParams(
        model=flags.model,
        dataset=flags.dataset,
        reset=flags.reset,

        num_units=flags.num_units,
        num_encoder_layers=flags.num_encoder_layers,
        num_decoder_layers=flags.num_decoder_layers,
        batch_size=flags.batch_size,
        summaries_dir=flags.summaries_dir,
        out_dir=flags.out_dir or "saved_models/%s_%s" %
                (flags.model.replace('-', '_'), flags.dataset.replace('-', '_')),

        num_train_steps=flags.num_train_steps,
        colocate_gradients_with_ops=flags.colocate_gradients_with_ops,

        sample_rate=flags.sample_rate,
        window_size_ms=flags.window_size_ms,
        window_stride_ms=flags.window_stride_ms,

        num_buckets=flags.num_buckets,
        max_train=flags.max_train,

        optimizer=flags.optimizer,
        learning_rate=flags.learning_rate,
        epoch_step=0,
    )

class ModelWrapper:
    def __init__(self, hparams, mode, BatchedInput, Model):
        self.graph = tf.Graph()
        self.hparams = hparams
        with self.graph.as_default():
            self.batched_input = BatchedInput(hparams, mode)
            self.batched_input.init_dataset()
            self.iterator = self.batched_input.iterator
            self.model = Model(
                hparams,
                mode=mode,
                iterator=self.iterator
            )

    def train(self, sess):
        return self.model.train(sess)

    def save(self, sess, global_step):
        tf.logging.info('Saving to "%s-%d"', self.hparams.out_dir, global_step)
        self.model.saver.save(sess, os.path.join(self.hparams.out_dir, "csp.ckpt"))

    def create_model(self, sess, name):
        sess.run(tf.global_variables_initializer())
        sess.run(tf.tables_initializer())
        global_step = self.model.global_step.eval(session=sess)
        return self.model, global_step

    def load_model(self, sess, name):
        latest_ckpt = tf.train.latest_checkpoint(self.hparams.out_dir)
        if latest_ckpt:
            self.model.saver.restore(sess, latest_ckpt)
            sess.run(tf.tables_initializer())
            global_step = self.model.global_step.eval(session=sess)
            return self.model, global_step
        else: return self.create_model(sess, name)

def train(Model, BatchedInput, hparams):
    hparams.num_classes = BatchedInput.num_classes
    train_model = ModelWrapper(
        hparams,
        tf.estimator.ModeKeys.TRAIN,
        BatchedInput, Model
    )
    eval_model = ModelWrapper(
        hparams,
        tf.estimator.ModeKeys.EVAL,
        BatchedInput, Model
    )

    train_sess = tf.Session(graph=train_model.graph)
    # train_sess = tf_debug.LocalCLIDebugWrapperSession(train_sess)

    with train_model.graph.as_default():
        train_model.batched_input.reset_iterator(train_sess)

    with train_model.graph.as_default():
        loaded_train_model, global_step = train_model.create_model(train_sess, "train") \
            if hparams.reset else train_model.load_model(train_sess, "train")

    train_writer = tf.summary.FileWriter(os.path.join(hparams.summaries_dir, "%s_%s" % (hparams.model, hparams.dataset), "log_train"), train_sess.graph)
    validation_writer = tf.summary.FileWriter(os.path.join(hparams.summaries_dir, "%s_%s" % (hparams.model, hparams.dataset), "log_validation"))

    last_save_step = global_step
    while global_step < hparams.num_train_steps:
        train_cost = train_ler = 0
        start = time.time()

        try:
            batch_cost, ler, global_step = loaded_train_model.train(train_sess)
            hparams.epoch_step += 1
        except tf.errors.OutOfRangeError:
            hparams.epoch_step = 0
            train_model.batched_input.reset_iterator(train_sess)
            continue

        train_cost += batch_cost * hparams.batch_size
        train_ler += ler * hparams.batch_size
        train_writer.add_summary(train_model.model.summary, global_step)

        train_cost /= hparams.batch_size
        train_ler /= hparams.batch_size

        # val_cost, val_ler = sess.run([self.cost, self.ler])
        val_cost, val_ler = 0, 0

        log = "Epoch {}, Batch {}/{}, train_cost = {:.3f}, train_ler = {:.3f}, time = {:.3f}"
        tf.logging.info(log.format(
            global_step * hparams.batch_size // train_model.batched_input.size + 1,
            (global_step * hparams.batch_size % train_model.batched_input.size) // (hparams.batch_size + 1),
            train_model.batched_input.size // hparams.batch_size,
            train_cost, train_ler,
            time.time() - start))

        if global_step - last_save_step >= 300:
            train_model.save(train_sess, global_step)
            last_save_step = global_step

def main(unused_argv):
    hparams = create_hparams(FLAGS)

    random_seed = FLAGS.random_seed
    if random_seed is not None and random_seed > 0:
        random.seed(random_seed)
        np.random.seed(random_seed)

    BatchedInput = utils.get_batched_input_class(FLAGS)
    Model = utils.get_model_class(FLAGS)

    train(Model, BatchedInput, hparams)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    FLAGS, unparsed = parser.parse_known_args()
    tf.app.run(main=main, argv=[sys.argv[0]] + unparsed)
