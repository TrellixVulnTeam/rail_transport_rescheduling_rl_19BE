#!/usr/bin/env python

import argparse
import collections
import json
import logging
import os
import pickle
import shelve
from pathlib import Path
import random
import yaml
import time

import gym
import numpy as np
import ray
from ray.rllib.agents.registry import get_agent_class
from ray.tune.registry import get_trainable_cls
from ray.rllib.env import MultiAgentEnv
from ray.rllib.env.base_env import _DUMMY_AGENT_ID
# from ray.rllib.evaluation.episode import _flatten_action # ray 0.8.4
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID
from ray.rllib.utils.space_utils import flatten_to_single_ndarray # ray 0.8.5
from ray.tune.utils import merge_dicts

from utils.loader import load_envs, load_models, load_algorithms
import wandb

logger = logging.getLogger(__name__)

EXAMPLE_USAGE = """
Example Usage:
    python rollout.py /Users/flaurent/Sites/flatland/flatland-checkpoints/checkpoint_940/checkpoint-940 --run APEX --no-render --episodes 1000 --env 'flatland_random_sparse_small' --config '{"env_config": {"test": "true", "min_seed": 1002, "max_seed": 213783, "min_test_seed": 0, "max_test_seed": 100, "reset_env_freq": "1", "regenerate_rail_on_reset": "True", "regenerate_schedule_on_reset": "True", "observation": "tree", "observation_config": {"max_depth": 2, "shortest_path_max_depth": 30}}, "model": {"fcnet_activation": "relu", "fcnet_hiddens": [256, 256], "vf_share_layers": "True"}}' 
"""

"""
# Testing in flatland_random_sparse_small:
python rollout.py /Users/flaurent/Sites/flatland/flatland-checkpoints/checkpoint_940/checkpoint-940 --run APEX --no-render --episodes 1000 --env 'flatland_random_sparse_small' --config '{"env_config": {"test": "true", "min_seed": 1002, "max_seed": 213783, "min_test_seed": 0, "max_test_seed": 100, "reset_env_freq": "1", "regenerate_rail_on_reset": "True", "regenerate_schedule_on_reset": "True", "observation": "tree", "observation_config": {"max_depth": 2, "shortest_path_max_depth": 30}}, "model": {"fcnet_activation": "relu", "fcnet_hiddens": [256, 256], "vf_share_layers": "True"}}' 

# Testing in flatland_sparse:
python rollout.py /Users/flaurent/Sites/flatland/flatland-checkpoints/checkpoint_940/checkpoint-940 --run APEX --no-render --episodes 1000 --env 'flatland_sparse' --config '{"env_config": {"test": "true", "generator": "sparse_rail_generator", "generator_config": "small_v0", "observation": "tree", "observation_config": {"max_depth": 2, "shortest_path_max_depth": 30}}, "model": {"fcnet_activation": "relu", "fcnet_hiddens": [256, 256], "vf_share_layers": "True"}}' 
"""

# Register all necessary assets in tune registries
load_envs(os.getcwd())  # Load envs
load_models(os.getcwd())  # Load models
from algorithms import CUSTOM_ALGORITHMS
load_algorithms(CUSTOM_ALGORITHMS)  # Load algorithms

from collections.abc import Mapping
from copy import deepcopy

# Default terminal state epsilon
# https://github.com/ray-project/ray/blob/master/rllib/agents/dqn/dqn.py
final_epsilon =  0.02
random.seed(1)

def val_replace(mapping):
    obj = deepcopy(mapping)
    if isinstance(mapping, Mapping):
        for key, val in mapping.items():
            obj[key] = val_replace(val)
    else:
        if mapping == "False":
            return False
        if mapping == "True":
            return True
        else:
            return mapping
    return obj


