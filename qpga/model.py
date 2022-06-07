import numpy as np
import tensorflow as tf
from tensorflow.python import keras
from tensorflow.python.keras import Sequential, Input
from tensorflow.python.keras.backend import dot
from tensorflow.python.keras.layers import Layer, Lambda



from qpga.utils import tf_to_k_complex, k_to_tf_complex
from qpga.constants import IDENTITY, CPHASE_MOD, BS_MATRIX, CPHASE
from qpga.linalg import tensors


def phase_shifts_to_tensor_product_space(phi_0, phi_1):
    phi_0_complex = tf.complex(tf.cos(phi_0), tf.sin(phi_0))
    phi_1_complex = tf.complex(tf.cos(phi_1), tf.sin(phi_1))

    single_qubit_ops = tf.unstack(
            tf.map_fn(lambda U: tf.linalg.tensor_diag(U), tf.transpose([phi_0_complex, phi_1_complex])))

    return tf.linalg.LinearOperatorKronecker([tf.linalg.LinearOperatorFullMatrix(U) for U in single_qubit_ops])


class SingleQubitOperationLayer(Layer):

    def __init__(self, num_qubits, **kwargs):
        self.num_qubits = num_qubits
        self.output_dim = 2 ** num_qubits
        super(SingleQubitOperationLayer, self).__init__(**kwargs)

    def get_config(self):
        config = super(SingleQubitOperationLayer, self).get_config()
        config.update({
            'num_qubits': self.num_qubits,
            # 'output_dim': self.output_dim
        })
        return config

    def build(self, input_shape):
        input_dim = input_shape[-1]
        assert input_dim == self.output_dim

        initializer = tf.random_uniform_initializer(minval = 0, maxval = 2 * np.pi)

        # Create a trainable weight variable for this layer.
        self.alphas = self.add_weight(name = 'alphas',
                                      dtype = tf.float64,
                                      shape = (self.num_qubits,),
                                      trainable = True,
                                      initializer = initializer)
        self.betas = self.add_weight(name = 'betas',
                                     dtype = tf.float64,
                                     shape = (self.num_qubits,),
                                     trainable = True,
                                     initializer = initializer)
        self.thetas = self.add_weight(name = 'thetas',
                                      dtype = tf.float64,
                                      shape = (self.num_qubits,),
                                      trainable = True,
                                      initializer = initializer)
        self.phis = self.add_weight(name = 'phis',
                                    dtype = tf.float64,
                                    shape = (self.num_qubits,),
                                    trainable = True,
                                    initializer = initializer)

        self.input_shifts = phase_shifts_to_tensor_product_space(self.alphas, self.betas).to_dense()
        self.theta_shifts = phase_shifts_to_tensor_product_space(self.thetas, tf.zeros_like(self.thetas)).to_dense()
        self.phi_shifts = phase_shifts_to_tensor_product_space(self.phis, tf.zeros_like(self.phis)).to_dense()

        self.bs_matrix = tf.convert_to_tensor(tensors([BS_MATRIX] * self.num_qubits), dtype = tf.complex128)

        # For TF 1.x
        super(SingleQubitOperationLayer, self).build(input_shape)

    # @tf.function
    # def get_hilbert_space_matrices(self):
    #     input_shifts = phase_shifts_to_tensor_product_space(self.alphas, self.betas).to_dense()
    #     theta_shifts = phase_shifts_to_tensor_product_space(self.thetas, tf.zeros_like(self.thetas)).to_dense()
    #     phi_shifts = phase_shifts_to_tensor_product_space(self.phis, tf.zeros_like(self.phis)).to_dense()
    #     bs_matrix = tf.convert_to_tensor(tensors([BS_MATRIX] * self.num_qubits), dtype = tf.complex128)
    #     return input_shifts, theta_shifts, phi_shifts, bs_matrix

    def call(self, x, **kwargs):
        out = x

        # The @tf.function decorator means that these tensor products are only computed once, so this isn't expensive
        # input_shifts, theta_shifts, phi_shifts, bs_matrix = self.get_hilbert_space_matrices()

        out = dot(out, self.input_shifts)
        out = dot(out, self.bs_matrix)
        out = dot(out, self.theta_shifts)
        out = dot(out, self.bs_matrix)
        out = dot(out, self.phi_shifts)

        return out

    def compute_output_shape(self, input_shape):
        return input_shape


