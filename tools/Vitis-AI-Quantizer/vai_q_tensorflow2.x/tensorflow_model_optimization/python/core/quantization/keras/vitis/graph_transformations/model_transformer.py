# Copyright 2019 Xilinx Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# pylint: disable=g-explicit-length-test
"""Apply graph transformations to a tf.keras model."""

import collections
import copy
import re

import tensorflow as tf

from tensorflow_model_optimization.python.core.quantization.keras.vitis.graph_transformations import transforms as transforms_mod
from tensorflow_model_optimization.python.core.quantization.keras.vitis.utils import common_utils

LayerNode = transforms_mod.LayerNode

keras = tf.keras
K = tf.keras.backend
logger = common_utils.VAILogger


def _is_tf_version_above(major, minor):
  """Check if tensorflow version is above (or equal) to major.minor."""
  tf_version = tf.__version__.split('.')
  return int(tf_version[0]) == major and int(tf_version[1]) >= minor


class ModelTransformer(object):
  """Matches patterns to apply transforms in a tf.keras model graph."""

  def __init__(self,
               model,
               transforms,
               candidate_layers=None,
               layer_metadata=None):
    """Construct ModelTransformer.

    Args:
      model: Keras model to be transformed.
      transforms: List of transforms to be applied to the model.
      candidate_layers: Names of layers which may be transformed. Only layers
        whose names are in candidate_layers are matched against patterns. The
        default is that all layers may be transformed.
      layer_metadata: Dictionary of metadata associated with each layer in the
        model. The keys are layer names.
    """
    if not self._is_sequential_or_functional_model(model):
      raise ValueError(
          'Only tf.keras sequential or functional models can be transformed.')

    if layer_metadata is None:
      layer_metadata = {}

    self.model = model
    self.transforms = transforms
    self.candidate_layers = candidate_layers
    self.layer_metadata = layer_metadata

  @staticmethod
  def _is_sequential_or_functional_model(model):
    return ModelTransformer._is_functional_model(model) or isinstance(
        model, keras.Sequential)

  @staticmethod
  def _is_functional_model(model):
    return isinstance(model, keras.Model) \
           and not isinstance(model, keras.Sequential) \
           and model._is_graph_network    # pylint: disable=protected-access

  @staticmethod
  def _is_tf_op_lambda_layer(layer):
    """Check if the layer is a TFOpLambda layer."""
    return layer['class_name'] == 'TFOpLambda'

  def _get_consuming_layers(self, check_layer):
    """Returns all the layers which are out nodes from the layer."""
    consuming_layers = []
    for layer in self._config['layers']:
      for inbound_node in layer['inbound_nodes']:
        # Fix for TFOpLambda when tensorflow versions >=2.4
        if _is_tf_version_above(2, 4) and self._is_tf_op_lambda_layer(layer):
          if inbound_node[0] == self._get_layer_name(check_layer):
            consuming_layers.append(layer)
        else:
          for connection_info in inbound_node:
            if connection_info[0] == self._get_layer_name(check_layer):
              consuming_layers.append(layer)
    return consuming_layers

  def _get_output_consumers(self, check_layer):
    """Returns if any tensors from the layer are outputs of the model."""
    output_consumers = []
    for output_layer in self._config['output_layers']:
      if output_layer[0] == self._get_layer_name(check_layer):
        output_consumers.append(output_layer)
    return output_consumers

  @staticmethod
  def _get_layer_name(layer):
    # TensorFlowOpLayer's config name is different from layer name,
    # make sure to get the right layer name
    if layer['class_name'] == 'TensorFlowOpLayer':
      return layer['name']
    else:
      return layer['config']['name']

  def _get_layers(self, layer_names):
    """Returns layers with given names, keeps the order."""
    return [
        layer for layer in self._config['layers']
        if self._get_layer_name(layer) in layer_names
    ]

  def _get_layer_weights(self, layer):
    layer_name = self._get_layer_name(layer)
    weights = self._layer_weights_map.get(layer_name, {})
    return weights

  def _get_layer_metadata(self, layer_name):
    return self._layer_metadata_map.get(layer_name, {})

  def _match_pattern(self, target, pattern):
    for p in pattern.split('|'):
      if re.match('^' + p + '$', target) is not None:
        return True
    return False

  def _match_layer(self, layer, pattern):
    """Check if specific layer matches the pattern."""

    if self.candidate_layers and layer['config'][
        'name'] not in self.candidate_layers:
      return False

    if not self._match_pattern(layer['class_name'], pattern.class_name):
      return False

    layer_config = layer['config']
    for key, value in pattern.config.items():
      # This comparison should probably use the serialized value.
      # Allow regex for config value matching.
      if not (self._match_pattern(str(layer_config.get(key)), str(value))):
        return False

    return True

  def _is_match_supported(self, layer, is_head_node):
    """Check if ModelTransformer supports transformations given number of inputs and outputs at a layer.

    Args:
      layer: layer for pattern matching. Must come from a Functional model.
      is_head_node: whether this is the head node (e.g. in A -> B , B is the
        head node).

    Returns:
      whether match is supported.
    """

    inbound_nodes = layer['inbound_nodes']

    if len(inbound_nodes) > 1:
      # `layer` is re-used for more than 1 connection from previous layers. If
      # a pattern matches one set of inputs and is replaced, it will break the
      # other connection.
      #
      # Note that theoretically it's possible to have multiple connections have
      # exactly the same pattern, and in that case the transform might be
      # applied. But that's a very complicated edge case not worth handling.
      return False

    # If a layer has multiple inbound nodes, it will produce multiple outbound
    # connections as well. Hence no need to explicitly check that.

    consuming_layers = self._get_consuming_layers(layer)
    output_consumers = self._get_output_consumers(layer)
    if len(consuming_layers) + len(output_consumers) > 1:
      # Even if a layer has only 1 incoming connection, multiple layers may
      # still consume the output. Having multiple consumers is only supported
      # for the head node, and not intermediate layers. Replacing intermediate
      # nodes with >1 consumer will lead to dangling nodes.
      #
      # Note that theoretically, intermediate layers can supported, as a part
      # of a general layer transform tool. This is not supported given no
      # motivating use case.
      if not is_head_node:
        return False

    return True

  def _get_input_layer_names(self, layer):
    """Get the names of a layer's input layers."""
    if self._is_functional_model(self.model):
      inbound_nodes = layer['inbound_nodes']
      # Fix for TFOpLambda when tensorflow versions >=2.4
      input_layer_names = []
      for inbound_node in inbound_nodes:
        if _is_tf_version_above(2, 4) and self._is_tf_op_lambda_layer(layer):
          input_layer_names.append(inbound_node[0])
        else:
          for connection_info in inbound_node:
            input_layer_names.append(connection_info[0])
      return input_layer_names
    else:  # Sequential model.
      layers = self._config['layers']
      i = layers.index(layer)
      if i == 0:
        # First layer has no inputs.
        return []
      else:
        return [layers[i - 1]['config']['name']]

  def _match_layer_with_inputs(self, layer, pattern, is_head_node):
    """Match pattern at this layer, and continue to match at its inputs."""

    if not self._match_layer(layer, pattern):
      return None

    if self._is_functional_model(
        self.model) and not self._is_match_supported(layer, is_head_node):
      return None

    if len(pattern.inputs) == 0:
      # Leaf layer in pattern.
      return LayerNode(layer, self._get_layer_weights(layer), [],
                       self._get_layer_metadata(self._get_layer_name(layer)))

    # There is a possible edge case where a single layer may output multiple
    # tensors and multiple tensors from that layer may be used by the
    # connection. Ignoring those for now.
    input_layer_names = self._get_input_layer_names(layer)
    input_layers = self._get_layers(input_layer_names)

    if len(input_layers) != len(pattern.inputs):
      # Number of inputs this layer takes is different from the number of
      # inputs in the pattern.
      #
      # This path currently has the limitation that it requires an exact number
      # of inputs to match a pattern. For example, if a user wants to match
      # 2 Convs -> Concat and 3 Convs -> Concat, they would need to write
      # 2 different patterns.
      return None

    # Inbound layers can have different order from the list of input patterns.
    # TODO(pulkitb): Fix by checking all permutations.
    input_match_layer_nodes = []
    for input_layer, pattern_ in zip(input_layers, pattern.inputs):
      match_layer_node = self._match_layer_with_inputs(
          input_layer, pattern_, is_head_node=False)
      if not match_layer_node:
        return None
      input_match_layer_nodes.append(match_layer_node)

    return LayerNode(layer, self._get_layer_weights(layer),
                     input_match_layer_nodes,
                     self._get_layer_metadata(self._get_layer_name(layer)))

  def _find_pattern(self, pattern, matched_layers=None):
    for layer in self._config['layers']:
      if matched_layers and self._get_layer_name(layer) in matched_layers:
        continue
      match_layer = self._match_layer_with_inputs(
          layer, pattern, is_head_node=True)
      if match_layer:
        return match_layer

    return None

  def _get_leaf_layers(self, match_layer):
    """Return leaf layers from this sub-graph tree."""

    if not match_layer.input_layers:
      return [match_layer.layer]

    leaf_layers = []
    for inp in match_layer.input_layers:
      leaf_layers.extend(self._get_leaf_layers(inp))

    # Remove duplicate leaf layers in case of:
    # 1) Two different layers point to the same leaf layer
    # 2) One layer uses the same leaf layer multiple times

    uniq_leaf_layers = []
    for layer in leaf_layers:
      if layer not in uniq_leaf_layers:
        uniq_leaf_layers.append(layer)

    return uniq_leaf_layers

  @staticmethod
  def _get_layer_names(layer_node):
    result = [ModelTransformer._get_layer_name(layer_node.layer)]
    for input_layer in layer_node.input_layers:
      result.extend(ModelTransformer._get_layer_names(input_layer))
    return result

  def _remove_layers(self, layers_to_remove, layers_to_remove_names):
    # Remove layers.
    for layer_to_remove in layers_to_remove:
      self._config['layers'].remove(layer_to_remove)
    # Remove entry from weight and metadata maps,
    # now that layer has been removed.
    for layer_name in layers_to_remove_names:
      self._layer_weights_map.pop(layer_name, None)
      self._layer_metadata_map.pop(layer_name, None)

  def _replace(self, match_layer_node, replacement_layer_node):
    """Replace the tree or chain of match_layer_node with replacement_layer_node."""
    if self._is_functional_model(self.model):
      self._replace_functional(match_layer_node, replacement_layer_node)
    else:
      self._replace_sequential(match_layer_node, replacement_layer_node)

  def _replace_functional(self, match_layer_node, replacement_layer_node):
    """Functional model: replace the tree of match_layer_node with replacement_layer_node."""

    # 1. Point all consumers of the head of the matching sub-tree to the head
    # replacement layer.
    #
    # There are some assumptions baked in. The head layer only has 1 inbound and
    # outbound node. The resulting number and shape of tensors from the
    # replaced layer should equal the original layer.

    consuming_layers = self._get_consuming_layers(match_layer_node.layer)
    for consumer in consuming_layers:
      for inbound_node in consumer['inbound_nodes']:
        # Fix for TFOpLambda when tensorflow versions >=2.4
        if _is_tf_version_above(2, 4) and self._is_tf_op_lambda_layer(consumer):
          if inbound_node[0] == self._get_layer_name(match_layer_node.layer):
            inbound_node[0] = self._get_layer_name(replacement_layer_node.layer)
        else:
          for connection_info in inbound_node:
            if connection_info[0] == self._get_layer_name(
                match_layer_node.layer):
              connection_info[0] = self._get_layer_name(
                  replacement_layer_node.layer)

    output_consumers = self._get_output_consumers(match_layer_node.layer)
    for output_consumer in output_consumers:
      output_consumer[0] = self._get_layer_name(replacement_layer_node.layer)

    # 2. Create inbound nodes for the replacement layers. This connects all
    # the replacement layers.

    def _assign_inbounds_for_replacement(layer_node):
      """_assign_inbounds_for_replacement."""

      if not layer_node.input_layers:
        return

      layer_node.layer['inbound_nodes'] = [[]]
      for input_layer in layer_node.input_layers:
        # inbound_nodes can be specific tensors from multiple inbound
        # connections. We make the following assumptions.
        # - Only 1 inbound node for each replacement layer.
        # - Only 1 tensor output from the previous layer which is connected.
        # - call() method during construction does not have any args.
        # These are reasonable assumptions for almost all case we are
        # interested in.
        layer_node.layer['inbound_nodes'][0].append(
            [self._get_layer_name(input_layer.layer), 0, 0, {}])

        _assign_inbounds_for_replacement(input_layer)

    _assign_inbounds_for_replacement(replacement_layer_node)

    # 3. Connect the leaves of the replacement_layers to the inbound_nodes of
    # the leaves in the original layer.

    original_leaf_layers = self._get_leaf_layers(match_layer_node)
    #  original_inbound_nodes = [
    #      layer['inbound_nodes'] for layer in original_leaf_layers
    #  ]

    replacement_leaf_layers = self._get_leaf_layers(replacement_layer_node)

    # The original pattern and the replacement pattern can potentially have
    # different number of leaf nodes and differences in how they consume these
    # input layers. Matching them will require sophisticated hackery to recreate
    # the new layers with the original input structure.

    # Given our existing transforms, we can assume they match.

    if len(original_leaf_layers) != len(replacement_leaf_layers):
      raise RuntimeError('Different size of leaf layers not supported yet.')

    # TODO(Xiao) Currently we do not support generation of TFOpLambda layers,
    # as it needs extra configurations in inbound_nodes.
    # As TFOpLambda layers have different structure of inbound_nodes than normal layers,
    # we need to pay more attention to them:
    #   normal leaf layer -> normal leaf layer: copy inbound_nodes
    #   lambda leaf layer -> normal leaf layer: convert inbound_nodes, erase params
    #   normal leaf layer -> lambda leaf layer: convert inbound_nodes, need extra params (not supported)
    #   lambda leaf layer -> lambda leaf layer: copy inbound_nodes

    for original_leaf_layer, replacement_leaf_layer in zip(
        original_leaf_layers, replacement_leaf_layers):
      if self._is_tf_op_lambda_layer(replacement_leaf_layer):
        if self._is_tf_op_lambda_layer(original_leaf_layer):
          logger.debug(
              'Copy leaf_layer\'s inbound_nodes from TFOpLambda layer `{}` to `{}`'
              .format(original_leaf_layer, replacement_leaf_layer))
          replacement_leaf_layer['inbound_nodes'] = original_leaf_layer[
              'inbound_nodes']
        else:
          logger.error(
              'TFOpLambda layer `{}` is not supported as generated replacement leaf layer.'
              .format(replacement_leaf_layer))
      elif self._is_tf_op_lambda_layer(original_leaf_layer):
        replacement_leaf_layer['inbound_nodes'] = [
            original_leaf_layer['inbound_nodes']
        ]
        if _is_tf_version_above(2, 4):
          replacement_leaf_layer['inbound_nodes'][0][0][3] = {}
        else:
          replacement_leaf_layer['inbound_nodes'][0][0][0][3] = {}
      else:
        replacement_leaf_layer['inbound_nodes'] = original_leaf_layer[
            'inbound_nodes']

    # 4. Remove the original matched layers
    layers_to_remove_names = self._get_layer_names(match_layer_node)
    layers_to_remove = self._get_layers(layers_to_remove_names)

    self._remove_layers(layers_to_remove, layers_to_remove_names)

    # 5. Add in the new layers.
    def _add_replacement_layer(layer_node):
      """Recursively add new layers."""
      self._config['layers'].append(layer_node.layer)
      layer_name = self._get_layer_name(layer_node.layer)
      if layer_node.weights:
        self._layer_weights_map[layer_name] = layer_node.weights
      if layer_node.metadata:
        self._layer_metadata_map[layer_name] = layer_node.metadata
      if self.candidate_layers:
        self.candidate_layers.add(layer_name)

      for input_layer in layer_node.input_layers:
        _add_replacement_layer(input_layer)

    _add_replacement_layer(replacement_layer_node)

  def _replace_sequential(self, match_layer_node, replacement_layer_node):
    """Sequential model: replace the chain of match_layer_node with replacement_layer_node."""
    # 1. Remove the original matched layers.
    layers_to_remove_names = self._get_layer_names(match_layer_node)
    layers_to_remove = self._get_layers(layers_to_remove_names)

    # These variables are needed when adding the new layers
    # and must be set before _remove_layers removes them.
    first_layer_removed = layers_to_remove[0]
    first_layer_removed_index = self._config['layers'].index(
        first_layer_removed)

    self._remove_layers(layers_to_remove, layers_to_remove_names)

    # 2. Add in the new layers.
    def _get_replacement_nodes(replacement_node):
      """Get list of replacement nodes in Sequential order."""
      replacement_nodes = []

      for input_layer in replacement_node.input_layers:
        replacement_nodes.extend(_get_replacement_nodes(input_layer))

      replacement_nodes.append(replacement_node)

      return replacement_nodes

    def _add_replacement_nodes(first_layer_removed_index, replacement_nodes):
      """Add replacement nodes to Sequential model."""

      # Potentially insert nodes into middle of model.
      i = first_layer_removed_index
      for replacement_node in replacement_nodes:
        self._config['layers'].insert(i, replacement_node.layer)
        layer_name = self._get_layer_name(replacement_node.layer)
        if replacement_node.weights:
          self._layer_weights_map[layer_name] = replacement_node.weights
        if replacement_node.metadata:
          self._layer_metadata_map[layer_name] = replacement_node.metadata
        if self.candidate_layers:
          self.candidate_layers.add(layer_name)
        i += 1

    replacement_nodes = _get_replacement_nodes(replacement_layer_node)
    _add_replacement_nodes(first_layer_removed_index, replacement_nodes)

  @staticmethod
  def _is_custom_layer(layer):
    if layer is None:
      return False

    real_custom_objects = {"CustomOpWrapper", "Vitis>CustomOpWrapper"}
    custom_objects = tf.keras.utils.get_custom_objects()
    # TODO: get vitis_objects automatically
    vitis_objects = set([
        'Vitis>CustomOpWrapper', 'Vitis>NoQuantizeActivation',
        'Vitis>QuantizeAwareActivation', 'Vitis>QuantizeWrapper',
        'Vitis>LastValueMinMaxQuantizer', 'Vitis>MovingAvgMinMaxQuantizer',
        'Vitis>LastValueQuantPosQuantizer', 'Vitis>LastValueLogThQuantizer',
        'Vitis>VitisQuantizeConfig', 'Vitis>NoQuantizeConfig',
        'Vitis>VitisQuantize', 'Vitis>VitisSigmoid',
        'Vitis>VitisGlobalAveragePooling2D', 'Vitis>AveragePooling2D',
        'Vitis>VitisConvBN', 'Vitis>VitisConvBNQuantize',
        'Vitis>VitisDepthwiseConvBN', 'Vitis>VitisDepthwiseConvBNQuantize',
        'QuantizeAwareActivation', 'NoQuantizeActivation', 'QuantizeWrapper',
        'CustomOpWrapper', 'LastValueMinMaxQuantizer',
        'LastValueQuantPosQuantizer', 'LastValueLogThQuantizer',
        'VitisQuantizeConfig', 'NoQuantizeConfig', 'Vitis8BitQuantizeConfig',
        'VitisQuantize', 'QuantizeLayer', 'VitisSigmoid',
        'VitisAveragePooling2D', 'VitisGlobalAveragePooling2D'
    ])

    # remove "CustomOpWrapper", "Vitis>CustomOpWrapper" from vitis_objects
    vitis_objects = vitis_objects - real_custom_objects
    for cls in custom_objects:
      if cls not in vitis_objects:
        real_custom_objects.add(cls)

    if isinstance(layer, dict):
      cls_name = layer['class_name']
    else:
      cls_name = layer.__class__.__name__
    return (cls_name in real_custom_objects)

  @staticmethod
  def _weight_name(name, layer=None):
    """Extracts the weight name by removing layer from TF variable name.

    For example, returns 'kernel:0' for 'dense_2/kernel:0'.

    Args:
      name: TensorFlow variable name.
      layer: a keras layer or a layer config dict

    Returns:
      Extracted weight name.
    """
    if ModelTransformer._is_custom_layer(layer):
      return name
    else:
      return name.split('/')[-1]

  def _get_keras_layer_weights(self, keras_layer):
    """Returns a map of weight name, weight matrix. Keeps keras ordering."""
    weights_map = collections.OrderedDict()
    for weight_tensor, weight_numpy in \
        zip(keras_layer.weights, keras_layer.get_weights()):
      weights_map[self._weight_name(weight_tensor.name,
                                    keras_layer)] = weight_numpy

    return weights_map

  def _set_layer_weights(self, layer, weights_map):
    """Sets the values of weights in a Keras layer."""
    weight_value_tuples = []
    weight_names = []
    for weight_tensor in layer.weights:
      weight_names.append(self._weight_name(weight_tensor.name, layer))

    # for custom layer, if not specify arg name when init layer,
    # every time from_config operation will init a new layer
    if self._is_custom_layer(layer):
      logger.info("setting custom layer weights, layer name: {}".format(
          layer.name))
      weights_map_new = collections.OrderedDict()
      for weight_name_old, weight_name_new in zip(weights_map.keys(),
                                                  weight_names):
        weights_map_new[weight_name_new] = weights_map[weight_name_old]
      weights_map_old = weights_map
      weights_map = weights_map_new

    for weight_tensor in layer.weights:
      weight_name = self._weight_name(weight_tensor.name, layer)
      if weight_name in weights_map:
        weight_value_tuples.append((weight_tensor, weights_map[weight_name]))
    if weight_value_tuples == []:
      logger.warning("Got empty weight_value_tuples during set layer weights," \
              "layer name: {}".format(layer.name))

    K.batch_set_value(weight_value_tuples)

  @staticmethod
  def _name(obj):
    return obj.__class__.__name__

  def _get_matched_layers(self, transform):
    return self._transform_matched_layers_map.get(self._name(transform), [])

  def _store_successful_match(self, transform, layer_node):
    if self._name(transform) not in self._transform_matched_layers_map:
      self._transform_matched_layers_map[self._name(transform)] = []

    self._transform_matched_layers_map[self._name(transform)].append(
        self._get_layer_name(layer_node.layer))

  def transform(self):
    """Transforms the Keras model by applying all the specified transforms.

    This is the main entry point function used to apply the transformations to
    the Keras model.

    Not suitable for multi-threaded use. Creates and manipulates internal state.

    Returns:
      (Keras model after transformation, Updated layer metadata map)
    """

    # Gets a serialized dict representation of the model, containing all its
    # layers, their connections and configuration. This is the main structure
    # which is used to understand model structure, and also manipulate it.
    #
    # config = {
    #   'input_layers': [ ... ],
    #   'layers': [{
    #       'inbound_nodes': [INPUT CONFIG OF LAYER],
    #       'name': 'LAYER_NAME',
    #       'config': { LAYER_CONFIG }
    #     }, {
    #     ...
    #   }],
    #   'output_layers': [ ... ],
    #   'name': 'MODEL_NAME',
    #
    self._config = self.model.get_config()

    # Stores map of Transform -> List of layer names matched by transform.
    # Same transform should not match+replace the same layer more than once
    # to prevent infinite loops.
    self._transform_matched_layers_map = {}
    self._layer_weights_map = {}
    for layer in self.model.layers:
      self._layer_weights_map[layer.name] = self._get_keras_layer_weights(layer)

    # Maintains a current mutable copy of the metadata through transformation.
    self._layer_metadata_map = copy.deepcopy(self.layer_metadata)

    # We run an infinite loop and keep applying transformations as long as
    # patterns are found. This allows recursive pattern matching where a
    # modification by one transform may lead to another match.
    #
    # TODO(pulkitb): This leads to infinite loops with poor patterns which may
    # match their replacement. Add counters with limits to fix it.
    while True:
      match_found = False
      for transform in self.transforms:
        # A transform may find multiple instances of a pattern in the model.
        # Keep finding and replacing till done.
        while True:
          match_layer_node = self._find_pattern(
              transform.pattern(), self._get_matched_layers(transform))

          # Pattern did not match any layer. Move to next transform.
          if not match_layer_node:
            break

          self._store_successful_match(transform, match_layer_node)

          # Copying the match_layer_node ensures the replacement code can
          # freely modify the match.
          replacement_layer_node = transform.replacement(
              copy.deepcopy(match_layer_node))

          # If equal, the matched layers are being replaced with exactly the
          # same set of layers that were matched with the same config.
          # For Transforms, that may inadvertently do this we can end up in
          # an infinite loop. Move on if no meaningful change has been made.
          if match_layer_node == replacement_layer_node:
            continue

          match_found = True
          self._replace(match_layer_node, replacement_layer_node)

      # None of the transforms found a pattern. We can stop now.
      if not match_found:
        break

    custom_objects = {}
    for transform in self.transforms:
      custom_objects.update(transform.custom_objects())

    # Reconstruct model from the config, using the cloned layers.
    if self._is_functional_model(self.model):
      transformed_model = keras.Model.from_config(self._config, custom_objects)
    else:
      transformed_model = keras.Sequential.from_config(self._config,
                                                       custom_objects)

    for layer in transformed_model.layers:
      weights_map = self._layer_weights_map.get(layer.name)
      if weights_map:
        self._set_layer_weights(layer, weights_map)

    return transformed_model, copy.deepcopy(self._layer_metadata_map)

  def recursive_transform(self):
    """Transforms the Keras model recursively by applying all the specified transforms.

    This is the main entry point function used to apply the transformations to
    the Keras model.

    Not suitable for multi-threaded use. Creates and manipulates internal state.

    Returns:
      (Keras model after transformation, Updated layer metadata map)
    """

    def _make_transform_fn():

      def transform_fn(layer):
        if self._is_sequential_or_functional_model(layer):
          transformed_layer, self.layer_metadata = ModelTransformer(
              layer, self.transforms, self.candidate_layers,
              self.layer_metadata).transform()
          logger.debug('Transform nested model: {}'.format(layer.name))
          return transformed_layer
        else:
          return layer

      return transform_fn

    transformed_model = keras.models.clone_model(
        self.model, clone_function=_make_transform_fn())
    return ModelTransformer(transformed_model, self.transforms,
                            self.candidate_layers,
                            self.layer_metadata).transform()