class RolloutSaver:
    """Utility class for storing rollouts.

    Currently supports two behaviours: the original, which
    simply dumps everything to a pickle file once complete,
    and a mode which stores each rollout as an entry in a Python
    shelf db file. The latter mode is more robust to memory problems
    or crashes part-way through the rollout generation. Each rollout
    is stored with a key based on the episode number (0-indexed),
    and the number of episodes is stored with the key "num_episodes",
    so to load the shelf file, use something like:

    with shelve.open('rollouts.pkl') as rollouts:
       for episode_index in range(rollouts["num_episodes"]):
          rollout = rollouts[str(episode_index)]

    If outfile is None, this class does nothing.
    """

    def __init__(self,
                 outfile=None,
                 use_shelve=False,
                 write_update_file=False,
                 target_steps=None,
                 target_episodes=None,
                 save_info=False):
        self._outfile = outfile
        self._update_file = None
        self._use_shelve = use_shelve
        self._write_update_file = write_update_file
        self._shelf = None
        self._num_episodes = 0
        self._rollouts = []
        self._current_rollout = []
        self._total_steps = 0
        self._target_episodes = target_episodes
        self._target_steps = target_steps
        self._save_info = save_info

    def _get_tmp_progress_filename(self):
        outpath = Path(self._outfile)
        return outpath.parent / ("__progress_" + outpath.name)

    @property
    def outfile(self):
        return self._outfile

    def __enter__(self):
        if self._outfile:
            if self._use_shelve:
                # Open a shelf file to store each rollout as they come in
                self._shelf = shelve.open(self._outfile)
            else:
                # Original behaviour - keep all rollouts in memory and save
                # them all at the end.
                # But check we can actually write to the outfile before going
                # through the effort of generating the rollouts:
                try:
                    with open(self._outfile, "wb") as _:
                        pass
                except IOError as x:
                    print("Can not open {} for writing - cancelling rollouts.".
                          format(self._outfile))
                    raise x
            if self._write_update_file:
                # Open a file to track rollout progress:
                self._update_file = self._get_tmp_progress_filename().open(
                    mode="w")
        return self

    def __exit__(self, type, value, traceback):
        if self._shelf:
            # Close the shelf file, and store the number of episodes for ease
            self._shelf["num_episodes"] = self._num_episodes
            self._shelf.close()
        elif self._outfile and not self._use_shelve:
            # Dump everything as one big pickle:
            pickle.dump(self._rollouts, open(self._outfile, "wb"))
        if self._update_file:
            # Remove the temp progress file:
            self._get_tmp_progress_filename().unlink()
            self._update_file = None

    def _get_progress(self):
        if self._target_episodes:
            return "{} / {} episodes completed".format(self._num_episodes,
                                                       self._target_episodes)
        elif self._target_steps:
            return "{} / {} steps completed".format(self._total_steps,
                                                    self._target_steps)
        else:
            return "{} episodes completed".format(self._num_episodes)

    def begin_rollout(self):
        self._current_rollout = []

    def end_rollout(self):
        if self._outfile:
            if self._use_shelve:
                # Save this episode as a new entry in the shelf database,
                # using the episode number as the key.
                self._shelf[str(self._num_episodes)] = self._current_rollout
            else:
                # Append this rollout to our list, to save laer.
                self._rollouts.append(self._current_rollout)
        self._num_episodes += 1
        if self._update_file:
            self._update_file.seek(0)
            self._update_file.write(self._get_progress() + "\n")
            self._update_file.flush()

    def append_step(self, obs, action, next_obs, reward, done, info):
        """Add a step to the current rollout, if we are saving them"""
        if self._outfile:
            if self._save_info:
                self._current_rollout.append(
                    [obs, action, next_obs, reward, done, info])
            else:
                self._current_rollout.append(
                    [obs, action, next_obs, reward, done])
        self._total_steps += 1