class CPhaseLayer(Layer):

    def __init__(self, num_qubits, parity = 0, use_standard_cphase = False, **kwargs):
        self.num_qubits = num_qubits
        self.parity = parity
        self.use_standard_cphase = use_standard_cphase
        self.output_dim = 2 ** num_qubits
        super(CPhaseLayer, self).__init__(**kwargs)

    def get_config(self):
        config = super(CPhaseLayer, self).get_config()
        config.update({
            'num_qubits'         : self.num_qubits,
            # 'output_dim'         : self.output_dim,
            'parity'             : self.parity,
            'use_standard_cphase': self.use_standard_cphase,
        })
        return config

    def get_cphase_gate(self):
        return CPHASE if self.use_standard_cphase else CPHASE_MOD

    def build(self, input_shape):
        input_dim = input_shape[-1]
        assert input_dim == self.output_dim

        ops = []
        if self.parity == 0:
            num_cphase = self.num_qubits // 2
            for _ in range(num_cphase):
                ops.append(self.get_cphase_gate())
            if 2 * num_cphase < self.num_qubits:
                ops.append(IDENTITY)
        else:
            ops.append(IDENTITY)
            num_cphase = (self.num_qubits - 1) // 2
            for _ in range(num_cphase):
                ops.append(self.get_cphase_gate())
            if 2 * num_cphase + 1 < self.num_qubits:
                ops.append(IDENTITY)

        self.transfer_matrix_np = tensors(ops)
        self.transfer_matrix = tf.convert_to_tensor(self.transfer_matrix_np, dtype = tf.complex128)

        # For TF 1.x
        super(CPhaseLayer, self).build(input_shape)

    # @tf.function
    def call(self, x, **kwargs):
        return dot(x, self.transfer_matrix)

    def compute_output_shape(self, input_shape):
        return input_shape


class QPGA(keras.Model):

    def __init__(self, num_qubits, depth,
                 complex_inputs = False,
                 complex_outputs = False,
                 use_standard_cphase = True):
        super(QPGA, self).__init__(name = 'qpga')

        self.num_qubits = num_qubits
        self.input_dim = 2 ** num_qubits

        self.depth = depth
        self.complex_inputs = complex_inputs
        self.complex_outputs = complex_outputs
        self.use_standard_cphase = use_standard_cphase

        self.input_layer = SingleQubitOperationLayer(self.num_qubits)
        self.single_qubit_layers = []
        self.cphase_layers = []
        for i in range(depth):
            self.cphase_layers.append(CPhaseLayer(self.num_qubits,
                                                  parity = i % 2,
                                                  use_standard_cphase = self.use_standard_cphase))
            self.single_qubit_layers.append(SingleQubitOperationLayer(self.num_qubits))

    def as_sequential(self):
        '''Converts the QPGA instance into a sequential model for easier inspection'''
        model = Sequential()
        model.num_qubits = self.num_qubits
        model.complex_inputs = self.complex_inputs
        if not self.complex_inputs:
            model.add(Input(shape = (2, self.input_dim,), dtype = 'float64'))
            model.add(Lambda(lambda x: k_to_tf_complex(x), output_shape = (self.input_dim,)))
        else:
            model.add(Input(shape = (self.input_dim,), dtype = 'complex128'))

        model.add(self.input_layer)
        for cphase_layer, single_qubit_layer in zip(self.cphase_layers, self.single_qubit_layers):
            model.add(cphase_layer)
            model.add(single_qubit_layer)

        model.complex_outputs = self.complex_outputs
        if not self.complex_outputs:
            model.add(Lambda(lambda x: tf_to_k_complex(x)))

        return model

    # @tf.function
    def call(self, inputs):
        x = inputs

        if not self.complex_inputs:
            x = k_to_tf_complex(x)

        x = self.input_layer(x)
        for cphase_layer, single_qubit_layer in zip(self.cphase_layers, self.single_qubit_layers):
            x = cphase_layer(x)
            x = single_qubit_layer(x)

        if not self.complex_outputs:
            x = tf_to_k_complex(x)

        return x


def antifidelity(state_true, state_pred):
    # inner_prods = tf.einsum('bs,bs->b', tf.math.conj(state_true), state_pred)
    print(state_true.shape)
    print(state_pred.shape)
    state_true = k_to_tf_complex(state_true)
    state_pred = k_to_tf_complex(state_pred)
    inner_prods = tf.reduce_sum(tf.multiply(tf.math.conj(state_true), state_pred), 1)
    amplitudes = tf.abs(inner_prods)
    return tf.ones_like(amplitudes) - amplitudes ** 2


def load_model(filename):
    # TODO: this doesn't work yet -- there is a bug at tensorflow/python/keras/saving/hdf5_format.py:721
    custom_object_dict = {
        "SingleQubitOperationLayer": SingleQubitOperationLayer,
        "CPhaseLayer"              : CPhaseLayer
    }
    return keras.models.load_model(filename, custom_objects = custom_object_dict, compile = False)
