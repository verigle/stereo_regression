import tensorflow as tf
import os
import sys
from collections import namedtuple

from data.tfrecords import read_and_decode
from data.decoders import get_decoder_class
from data.shapes import InputShape
from experiment.configuration import Configuration
from experiment.logger import Logger
from models.factory import create_model

DecodeConfig = namedtuple('DecodeConfig', 'name flags is_training size shapes queues')
SplitSizes = namedtuple('SplitSizes', 'train train_valid valid test')

flags = tf.app.flags

flags.DEFINE_string('model', None, 'Name of the model to create')
flags.DEFINE_string('dataset', 'kitti', 'Name of the dataset to prepare')
flags.DEFINE_integer('epochs', 100, 'Number of train epochs')
flags.DEFINE_integer('examples', 200, 'Number of dataset examples')
flags.DEFINE_float('lr', 1e-3, 'Initial learning rate')

flags.DEFINE_float('train_ratio', 0.8, 'Train subset split size')
flags.DEFINE_float('train_valid_ratio', 0.01, 'Train valid subset split size')
flags.DEFINE_float('valid_ratio', 0.19, 'Valid subset split size')
flags.DEFINE_float('test_ratio', 0.0, 'Test subset split size')

flags.DEFINE_integer('batch_size', 1, 'Batch size')
flags.DEFINE_integer('num_threads', 5, 'Number of reading threads')
flags.DEFINE_integer('capacity', 50, 'Queue capacity')

flags.DEFINE_integer('width', 512, 'Crop width')
flags.DEFINE_integer('height', 256, 'Crop width')
flags.DEFINE_integer('max_disp', 192, 'Maximum possible disparity')

flags.DEFINE_string('config', None, 'Configuration file')

FLAGS = flags.FLAGS


def get_decoder_configurations(flags, config, split_sizes):
    shapes = InputShape(flags.width, flags.height, 3, config.get('max_disp', flags.max_disp), 256)
    shapes_l = InputShape(900, 300, 3, config.get('max_disp', flags.max_disp), 256)
    decode_configs = [DecodeConfig('train', flags, True, split_sizes.train, shapes, config.train)]
    if split_sizes.train_valid > 0:
        decode_configs.append(
            DecodeConfig('train_valid', flags, False, split_sizes.train_valid, shapes_l, config.train_valid))
    if split_sizes.valid > 0:
        decode_configs.append(
            DecodeConfig('valid', flags, False, split_sizes.valid, shapes_l, config.valid))
    if split_sizes.test > 0:
        decode_configs.append(DecodeConfig('test', flags, False, split_sizes.test, shapes_l, config.test))
    return decode_configs


def main(_):
    # create global configuration object
    model_config = Configuration(FLAGS.config)
    # calculate number of steps in an epoch for each subset
    train_epoch_steps = int(round(FLAGS.examples * FLAGS.train_ratio / FLAGS.batch_size))
    train_valid_epoch_steps = int(round(FLAGS.examples * FLAGS.train_valid_ratio / FLAGS.batch_size))
    valid_epoch_steps = int(round(FLAGS.examples * FLAGS.valid_ratio / FLAGS.batch_size))
    test_epoch_steps = int(round(FLAGS.examples * FLAGS.test_ratio / FLAGS.batch_size))
    split_sizes = SplitSizes(train_epoch_steps, train_valid_epoch_steps, valid_epoch_steps, test_epoch_steps)
    # create placeholders for queue runners
    configs = get_decoder_configurations(FLAGS, model_config, split_sizes)
    decoder_class = get_decoder_class(FLAGS.dataset)
    with tf.variable_scope('placeholders'):
        placeholders = {}
        for config in configs:
            with tf.variable_scope('input_{}'.format(config.name)):
                placeholders[config.name] = read_and_decode(
                    tf.train.string_input_producer(config.queues, shuffle=config.is_training, capacity=FLAGS.capacity),
                    decoder_class(config))
    # create model and create graphs for each input
    model = create_model(FLAGS, model_config)
    model.build(placeholders['train'], True, None)
    print(placeholders.keys(), split_sizes)
    for split, steps in zip(['train_valid', 'valid', 'test'],
                            [split_sizes.train_valid, split_sizes.valid, split_sizes.test]):
        if steps > 0:
            model.build(placeholders[split], False, True)
    saver = tf.train.Saver()
    session = tf.Session()
    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=session, coord=coord)
    # create train method
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        optimizers = {
            'adam': tf.train.AdamOptimizer,
            'sgd': tf.train.GradientDescentOptimizer,
            'rms_prop': tf.train.RMSPropOptimizer,
        }
        optimizer = optimizers[model_config.get('optimizer', 'adam')]
        train_step = optimizer(FLAGS.lr).minimize(model.losses[placeholders['train']])
    # init variables
    session.run(tf.local_variables_initializer())
    session.run(tf.global_variables_initializer())
    # restore model if provided a checkpoint
    if model_config.checkpoint is not None:
        saver.restore(session, model_config.checkpoint)
    # redirect stdout to file keeping stdout unchanged
    f = open(os.path.join(model_config.directory, 'log.txt'), 'w')
    sys.stdout = Logger(sys.stdout, f)
    # prepare directory for checkpoint storing
    checkpoints = os.path.join(model_config.directory, 'checkpoints')
    os.makedirs(checkpoints, exist_ok=True)
    try:
        for epoch in range(FLAGS.epochs):
            # calculate train losses and perform train steps
            for _ in range(split_sizes.train):
                _, train_loss = session.run([train_step, model.losses[placeholders['train']]])
                print("train: epoch {} loss {}".format(epoch, train_loss))
            # calculate valid losses
            for _ in range(split_sizes.valid):
                valid_loss = session.run(model.losses[placeholders['valid']])
                print("valid: epoch {} loss {}".format(epoch, valid_loss))
            # calculate losses used for early stopping and save checkpoint if best parameters found
            if split_sizes.train_valid > 0:
                train_valid_losses = []
                for _ in range(split_sizes.train_valid):
                    train_valid_losses.append(session.run(model.losses[placeholders['train_valid']]))
                    print("train_valid: epoch {} loss {}".format(epoch, train_valid_losses[-1]))
                try:
                    current = sum(train_valid_losses) / len(train_valid_losses)
                    if epoch == 0:
                        best = current
                    if current <= best:
                        saver.save(session, os.path.join(checkpoints, '{}.cpkt'.format(epoch)))
                except ZeroDivisionError:
                    pass

    except Exception as e:
        print(e)
    finally:
        # in case of an exception, store model checkpoint and stop queue runners
        checkpoint_file = os.path.join(checkpoints, 'final.cpkt')
        saver.save(session, checkpoint_file)
        print("Model saved to {}".format(checkpoint_file), file=sys.stderr)
        coord.request_stop()
        coord.join(threads)


if __name__ == '__main__':
    tf.app.run()
