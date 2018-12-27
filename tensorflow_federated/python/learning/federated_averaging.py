# Copyright 2018, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""An implementation of the Federated Averaging algorithm.

Based on the paper:

Communication-Efficient Learning of Deep Networks from Decentralized Data
    H. Brendan McMahan, Eider Moore, Daniel Ramage,
    Seth Hampson, Blaise Aguera y Arcas. AISTATS 2017.
    https://arxiv.org/abs/1602.05629
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections

# Dependency imports

import tensorflow as tf

from tensorflow.python.util import nest
from tensorflow_federated.python.common_libs import py_typecheck
from tensorflow_federated.python.learning import model_utils
from tensorflow_federated.python.learning.framework import optimizer_utils
from tensorflow_federated.python.tensorflow_libs import tensor_utils


class ClientFedAvg(optimizer_utils.ClientDeltaFn):
  """Client TensorFlow logic for Federated Averaging."""

  def __init__(self, model, client_weight_fn=None):
    """Creates the client computation for Federated Averaging.

    Args:
      model: A `learning.TrainableModel`.
      client_weight_fn: Optional function that takes the output
        of model.aggregated_outputs() and returns a tensor that provides
        the weight in the federated average of model deltas. If not provided,
        the default is the total number of examples processed on device.
    """
    self._model = model_utils.enhance(model)
    py_typecheck.check_type(self._model, model_utils.EnhancedTrainableModel)

    self._num_examples = tf.Variable(0, name='num_examples')
    if client_weight_fn is not None:
      py_typecheck.check_callable(client_weight_fn)
      self._client_weight_fn = client_weight_fn
    else:
      self._client_weight_fn = lambda _: tf.cast(self._num_examples, tf.float32)

  @property
  def variables(self):
    return [self._num_examples]

  @tf.contrib.eager.function(autograph=False)
  def __call__(self, dataset, initial_weights):
    # N.B. When not in eager mode, this code must be wrapped as a defun
    # as it uses program-order semantics to avoid adding many explicit
    # control dependencies.
    model = self._model
    py_typecheck.check_type(dataset, tf.data.Dataset)

    # TODO(b/120801384): We should initialize model.local_variables here.
    # Or, we may just need a convention that TFF initializes all variables
    # before invoking the TF function.

    nest.map_structure(tf.assign, model.weights, initial_weights)

    @tf.contrib.eager.function(autograph=False)
    def reduce_fn(dummy_state, batch):
      """Runs train_on_batch on batch."""
      output = model.train_on_batch(batch)
      tf.assign_add(self._num_examples, tf.shape(output.predictions)[0])
      return dummy_state

    # TODO(b/121400757): Remove dummy_output when bug fixed.
    dummy_output = dataset.reduce(
        initial_state=tf.constant(0.0), reduce_func=reduce_fn)

    weights_delta = nest.map_structure(tf.subtract, model.weights.trainable,
                                       initial_weights.trainable)

    aggregated_outputs = model.aggregated_outputs()
    weights_delta_weight = self._client_weight_fn(aggregated_outputs)  # pylint:disable=not-callable

    # TODO(b/122071074): Consider moving this functionality into
    # federated_averaging?
    weights_delta, has_non_finite_delta = (
        tensor_utils.zero_all_if_any_non_finite(weights_delta))
    weights_delta_weight = tf.cond(
        tf.equal(has_non_finite_delta,
                 0), lambda: weights_delta_weight, lambda: tf.constant(0.0))

    return optimizer_utils.ClientOutput(
        weights_delta, weights_delta_weight, aggregated_outputs,
        tensor_utils.to_odict({
            'num_examples': self._num_examples.value(),
            'has_non_finite_delta': has_non_finite_delta,
            'workaround for b/121400757': dummy_output,
        }))


#
# Server TF computations
#


def _create_optimizer_and_server_state(model, optimizer):
  """A helper that constructs the model and optimizer.

  This code is needed both in server_init (to introduce variables so
  we can read off there initial values) and in server_update_model.

  Args:
    model: A `tff.learning.Model`.
    optimizer: A `tf.train.Optimizer`.

  Returns:
    A tuple of (apply_delta_fn, server_state), where:
      *  apply_delta_fn is a tensorflow function that takes a model delta and
         updates the model variables as well as possibly optimizer_state
         variables introduced by the optimizer.
      *  server_state is a `ServerState` tuple holding those variables.
  """

  @tf.contrib.eager.defun(autograph=False)
  def apply_delta(delta):
    """Applies delta to model.weights."""
    nest.assert_same_structure(delta, model.weights.trainable)
    grads_and_vars = nest.map_structure(
        lambda x, v: (-1.0 * x, v), nest.flatten(delta),
        nest.flatten(model.weights.trainable))
    # N.B. This may create variables.
    # TODO(b/109733734): In TF 2, you shouldn't create variables
    # inside a defun. Perhaps use Keras optimizers or OptimizerV2?
    optimizer.apply_gradients(grads_and_vars, name='server_update')
    return tf.constant(1)  # We have to return something.

  # Create a dummy input and trace apply_delta so that
  # we can determine the optimizers variables.
  weights_delta = nest.map_structure(tf.zeros_like, model.weights.trainable)

  # TODO(b/109733734): We would like to call get_concrete_function,
  # but that does not currently work with structured inputs.
  # For now, we just call the function on dummy input, which
  # still ensures the function is traced (so variables are created).
  apply_delta(delta=weights_delta)

  # N.B. Using to_var_dict doesn't work here, because we
  # may get different names.
  optimizer_vars = optimizer.variables()

  return apply_delta, ServerState(model=model.weights,
                                  optimizer_state=optimizer_vars)


# Represents the state of the server carried between rounds.
ServerState = collections.namedtuple(
    'ServerState',
    [
        # A ModelWeights structure, containing Tensors or Variables.
        'model',
        # A list of Tensors or Variables, in the order
        # returned by optimizer.variables()
        'optimizer_state'
    ])


def server_init(model_fn, optimizer_fn):
  """Returns initial `ServerState`.

  Args:
    model_fn: A no-arg function that returns a `tff.learning.Model`.
    optimizer_fn: A no-arg function that returns a `tf.train.Optimizer`.
      Returns A `ServerState` namedtuple.
  """
  model = model_utils.enhance(model_fn())  # Constructs variables
  optimizer = optimizer_fn()  # Might create variables?
  _, server_state = _create_optimizer_and_server_state(model, optimizer)
  return server_state


def server_update_model(server_state, weights_delta, model_fn, optimizer_fn):
  """Updates `server_state` based on `weights_delta`.

  Args:
    server_state: A `ServerState` namedtuple.
    weights_delta: An update to the trainable variables of the model.
    model_fn: A no-arg function that returns a `tff.learning.Model`. Passing in
      a function ensures any variables are created when server_update_model is
      called, so they can be captured in a specific graph or other context.
    optimizer_fn: A no-arg function that returns a `tf.train.Optimizer`. As with
      model_fn, we pass in a function to control when variables are created.

  Returns:
    An updated `ServerState` namedtuple.
  """
  model = model_utils.enhance(model_fn())  # Constructs variables
  optimizer = optimizer_fn()  # Might create variables?
  apply_delta_fn, server_vars = _create_optimizer_and_server_state(
      model, optimizer)

  @tf.contrib.eager.function(autograph=False)
  def update_model_inner():
    nest.map_structure(tf.assign, server_vars, server_state)
    apply_delta_fn(weights_delta)
    return server_vars

  return update_model_inner()