def create_parser(parser_creator=None):
    parser_creator = parser_creator or argparse.ArgumentParser
    parser = parser_creator(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Roll out a reinforcement learning agent "
                    "given a checkpoint.",
        epilog=EXAMPLE_USAGE)

    required_named = parser.add_argument_group("required named arguments")
    required_named.add_argument(
        "--checkpoint", type=str, help="Checkpoint from which to roll out.")
    required_named.add_argument(
        "--run",
        type=str,
        required=True,
        help="The algorithm or model to train. This may refer to the name "
             "of a built-on algorithm (e.g. RLLib's DQN or PPO), or a "
             "user-defined trainable function or class registered in the "
             "tune registry.")
    required_named.add_argument(
        "--env", type=str, help="The gym environment to use.")
    parser.add_argument(
        "--no-render",
        default=False,
        action="store_const",
        const=True,
        help="Surpress rendering of the environment.")
    parser.add_argument(
        "--monitor",
        default=False,
        action="store_const",
        const=True,
        help="Wrap environment in gym Monitor to record video.")
    parser.add_argument(
        "--steps", default=10000, help="Number of steps to roll out.")
    parser.add_argument("--out", default=None, help="Output filename.")
    parser.add_argument(
        "--config",
        default="{}",
        type=json.loads,
        help="Algorithm-specific configuration (e.g. env, hyperparams). "
             "Surpresses loading of configuration from checkpoint.")
    parser.add_argument(
        "--cfile",
        default="{}",
        type=str,
        help= "Load config from .pkl file."
            "Algorithm-specific configuration (e.g. env, hyperparams). "
             "Surpresses loading of configuration from checkpoint.")
    parser.add_argument(
        "--episodes",
        default=0,
        help="Number of complete episodes to roll out. (Overrides --steps)")
    parser.add_argument(
        "--save-info",
        default=False,
        action="store_true",
        help="Save the info field generated by the step() method, "
             "as well as the action, observations, rewards and done fields.")
    parser.add_argument(
        "--use-shelve",
        default=False,
        action="store_true",
        help="Save rollouts into a python shelf file (will save each episode "
             "as it is generated). An output filename must be set using --out.")
    parser.add_argument(
        "--track-progress",
        default=False,
        action="store_true",
        help="Write progress to a temporary file (updated "
             "after each episode). An output filename must be set using --out; "
             "the progress file will live in the same folder.")
    parser.add_argument(
        "--eager",
        action="store_true",
        help="Whether to attempt to enable TF eager execution.")
    return parser


def run(args, parser):
    config = {}
    # Load configuration from file
    config_dir = os.path.dirname(args.checkpoint)
    # config_path = os.path.join(config_dir, "params.pkl")
    config_path = args.cfile
    if not os.path.exists(config_path):
        config_path = os.path.join(config_dir, "../",args.cfile)
    if not os.path.exists(config_path):
        if not args.config:
            raise ValueError(
                "Could not find " + args.cfile + " in the checkpoint's parent dir.")
    else:
        with open(config_path, "rb") as f:
            config = yaml.safe_load(f)
        # with open(config_path, "rb") as f:
        #     config = pickle.load(f)
    if "num_workers" in config:
        config["num_workers"] = min(2, config["num_workers"])

    updated_config = val_replace(args.config)
    config = merge_dicts(config, updated_config)
    if not args.env:
        if not config.get("env"):
            parser.error("the following arguments are required: --env")
        args.env = config.get("env")

    wandb.init(config=config, project="rollout")
    ray.init()
    
    if args.eager:
        from tensorflow.python.framework.ops import enable_eager_execution
        enable_eager_execution()
        config['eager'] = True
    
    cls = get_trainable_cls(args.run)
    agent = cls(env=args.env, config=config)
    agent.restore(args.checkpoint)
    num_steps = int(args.steps)
    num_episodes = int(args.episodes)
    with RolloutSaver(
            args.out,
            args.use_shelve,
            write_update_file=args.track_progress,
            target_steps=num_steps,
            target_episodes=num_episodes,
            save_info=args.save_info) as saver:
        outcome = rollout(agent, args.env, num_steps, num_episodes, saver,
                          args.no_render, args.monitor)
        outcome_file = os.path.join(os.path.dirname(config_path), 'test_outcome.json')
        with open(outcome_file, 'w') as f:
            json.dump(outcome, f, indent=4)


