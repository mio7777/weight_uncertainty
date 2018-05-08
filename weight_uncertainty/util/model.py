from weight_uncertainty.util.util import MixturePrior, make_train_op
from weight_uncertainty.util.util_layers import BayesianLSTMCell, BayesianConvCell, SoftmaxLayer
import tensorflow as tf
from weight_uncertainty import conf
import numpy as np


class Model(object):
    def __init__(self, num_classes, size_sample):
        # Set up the placeholders
        self.x_placeholder = tf.placeholder(tf.float32, [None] + list(size_sample), name='input')
        self.y_placeholder = tf.placeholder(tf.int32, [None, ], name='target')

        # Instantiate a prior over the weights
        self.prior = MixturePrior(conf.sigma_prior)

        self.is_time_series = len(size_sample) == 1
        use_rnn = False

        self.layers = []  # Store the parameters of each layer, so we can compute the cost function later
        if use_rnn:
            outputs = self.add_RNN()
        else:
            outputs = self.add_CNN()

        logits = self.softmax_layer(outputs, num_classes)

        self.predictions = tf.nn.softmax(logits, name='predictions')

        # Classification loss
        class_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=self.y_placeholder)
        self.loss = tf.identity(tf.reduce_mean(class_loss), name='classification_loss')

        # KL loss
        # Sum the KL losses of each layer in layers
        self.kl_loss = 0.0
        for layer in self.layers:
            self.kl_loss += layer.get_kl()

        # Weigh the kl loss across all the batches
        # See equation 9 in
        # Weight uncertainty in neural networks
        # https://arxiv.org/abs/1505.05424
        num_batches = conf.max_steps  # Make explicit that this represents the number of batches
        # pi = 1./num_batches
        pi = ramp_and_clip(1/1000., 1/10., 3000, 20000, global_step=None)
        total_loss = self.loss + pi*self.kl_loss

        # Set up the optimizer
        tvars = tf.trainable_variables()
        shapes = [tvar.get_shape() for tvar in tvars]
        print("# params: %d" % np.sum([np.prod(s) for s in shapes]))

        # Clip the gradients if desired
        grads = tf.gradients(total_loss, tvars)
        # for grad, tvar in zip(grads, tvars):
        #     name = str(tvar.name).replace(':', '_')
        #     if 'mask' in name:
        #         continue
        #
        #     tf.summary.histogram(name + '_var', tvar)
        #     try:
        #         tf.summary.histogram(name + '_grad', grad)
        #     except ValueError as e:
        #         print(name)
        #         raise Exception(e)
        if conf.clip_norm > 0.0:
            grads, grads_norm = tf.clip_by_global_norm(grads, conf.clip_norm)
        else:
            grads_norm = tf.global_norm(grads)

        self.train_op = make_train_op(conf.optimizer_name, grads, tvars)

        # Calculate accuracy
        decisions = tf.argmax(logits, axis=1, output_type=tf.int32)
        self.accuracy = tf.reduce_mean(tf.cast(tf.equal(decisions, self.y_placeholder), tf.float32), name='accuracy')

        self.add_tensorboard_summaries(grads_norm)

        # Calculate total number of bits
        self.total_bits = tf.constant(0.0, dtype=tf.float32)
        sigma_collection = tf.get_collection('all_sigma')
        for var in sigma_collection:
            self.total_bits += tf.reduce_mean(var)
        # Total bits is the -log of the average standard deviation
        self.total_bits = -tf.log(self.total_bits/float(len(sigma_collection))) / tf.log(2.)
        tf.summary.scalar('Total bits', self.total_bits)

        # Final Tensorflow bookkeeping
        self.summary_op = tf.summary.merge_all()
        self.saver = tf.train.Saver()
        self.init_op = tf.global_variables_initializer()
        self.saver = tf.train.Saver()
        self.add_to_collections()
        print('Finished model')

    def add_RNN(self):
        inputs = tf.unstack(tf.expand_dims(self.x_placeholder, axis=1), axis=2)
        # Stack many BayesianLSTMCells
        # Note that by this call, the epsilon is equal for all time steps
        for _ in range(conf.num_layers):
            lstm_cell = BayesianLSTMCell(conf.hidden_dim, self.prior,
                                         forget_bias=1.0, state_is_tuple=True, layer_norm=False)
            self.layers.append(lstm_cell)

        cell = tf.nn.rnn_cell.MultiRNNCell(self.layers, state_is_tuple=True)

        # Make the RNN
        with tf.variable_scope("RNN"):
            outputs, state = tf.nn.static_rnn(cell, inputs,
                                              dtype=tf.float32)
        outputs = outputs[-1]  # Perform classification on the final state
        return outputs

    def add_CNN(self):
        if self.is_time_series:
            inputs = tf.expand_dims(tf.expand_dims(self.x_placeholder, axis=2), axis=3)
        else:
            inputs = self.x_placeholder

        filter_shape = conf.get_filter_shape(self.is_time_series)

        # First layer
        conv_layer1 = BayesianConvCell('conv1', num_filters=conf.num_filters[0], filter_shape=filter_shape, stride=3, prior=self.prior,
                                         activation=tf.nn.selu)
        hidden1 = conv_layer1(inputs)
        self.layers.append(conv_layer1)
        tf.summary.histogram('Layer1', hidden1, family='activations')

        # Second layer
        conv_layer2 = BayesianConvCell('conv2', num_filters=conf.num_filters[1], filter_shape=filter_shape, stride=3, prior=self.prior,
                                         activation=tf.nn.selu)
        hidden2 = conv_layer2(hidden1)
        self.layers.append(conv_layer2)

        # Third layer
        conv_layer3 = BayesianConvCell('conv3', num_filters=conf.num_filters[2], filter_shape=filter_shape, stride=3, prior=self.prior,
                                         activation=tf.nn.selu)
        hidden3 = conv_layer3(hidden2)
        self.layers.append(conv_layer3)

        h3_shape = hidden3.shape
        outputs = tf.reshape(hidden3, shape=[-1, h3_shape[1:].num_elements()])
        return outputs

    def softmax_layer(self, outputs, num_classes):
        # Final output mapping to num_classes
        softmaxlayer = SoftmaxLayer(num_classes, self.prior)
        self.layers.append(softmaxlayer)
        return softmaxlayer(outputs)

    def add_tensorboard_summaries(self, grads_norm=0.0):
        """
        Add some nice summaries for Tensorboard
        :param grads_norm:
        :return:
        """
        # Summaries for TensorBoard
        with tf.name_scope("summaries"):
            tf.summary.scalar("loss", self.loss)
            tf.summary.scalar("kl_loss", self.kl_loss)
            tf.summary.scalar("grads_norm", grads_norm)
            tf.summary.scalar("accuracy", self.accuracy)

        self.all_sigma = tf.concat([tf.reshape(s, [-1]) for s in tf.get_collection('all_sigma')], axis=0)
        tf.summary.histogram('Sigmas', self.all_sigma, family='sigmas')

        self.all_SNR = tf.concat([tf.reshape(tf.abs(mean)/sig, [-1]) for mean, sig in zip(tf.get_collection('random_mean'),
                                                                       tf.get_collection('all_sigma'))],
                        axis=0)
        tf.summary.histogram('snr', self.all_SNR, family='SNR')

    def add_to_collections(self):
        """
        Add the variables to a collection that we will use when restoring a model
        :return:
        """
        for var in [self.x_placeholder,
                    self.y_placeholder,
                    self.predictions,
                    self.loss,
                    self.accuracy]:
            tf.add_to_collection('restore_vars', var)


def ramp_and_clip(value_start, value_stop, step_start, step_stop, global_step=None):
    if not global_step:
        global_step = tf.train.get_or_create_global_step()
    pi = value_start + (value_stop - value_start) * \
           tf.clip_by_value(1. / (step_stop- step_start) *
                            (tf.cast(global_step, tf.float32) - step_start), 0., 1.)
    tf.summary.scalar('pi', pi, family='summaries')
    return pi