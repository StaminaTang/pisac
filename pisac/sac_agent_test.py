# coding=utf-8
# Copyright 2020 The PI-SAC Authors.
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
"""Tests for pisac.sac_agent."""

from pisac import sac_agent
import tensorflow as tf  # pylint: disable=g-explicit-tensorflow-version-import

from tf_agents.agents.ddpg import critic_rnn_network
from tf_agents.networks import actor_distribution_rnn_network
from tf_agents.networks import network
from tf_agents.specs import tensor_spec
from tf_agents.trajectories import time_step as ts
from tf_agents.trajectories import trajectory
from tf_agents.trajectories.policy_step import PolicyStep
from tf_agents.utils import common
from tf_agents.utils import test_utils


class _MockDistribution(object):

  def __init__(self, action):
    self._action = action

  def sample(self):
    return self._action

  def log_prob(self, unused_sample):
    return tf.constant(10., shape=[1])


class DummyActorPolicy(object):

  def __init__(self,
               time_step_spec,
               action_spec,
               actor_network,
               training=False):
    del time_step_spec
    del actor_network
    del training
    single_action_spec = tf.nest.flatten(action_spec)[0]
    # Action is maximum of action range.
    self._action = single_action_spec.maximum
    self._action_spec = action_spec
    self.info_spec = ()

  def action(self, time_step):
    observation = time_step.observation
    batch_size = observation.shape[0]
    action = tf.constant(self._action, dtype=tf.float32, shape=[batch_size, 1])
    return PolicyStep(action=action)

  def distribution(self, time_step, policy_state=()):
    del policy_state
    action = self.action(time_step).action
    return PolicyStep(action=_MockDistribution(action))

  def get_initial_state(self, batch_size):
    del batch_size
    return ()


class DummyCriticNet(network.Network):

  def __init__(self, l2_regularization_weight=0.0, shared_layer=None):
    super(DummyCriticNet, self).__init__(
        input_tensor_spec=(tensor_spec.TensorSpec([2], tf.float32),
                           tensor_spec.TensorSpec([1], tf.float32)),
        state_spec=(),
        name=None)
    self._l2_regularization_weight = l2_regularization_weight
    self._value_layer = tf.keras.layers.Dense(
        1,
        kernel_regularizer=tf.keras.regularizers.l2(l2_regularization_weight),
        kernel_initializer=tf.compat.v1.initializers.constant([[0], [1]]),
        bias_initializer=tf.compat.v1.initializers.constant([[0]]))
    self._shared_layer = shared_layer
    self._action_layer = tf.keras.layers.Dense(
        1,
        kernel_regularizer=tf.keras.regularizers.l2(l2_regularization_weight),
        kernel_initializer=tf.compat.v1.initializers.constant([[1]]),
        bias_initializer=tf.compat.v1.initializers.constant([[0]]))

  def copy(self, name=''):
    del name
    return DummyCriticNet(
        l2_regularization_weight=self._l2_regularization_weight,
        shared_layer=self._shared_layer)

  def call(self, inputs, step_type, network_state=()):
    del step_type
    observation, actions = inputs
    actions = tf.cast(tf.nest.flatten(actions)[0], tf.float32)

    states = tf.cast(tf.nest.flatten(observation)[0], tf.float32)

    s_value = self._value_layer(states)
    if self._shared_layer:
      s_value = self._shared_layer(s_value)
    a_value = self._action_layer(actions)
    # Biggest state is best state.
    q_value = tf.reshape(s_value + a_value, [-1])
    return q_value, network_state