class DefaultMapping(collections.defaultdict):
    """default_factory now takes as an argument the missing key."""

    def __missing__(self, key):
        self[key] = value = self.default_factory(key)
        return value


def default_policy_agent_mapping(unused_agent_id):
    return DEFAULT_POLICY_ID


def keep_going(steps, num_steps, episodes, num_episodes):
    """Determine whether we've collected enough data"""
    # if num_episodes is set, this overrides num_steps
    if num_episodes:
        return episodes < num_episodes
    # if num_steps is set, continue until we reach the limit
    if num_steps:
        return steps < num_steps
    # otherwise keep going forever
    return True


def rollout(agent,
            env_name,
            num_steps,
            num_episodes=0,
            saver=None,
            no_render=True,
            monitor=False):
    policy_agent_mapping = default_policy_agent_mapping

    if saver is None:
        saver = RolloutSaver()

    if hasattr(agent, "workers"):
        env = agent.workers.local_worker().env
        multiagent = isinstance(env, MultiAgentEnv)
        if agent.workers.local_worker().multiagent:
            policy_agent_mapping = agent.config["multiagent"][
                "policy_mapping_fn"]

        policy_map = agent.workers.local_worker().policy_map
        state_init = {p: m.get_initial_state() for p, m in policy_map.items()}
        use_lstm = {p: len(s) > 0 for p, s in state_init.items()}
        action_init = {
            p: flatten_to_single_ndarray(m.action_space.sample()) # ray 0.8.5
            # p: _flatten_action(m.action_space.sample()) # ray 0.8.4
            for p, m in policy_map.items()
        }
    else:
        env = gym.make(env_name)
        multiagent = False
        use_lstm = {DEFAULT_POLICY_ID: False}

    if monitor and not no_render and saver and saver.outfile is not None:
        # If monitoring has been requested,
        # manually wrap our environment with a gym monitor
        # which is set to record every episode.
        env = gym.wrappers.Monitor(
            env, os.path.join(os.path.dirname(saver.outfile), "monitor"),
            video_callable=lambda x: True, force=True)

    steps = 0
    episodes = 0
    simulation_rewards = []
    simulation_rewards_normalized = []
    simulation_percentage_complete = []
    simulation_steps = []
    start = time.time()
    times = []

    while keep_going(steps, num_steps, episodes, num_episodes):
        mapping_cache = {}  # in case policy_agent_mapping is stochastic
        saver.begin_rollout()
        obs = env.reset()
        agent_states = DefaultMapping(
            lambda agent_id: state_init[mapping_cache[agent_id]])
        prev_actions = DefaultMapping(
            lambda agent_id: action_init[mapping_cache[agent_id]])
        prev_rewards = collections.defaultdict(lambda: 0.)
        done = False
        reward_total = 0.0

        episode_steps = 0
        episode_max_steps = 0
        episode_num_agents = 0
        agents_score = collections.defaultdict(lambda: 0.)
        agents_done = set()
        start_time = time.time()

        while not done and keep_going(steps, num_steps, episodes,
                                      num_episodes):
            multi_obs = obs if multiagent else {_DUMMY_AGENT_ID: obs}
            action_dict = {}
            for agent_id, a_obs in multi_obs.items():
                if a_obs is not None:
                    policy_id = mapping_cache.setdefault(
                        agent_id, policy_agent_mapping(agent_id))
                    p_use_lstm = use_lstm[policy_id]
                    if p_use_lstm:
                        a_action, p_state, _ = agent.compute_action(
                            a_obs,
                            state=agent_states[agent_id],
                            prev_action=prev_actions[agent_id],
                            prev_reward=prev_rewards[agent_id],
                            policy_id=policy_id)
                        agent_states[agent_id] = p_state
                    else:
                        a_action = agent.compute_action(
                            a_obs,
                            prev_action=prev_actions[agent_id],
                            prev_reward=prev_rewards[agent_id],
                            policy_id=policy_id)
                    a_action = flatten_to_single_ndarray(a_action)  # ray 0.8.5
                    # a_action = _flatten_action(a_action)  # tuple actions # ray 0.8.4

                    # Epsilon-greedy action selection for APEX
                    if hasattr(agent, '_name'):
                        if agent._name == "APEX":
                            if random.random() <= final_epsilon:
                                a_action = random.choice(np.arange(env.action_space.n))

                    action_dict[agent_id] = a_action
                    prev_actions[agent_id] = a_action
            action = action_dict

            action = action if multiagent else action[_DUMMY_AGENT_ID]
            next_obs, reward, done, info = env.step(action)
            if multiagent:
                for agent_id, r in reward.items():
                    prev_rewards[agent_id] = r
            else:
                prev_rewards[_DUMMY_AGENT_ID] = reward

            if multiagent:
                done = done["__all__"]
                reward_total += sum(reward.values())
            else:
                reward_total += reward
            if not no_render:
                env.render()
            saver.append_step(obs, action, next_obs, reward, done, info)
            steps += 1
            obs = next_obs

            for agent_id, agent_info in info.items():
                if episode_max_steps == 0:
                    episode_max_steps = agent_info["max_episode_steps"]
                    episode_num_agents = agent_info["num_agents"]
                episode_steps = max(episode_steps, agent_info["agent_step"])
                agents_score[agent_id] = agent_info["agent_score"]
                if agent_info["agent_done"]:
                    agents_done.add(agent_id)

        episode_score = sum(agents_score.values())
        simulation_rewards.append(episode_score)
        simulation_rewards_normalized.append(episode_score / (episode_max_steps * episode_num_agents))
        simulation_percentage_complete.append(float(len(agents_done)) / episode_num_agents)
        simulation_steps.append(episode_steps)
        end_time = time.time()
        times.append(end_time - start_time)

        saver.end_rollout()
        wandb.log({'Episode':episodes, 'score': episode_score, 'normalized_score':simulation_rewards_normalized[-1],
         'percentage_complete': simulation_percentage_complete[-1], 
         'time_this_iter': end_time - start_time, 'cum_time': end_time - start})

        print(f"Episode #{episodes}: "
              f"score: {episode_score:.2f} "
              f"({np.mean(simulation_rewards):.2f}), "
              f"normalized score: {simulation_rewards_normalized[-1]:.2f} "
              f"({np.mean(simulation_rewards_normalized):.2f}), "
              f"percentage_complete: {simulation_percentage_complete[-1]:.2f} "
              f"({np.mean(simulation_percentage_complete):.2f})")
        if done:
            episodes += 1

    print("Evaluation completed:\n"
          f"Episodes: {episodes}\n"
          f"Mean Reward: {np.round(np.mean(simulation_rewards))}\n"
          f"Mean Normalized Reward: {np.round(np.mean(simulation_rewards_normalized))}\n"
          f"Mean Percentage Complete: {np.round(np.mean(simulation_percentage_complete), 3)}\n"
          f"Mean Steps: {np.round(np.mean(simulation_steps), 2)}")

    metric = {
        'reward': [float(r) for r in simulation_rewards],
        'reward_mean': np.mean(simulation_rewards),
        'reward_std': np.std(simulation_rewards),
        'normalized_reward': [float(r) for r in simulation_rewards_normalized],
        'normalized_reward_mean': np.mean(simulation_rewards_normalized),
        'normalized_reward_std': np.std(simulation_rewards_normalized),
        'percentage_complete': [float(c) for c in simulation_percentage_complete],
        'percentage_complete_mean': np.mean(simulation_percentage_complete),
        'percentage_complete_std': np.std(simulation_percentage_complete),
        'steps': [float(c) for c in simulation_steps],
        'steps_mean': np.mean(simulation_steps),
        'steps_std': np.std(simulation_steps),
        'time': [float(r) for r in times],
        'time_mean': np.mean(times),
        'time_std': np.std(times)
    }
    wandb.log(metric)
    return metric


if __name__ == "__main__":
    parser = create_parser()
    args = parser.parse_args()
    run(args, parser)
