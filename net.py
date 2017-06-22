# encoding: utf-8

import numpy as np

import chainer
from chainer import cuda
import chainer.functions as F
import chainer.links as L
from chainer import reporter

from seq2seq import source_pad_concat_convert

# linear_init = chainer.initializers.GlorotNormal()
linear_init = chainer.initializers.LeCunUniform()


def sentence_block_embed(embed, x):
    """ Change implicitly embed_id function's target to ndim=2

    Apply embed_id for array of ndim 2,
    shape (batchsize, sentence_length),
    instead for array of ndim 1.

    """

    batch, length = x.shape
    e = embed(x.reshape((batch * length, )))
    # (batch * length, units)
    e = F.transpose(F.stack(F.split_axis(e, batch, axis=0), axis=0), (0, 2, 1))
    # (batch, units, length)
    return e


def seq_func(func, x, reconstruct_shape=True):
    """ Change implicitly function's target to ndim=3

    Apply a given function for array of ndim 3,
    shape (batchsize, dimension, sentence_length),
    instead for array of ndim 2.

    """

    batch, units, length = x.shape
    e = F.transpose(x, (0, 2, 1)).reshape(batch * length, units)
    e = func(e)
    if not reconstruct_shape:
        return e
    out_units = e.shape[1]
    e = F.transpose(e.reshape((batch, length, out_units)), (0, 2, 1))
    return e


class LayerNormalizationSentence(L.LayerNormalization):

    """ Position-wise Linear Layer for Sentence Block

    Position-wise layer-normalization layer for array of shape
    (batchsize, dimension, sentence_length).

    """

    def __init__(self, *args, **kwargs):
        super(LayerNormalizationSentence, self).__init__(*args, **kwargs)

    def __call__(self, x):
        y = seq_func(super(LayerNormalizationSentence, self).__call__, x)
        return y


class ConvolutionSentence(L.Convolution2D):

    """ Position-wise Linear Layer for Sentence Block

    Position-wise linear layer for array of shape
    (batchsize, dimension, sentence_length)
    can be implemented a convolution layer.

    """

    def __init__(self, in_channels, out_channels,
                 ksize=1, stride=1, pad=0, nobias=False,
                 initialW=None, initial_bias=None):
        super(ConvolutionSentence, self).__init__(
            in_channels, out_channels,
            ksize, stride, pad, nobias,
            initialW, initial_bias)

    def __call__(self, x):
        """Applies the linear layer.

        Args:
            x (~chainer.Variable): Batch of input vector block. Its shape is
                (batchsize, in_channels, sentence_length).

        Returns:
            ~chainer.Variable: Output of the linear layer. Its shape is
                (batchsize, out_channels, sentence_length).

        """
        x = F.expand_dims(x, axis=3)
        y = super(ConvolutionSentence, self).__call__(x)
        y = F.squeeze(y, axis=3)
        return y