class SacAgentTest(test_utils.TestCase):

  def setUp(self):
    super(SacAgentTest, self).setUp()
    self._obs_spec = tensor_spec.TensorSpec([2], tf.float32)
    self._time_step_spec = ts.time_step_spec(self._obs_spec)
    self._action_spec = tensor_spec.BoundedTensorSpec([1], tf.float32, -1, 1)

  def testCreateAgent(self):
    sac_agent.SacAgent(
        self._time_step_spec,
        self._action_spec,
        critic_network=DummyCriticNet(),
        actor_network=None,
        actor_optimizer=None,
        critic_optimizer=None,
        alpha_optimizer=None,
        actor_policy_ctor=DummyActorPolicy)

  def testCriticLoss(self):
    agent = sac_agent.SacAgent(
        self._time_step_spec,
        self._action_spec,
        critic_network=DummyCriticNet(),
        actor_network=None,
        actor_optimizer=None,
        critic_optimizer=None,
        alpha_optimizer=None,
        actor_policy_ctor=DummyActorPolicy)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([[5], [6]], dtype=tf.float32)

    rewards = tf.constant([10, 20], dtype=tf.float32)
    discounts = tf.constant([0.9, 0.9], dtype=tf.float32)
    next_observations = tf.constant([[5, 6], [7, 8]], dtype=tf.float32)
    next_time_steps = ts.transition(next_observations, rewards, discounts)

    td_targets = [7.3, 19.1]
    pred_td_targets = [7., 10.]

    self.evaluate(tf.compat.v1.global_variables_initializer())

    # Expected critic loss has factor of 2, for the two TD3 critics.
    expected_loss = self.evaluate(2 * tf.compat.v1.losses.mean_squared_error(
        tf.constant(td_targets), tf.constant(pred_td_targets)))

    loss = agent.critic_loss(
        time_steps,
        actions,
        next_time_steps,
        td_errors_loss_fn=tf.math.squared_difference)

    self.evaluate(tf.compat.v1.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  def testCriticLossQAug(self):
    agent = sac_agent.SacAgent(
        self._time_step_spec,
        self._action_spec,
        critic_network=DummyCriticNet(),
        actor_network=None,
        actor_optimizer=None,
        critic_optimizer=None,
        alpha_optimizer=None,
        actor_policy_ctor=DummyActorPolicy)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)
    actions = tf.constant([[5], [6]], dtype=tf.float32)

    rewards = tf.constant([10, 20], dtype=tf.float32)
    discounts = tf.constant([0.9, 0.9], dtype=tf.float32)
    next_observations = tf.constant([[5, 6], [7, 8]], dtype=tf.float32)
    next_time_steps = ts.transition(next_observations, rewards, discounts)

    td_targets = [7.3, 19.1]
    pred_td_targets = [7., 10.]

    self.evaluate(tf.compat.v1.global_variables_initializer())

    # Expected critic loss has factor of 2, for the two TD3 critics.
    expected_loss = self.evaluate(2 * tf.compat.v1.losses.mean_squared_error(
        tf.constant(td_targets), tf.constant(pred_td_targets)))

    loss = agent.critic_loss_q_aug(
        time_steps,
        actions,
        next_time_steps,
        target_obs=next_observations,
        td_errors_loss_fn=tf.math.squared_difference)

    self.evaluate(tf.compat.v1.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  def testActorLoss(self):
    agent = sac_agent.SacAgent(
        self._time_step_spec,
        self._action_spec,
        critic_network=DummyCriticNet(),
        actor_network=None,
        actor_optimizer=None,
        critic_optimizer=None,
        alpha_optimizer=None,
        actor_policy_ctor=DummyActorPolicy)

    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)

    expected_loss = (2 * 10 - (2 + 1) - (4 + 1)) / 2
    loss = agent.actor_loss(time_steps)

    self.evaluate(tf.compat.v1.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  def testAlphaLoss(self):
    agent = sac_agent.SacAgent(
        self._time_step_spec,
        self._action_spec,
        critic_network=DummyCriticNet(),
        actor_network=None,
        actor_optimizer=None,
        critic_optimizer=None,
        alpha_optimizer=None,
        target_entropy=3.0,
        initial_log_alpha=4.0,
        actor_policy_ctor=DummyActorPolicy)
    observations = tf.constant([[1, 2], [3, 4]], dtype=tf.float32)
    time_steps = ts.restart(observations, batch_size=2)

    expected_loss = 4.0 * (-10 - 3)
    loss = agent.alpha_loss(time_steps)

    self.evaluate(tf.compat.v1.global_variables_initializer())
    loss_ = self.evaluate(loss)
    self.assertAllClose(loss_, expected_loss)

  def testPolicy(self):
    agent = sac_agent.SacAgent(
        self._time_step_spec,
        self._action_spec,
        critic_network=DummyCriticNet(),
        actor_network=None,
        actor_optimizer=None,
        critic_optimizer=None,
        alpha_optimizer=None,
        actor_policy_ctor=DummyActorPolicy)

    observations = tf.constant([[1, 2]], dtype=tf.float32)
    time_steps = ts.restart(observations)
    action_step = agent.policy.action(time_steps)

    self.evaluate(tf.compat.v1.global_variables_initializer())
    action_ = self.evaluate(action_step.action)
    self.assertLessEqual(action_, self._action_spec.maximum)
    self.assertGreaterEqual(action_, self._action_spec.minimum)

  def testTrainWithRnn(self):
    actor_net = actor_distribution_rnn_network.ActorDistributionRnnNetwork(
        self._obs_spec,
        self._action_spec,
        input_fc_layer_params=None,
        output_fc_layer_params=None,
        conv_layer_params=None,
        lstm_size=(40,),
    )

    critic_net = critic_rnn_network.CriticRnnNetwork(
        (self._obs_spec, self._action_spec),
        observation_fc_layer_params=(16,),
        action_fc_layer_params=(16,),
        joint_fc_layer_params=(16,),
        lstm_size=(16,),
        output_fc_layer_params=None,
    )

    counter = common.create_variable('test_train_counter')

    optimizer_fn = tf.compat.v1.train.AdamOptimizer

    agent = sac_agent.SacAgent(
        self._time_step_spec,
        self._action_spec,
        critic_network=critic_net,
        actor_network=actor_net,
        actor_optimizer=optimizer_fn(1e-3),
        critic_optimizer=optimizer_fn(1e-3),
        alpha_optimizer=optimizer_fn(1e-3),
        train_sequence_length=None,  # for RNN
        auto_step=True,  # enable auto step
        train_step_counter=counter,
    )

    batch_size = 5
    observations = tf.constant(
        [[[1, 2], [3, 4], [5, 6]]] * batch_size, dtype=tf.float32)
    actions = tf.constant([[[0], [1], [1]]] * batch_size, dtype=tf.float32)
    time_steps = ts.TimeStep(
        step_type=tf.constant([[1] * 3] * batch_size, dtype=tf.int32),
        reward=tf.constant([[1] * 3] * batch_size, dtype=tf.float32),
        discount=tf.constant([[1] * 3] * batch_size, dtype=tf.float32),
        observation=observations)

    experience = trajectory.Trajectory(
        time_steps.step_type, observations, actions, (),
        time_steps.step_type, time_steps.reward, time_steps.discount)

    # Force variable creation.
    agent.policy.variables()

    if not tf.executing_eagerly():
      # Get experience first to make sure optimizer variables are created and
      # can be initialized.
      experience = agent.train(experience)
      with self.cached_session() as sess:
        common.initialize_uninitialized_variables(sess)
      self.assertEqual(self.evaluate(counter), 0)
      self.evaluate(experience)
      self.assertEqual(self.evaluate(counter), 1)
    else:
      self.assertEqual(self.evaluate(counter), 0)
      self.evaluate(agent.train(experience))
      self.assertEqual(self.evaluate(counter), 1)


if __name__ == '__main__':
  tf.test.main()