class MultiHeadAttention(chainer.Chain):

    """ Multi Head Attention Layer for Sentence Blocks

    For batch computation efficiency, dot product to calculate query-key
    scores is performed all heads together.

    """

    def __init__(self, n_units, h=8, dropout=0.1, self_attention=True):
        super(MultiHeadAttention, self).__init__()
        with self.init_scope():
            if self_attention:
                self.W_QKV = ConvolutionSentence(
                    n_units, n_units * 3, nobias=True,
                    initialW=linear_init)
            else:
                self.W_Q = ConvolutionSentence(
                    n_units, n_units, nobias=True,
                    initialW=linear_init)
                self.W_KV = ConvolutionSentence(
                    n_units, n_units * 2, nobias=True,
                    initialW=linear_init)
            self.FinishingLinearLayer = ConvolutionSentence(
                n_units, n_units, nobias=True,
                initialW=linear_init)
        self.h = h
        self.scale_score = 1. / (n_units // h) ** 0.5
        self.dropout = dropout
        self.self_attention = self_attention

    def __call__(self, x, z=None, mask=None):
        xp = self.xp
        h = self.h

        if self.self_attention:
            query, key, value = F.split_axis(self.W_QKV(x), 3, axis=1)
        else:
            query = self.W_Q(x)
            key, value = F.split_axis(self.W_KV(z), 2, axis=1)
        batch, n_units, n_querys = query.shape

        # Calculate Attention Scores with Mask for Zero-padded Areas
        # Perform Multi-head Attention using pseudo batching
        # all together at once for efficiency

        pseudo_batch_query = F.concat(F.split_axis(query, h, axis=1), axis=0)
        pseudo_batch_key = F.concat(F.split_axis(key, h, axis=1), axis=0)
        pseudo_batch_value = F.concat(F.split_axis(value, h, axis=1), axis=0)
        # q.shape = (b * h, n_units // h, n_querys)
        # k.shape = (b * h, n_units // h, n_keys)
        # v.shape = (b * h, n_units // h, n_keys)

        a = F.batch_matmul(pseudo_batch_query, pseudo_batch_key, transa=True)
        a *= self.scale_score
        mask = xp.concatenate([mask] * h, axis=0)
        a = F.where(mask, a, xp.full(a.shape, -np.inf, 'f'))
        a = F.softmax(a, axis=2)
        a = F.where(xp.isnan(a.data), xp.zeros(a.shape, 'f'), a)
        # a.shape = (b * h, n_querys, n_keys)

        # Calculate Weighted Sum
        a, pseudo_batch_value = F.broadcast(
            a[:, None], pseudo_batch_value[:, :, None])
        multi_c = F.sum(a * pseudo_batch_value, axis=3)
        concat_c = F.concat(F.split_axis(multi_c, self.h, axis=0), axis=1)
        lineared_c = self.FinishingLinearLayer(concat_c)
        return lineared_c


# Section 3.3 says the inner-layer has dimension 2048.
# But, Table 4 says d_{ff} = 1024 (for "base model").
# d_{ff}'s denotation is unclear, but it seems to denote the same one.


class FeedForwardLayer(chainer.Chain):
    def __init__(self, n_units):
        super(FeedForwardLayer, self).__init__()
        n_inner_units = n_units * 4
        with self.init_scope():
            self.W_1 = ConvolutionSentence(n_units, n_inner_units,
                                           initialW=linear_init)
            self.W_2 = ConvolutionSentence(n_inner_units, n_units,
                                           initialW=linear_init)
            self.act = F.relu

    def __call__(self, e):
        e = self.W_1(e)
        e = self.act(e)
        e = self.W_2(e)
        return e


class EncoderLayer(chainer.Chain):
    def __init__(self, n_units, h=8, dropout=0.1):
        super(EncoderLayer, self).__init__()
        with self.init_scope():
            self.SelfAttention = MultiHeadAttention(n_units, h)
            self.FeedForward = FeedForwardLayer(n_units)
            self.LN_1 = LayerNormalizationSentence(n_units, eps=1e-9)
            self.LN_2 = LayerNormalizationSentence(n_units, eps=1e-9)
        self.dropout = dropout

    def __call__(self, e, xx_mask):
        sub = self.SelfAttention(e, e, xx_mask)
        e = e + F.dropout(sub, self.dropout)
        e = self.LN_1(e)

        sub = self.FeedForward(e)
        e = e + F.dropout(sub, self.dropout)
        e = self.LN_2(e)
        return e


class DecoderLayer(chainer.Chain):
    def __init__(self, n_units, h=8, dropout=0.1):
        super(DecoderLayer, self).__init__()
        with self.init_scope():
            self.SelfAttention = MultiHeadAttention(n_units, h)
            self.SourceAttention = MultiHeadAttention(n_units, h,
                                                      self_attention=False)
            self.FeedForward = FeedForwardLayer(n_units)
            self.LN_1 = LayerNormalizationSentence(n_units, eps=1e-9)
            self.LN_2 = LayerNormalizationSentence(n_units, eps=1e-9)
            self.LN_3 = LayerNormalizationSentence(n_units, eps=1e-9)
        self.dropout = dropout

    def __call__(self, e, s, xy_mask, yy_mask):
        sub = self.SelfAttention(e, e, yy_mask)
        e = e + F.dropout(sub, self.dropout)
        e = self.LN_1(e)

        sub = self.SourceAttention(e, s, xy_mask)
        e = e + F.dropout(sub, self.dropout)
        e = self.LN_2(e)

        sub = self.FeedForward(e)
        e = e + F.dropout(sub, self.dropout)
        e = self.LN_3(e)
        return e


class Encoder(chainer.Chain):
    def __init__(self, n_layers, n_units, h=8, dropout=0.1):
        super(Encoder, self).__init__()
        self.layer_names = []
        for i in range(1, n_layers + 1):
            name = 'l{}'.format(i)
            layer = EncoderLayer(n_units, h, dropout)
            self.add_link(name, layer)
            self.layer_names.append(name)

    def __call__(self, e, xx_mask):
        for name in self.layer_names:
            e = getattr(self, name)(e, xx_mask)
        return e


class Decoder(chainer.Chain):
    def __init__(self, n_layers, n_units, h=8, dropout=0.1):
        super(Decoder, self).__init__()
        self.layer_names = []
        for i in range(1, n_layers + 1):
            name = 'l{}'.format(i)
            layer = DecoderLayer(n_units, h, dropout)
            self.add_link(name, layer)
            self.layer_names.append(name)

    def __call__(self, e, source, xy_mask, yy_mask):
        # Attention target is the final output of encoder,
        # or each output of each layer in it?
        # It depends on self.attend_one_by_one
        for name in self.layer_names:
            e = getattr(self, name)(e, source, xy_mask, yy_mask)
        return e


class Transformer(chainer.Chain):

    def __init__(self, n_layers, n_source_vocab, n_target_vocab, n_units,
                 h=8, dropout=0.1, max_length=500,
                 use_label_smoothing=False,
                 embed_position=False):
        super(Transformer, self).__init__()
        with self.init_scope():
            self.embed_x = L.EmbedID(n_source_vocab, n_units, ignore_label=-1,
                                     initialW=linear_init)
            self.embed_y = L.EmbedID(n_target_vocab, n_units, ignore_label=-1,
                                     initialW=linear_init)
            self.encoder = Encoder(n_layers, n_units, h, dropout)
            self.decoder = Decoder(n_layers, n_units, h, dropout)
            if embed_position:
                self.embed_pos = L.EmbedID(max_length, n_units,
                                           ignore_label=-1)
        self.n_layers = n_layers
        self.n_units = n_units
        self.n_target_vocab = n_target_vocab
        self.dropout = dropout
        self.use_label_smoothing = use_label_smoothing
        self.initialize_position_encoding(max_length, n_units)
        self.scale_emb = self.n_units ** 0.5

    def initialize_position_encoding(self, length, n_units):
        xp = self.xp
        start = 1  # index starts from 1 or 0
        posi_block = xp.arange(
            start, length + start, dtype='f')[None, None, :]
        unit_block = xp.arange(
            start, n_units // 2 + start, dtype='f')[None, :, None]
        rad_block = posi_block / 10000. ** (unit_block / (n_units // 2))
        sin_block = xp.sin(rad_block)
        cos_block = xp.cos(rad_block)
        self.position_encoding_block = xp.empty((1, n_units, length), 'f')
        self.position_encoding_block[:, ::2, :] = sin_block
        self.position_encoding_block[:, 1::2, :] = cos_block

    def make_input_embedding(self, embed, block):
        batch, length = block.shape
        emb_block = sentence_block_embed(embed, block) * self.scale_emb
        emb_block += self.xp.array(self.position_encoding_block[:, :, :length])
        if hasattr(self, 'embed_pos'):
            emb_block += sentence_block_embed(
                self.embed_pos,
                self.xp.broadcast_to(
                    self.xp.arange(length).astype('i')[None, :], block.shape))
        emb_block = F.dropout(emb_block, self.dropout)
        return emb_block

    def make_attention_mask(self, source_block, target_block):
        mask = (target_block[:, None, :] >= 0) * \
            (source_block[:, :, None] >= 0)
        # (batch, source_length, target_length)
        return mask

    def make_retrospective_mask(self, block):
        batch, length = block.shape
        arange = self.xp.arange(length)
        retrospective_mask = (arange[None, ] <= arange[:, None])[None, ]
        retrospective_mask = self.xp.broadcast_to(
            retrospective_mask, (batch, length, length))
        return retrospective_mask

    def output(self, h):
        return F.linear(h, self.embed_y.W)

    def output_and_loss(self, h_block, t_block):
        batch, units, length = h_block.shape

        # Output (all together at once for efficiency)
        concat_logit_block = seq_func(self.output, h_block,
                                      reconstruct_shape=False)
        rebatch, _ = concat_logit_block.shape
        # Make target
        concat_t_block = t_block.reshape((rebatch))
        ignore_mask = (concat_t_block >= 0)
        n_token = ignore_mask.sum()
        normalizer = n_token  # n_token or batch or 1

        if not self.use_label_smoothing:
            loss = F.softmax_cross_entropy(concat_logit_block, concat_t_block)
            loss = loss * n_token / normalizer
        else:
            log_prob = F.log_softmax(concat_logit_block)
            broad_ignore_mask = self.xp.broadcast_to(
                ignore_mask[:, None],
                concat_logit_block.shape)
            pre_loss = ignore_mask * \
                log_prob[self.xp.arange(rebatch), concat_t_block]
            loss = - F.sum(pre_loss) / normalizer

        accuracy = F.accuracy(
            concat_logit_block, concat_t_block, ignore_label=-1)
        perp = self.xp.exp(loss.data * normalizer / n_token)

        # Report the Values
        reporter.report({'loss': loss.data * normalizer / n_token,
                         'acc': accuracy.data,
                         'perp': perp}, self)

        if self.use_label_smoothing:
            label_smoothing = broad_ignore_mask * \
                - 1. / self.n_target_vocab * log_prob
            label_smoothing = F.sum(label_smoothing) / normalizer
            loss = 0.9 * loss + 0.1 * label_smoothing
        return loss

    def __call__(self, x_block, y_in_block, y_out_block, get_prediction=False):
        batch, x_length = x_block.shape
        batch, y_length = y_in_block.shape

        # Make Embedding
        ex_block = self.make_input_embedding(self.embed_x, x_block)
        ey_block = self.make_input_embedding(self.embed_y, y_in_block)

        # Make Masks
        xx_mask = self.make_attention_mask(x_block, x_block)
        xy_mask = self.make_attention_mask(y_in_block, x_block)
        yy_mask = self.make_attention_mask(y_in_block, y_in_block)
        yy_mask *= self.make_retrospective_mask(y_in_block)

        # Encode Sources
        z_blocks = self.encoder(ex_block, xx_mask)
        # [(batch, n_units, x_length), ...]

        # Encode Targets with Sources (Decode without Output)
        h_block = self.decoder(ey_block, z_blocks, xy_mask, yy_mask)
        # (batch, n_units, y_length)

        if get_prediction:
            return self.output(h_block[:, :, -1])
        else:
            return self.output_and_loss(h_block, y_out_block)

    def translate(self, x_block, max_length=50):
        # TODO: efficient inference by re-using result
        with chainer.no_backprop_mode():
            with chainer.using_config('train', False):
                x_block = source_pad_concat_convert(
                    x_block, device=None)
                batch, x_length = x_block.shape
                y_block = self.xp.zeros((batch, 1), dtype=x_block.dtype)
                eos_flags = self.xp.zeros((batch, ), dtype=x_block.dtype)
                result = []
                for i in range(max_length):
                    log_prob_tail = self(x_block, y_block, y_block,
                                         get_prediction=True)
                    ys = self.xp.argmax(log_prob_tail.data, axis=1).astype('i')
                    result.append(ys)
                    y_block = F.concat([y_block, ys[:, None]], axis=1).data
                    eos_flags += (ys == 0)
                    if self.xp.all(eos_flags):
                        break

        result = cuda.to_cpu(self.xp.stack(result).T)

        # Remove EOS taggs
        outs = []
        for y in result:
            inds = np.argwhere(y == 0)
            if len(inds) > 0:
                y = y[:inds[0, 0]]
            if len(y) == 0:
                y = np.array([1], 'i')
            outs.append(y)
        return outs
