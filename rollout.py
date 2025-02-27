import argparse
import collections
import copy
import gym
from gym import wrappers as gym_wrappers
import json
import os
from pathlib import Path
import shelve
import numpy as np
import logging
import random

import ray
import ray.cloudpickle as cloudpickle
from ray.rllib.env import MultiAgentEnv
from ray.rllib.env.base_env import _DUMMY_AGENT_ID
from ray.rllib.env.env_context import EnvContext
from ray.rllib.evaluation.worker_set import WorkerSet
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID
from ray.rllib.utils.deprecation import deprecation_warning
from ray.rllib.utils.space_utils import flatten_to_single_ndarray
from ray.tune.utils import merge_dicts
from ray.tune.registry import get_trainable_cls, _global_registry, ENV_CREATOR

from utils.loader import load_envs, load_models, load_algorithms
import wandb
import yaml
import time

# Register all necessary assets in tune registries
load_envs(os.getcwd())  # Load envs
load_models(os.getcwd())  # Load models
from algorithms import CUSTOM_ALGORITHMS
load_algorithms(CUSTOM_ALGORITHMS)  # Load algorithms

logger = logging.getLogger(__name__)
final_epsilon =  0.02

EXAMPLE_USAGE = """
Example Usage via RLlib CLI:
    rllib rollout /tmp/ray/checkpoint_dir/checkpoint-0 --run DQN
    --env CartPole-v0 --steps 1000000 --out rollouts.pkl
Example Usage via executable:
    ./rollout.py /tmp/ray/checkpoint_dir/checkpoint-0 --run DQN
    --env CartPole-v0 --steps 1000000 --out rollouts.pkl
"""

# Note: if you use any custom models or envs, register them here first, e.g.:
#
# from ray.rllib.examples.env.parametric_actions_cartpole import \
#     ParametricActionsCartPole
# from ray.rllib.examples.model.parametric_actions_model import \
#     ParametricActionsModel
# ModelCatalog.register_custom_model("pa_model", ParametricActionsModel)
# register_env("pa_cartpole", lambda _: ParametricActionsCartPole(10))
from collections.abc import Mapping
from copy import deepcopy

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
            cloudpickle.dump(self._rollouts, open(self._outfile, "wb"))
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
    parser.add_argument(
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
        help="Suppress rendering of the environment.")
    parser.add_argument(
        "--monitor",
        default=False,
        action="store_true",
        help="Wrap environment in gym Monitor to record video. NOTE: This "
        "option is deprecated: Use `--video-dir [some dir]` instead.")
    parser.add_argument(
        "--video-dir",
        type=str,
        default=None,
        help="Specifies the directory into which videos of all episode "
        "rollouts will be stored.")
    parser.add_argument(
        "--steps",
        default=10000,
        help="Number of timesteps to roll out (overwritten by --episodes).")
    parser.add_argument(
        "--episodes",
        default=0,
        help="Number of complete episodes to roll out (overrides --steps).")
    parser.add_argument("--out", default=None, help="Output filename.")
    parser.add_argument(
        "--config",
        default="{}",
        type=json.loads,
        help="Algorithm-specific configuration (e.g. env, hyperparams). "
        "Gets merged with loaded configuration from checkpoint file and "
        "`evaluation_config` settings therein.")
    parser.add_argument(
        "--cfile",
        default="{}",
        type=str,
        help= "Load config from .pkl file."
            "Algorithm-specific configuration (e.g. env, hyperparams). "
             "Surpresses loading of configuration from checkpoint.")
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
        "--project",
        default="Rollout",
        help="Save rollouts to W&B project.")
    return parser


def run(args, parser):
    # Load configuration from checkpoint file.
    config_dir = os.path.dirname(args.checkpoint)
    # config_path = os.path.join(config_dir, "params.pkl")
    config_path = args.cfile
    # Try parent directory.
    if not os.path.exists(config_path):
        config_path = os.path.join(config_dir, "../", args.cfile)

    # If no yaml file found, require command line `--config`.
    if not os.path.exists(config_path):
        if not args.config:
            raise ValueError(
                "Could not find params.pkl in either the checkpoint dir or "
                "its parent directory AND no config given on command line!")
        else:
            config = args.config

    # Load the config from pickled.
    else:
        with open(config_path, "rb") as f:
            config = yaml.safe_load(f)

    # Set num_workers to be at least 2.
    if "num_workers" in config:
        config["num_workers"] = min(2, config["num_workers"])

    # Make sure worker 0 has an Env.
    # config["create_env_on_driver"] = True

    # Merge with `evaluation_config` (first try from command line, then from
    # pkl file).
    evaluation_config = copy.deepcopy(
        args.config.get("evaluation_config", config.get(
            "evaluation_config", {})))
    config = merge_dicts(config, evaluation_config)
    # Merge with command line `--config` settings (if not already the same
    # anyways).
    config = merge_dicts(config, args.config)

    if not args.env:
        if not config.get("env"):
            parser.error("the following arguments are required: --env")
        args.env = config.get("env")

    wandb.init(config=config, project=args.project)
    # print(wandb.run.dir)
    ray.init()

    # Create the Trainer from config.
    cls = get_trainable_cls(args.run)
    agent = cls(env=args.env, config=config)
    # Load state from checkpoint.
    agent.restore(args.checkpoint)
    num_steps = int(args.steps)
    num_episodes = int(args.episodes)

    # Determine the video output directory.
    # Deprecated way: Use (--out|~/ray_results) + "/monitor" as dir.
    video_dir = None
    if args.monitor:
        # video_dir = os.path.join(
        #     os.path.dirname(args.out or "")
        #     or os.path.expanduser("~/ray_results/"), "monitor")
        video_dir = os.path.join(wandb.run.dir, "media")
    # New way: Allow user to specify a video output path.
    elif args.video_dir:
        video_dir = os.path.expanduser(args.video_dir)

    # Do the actual rollout.
    with RolloutSaver(
            args.out,
            args.use_shelve,
            write_update_file=args.track_progress,
            target_steps=num_steps,
            target_episodes=num_episodes,
            save_info=args.save_info) as saver:
        rollout(agent, args.env, num_steps, num_episodes, saver,
                args.no_render, video_dir)
    agent.stop()


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
            video_dir=None):
    policy_agent_mapping = default_policy_agent_mapping

    if saver is None:
        saver = RolloutSaver()

    if hasattr(agent, "workers") and isinstance(agent.workers, WorkerSet):
        env = agent.workers.local_worker().env
        # env.render()
        multiagent = isinstance(env, MultiAgentEnv)
        if agent.workers.local_worker().multiagent:
            policy_agent_mapping = agent.config["multiagent"][
                "policy_mapping_fn"]
        policy_map = agent.workers.local_worker().policy_map
        state_init = {p: m.get_initial_state() for p, m in policy_map.items()}
        use_lstm = {p: len(s) > 0 for p, s in state_init.items()}
        # print(state_init)
    else:
        from gym import envs
        if envs.registry.env_specs.get(agent.config["env"]):
            # if environment is gym environment, load from gym
            env = gym.make(agent.config["env"])
        else:
            # if environment registered ray environment, load from ray
            env_creator = _global_registry.get(ENV_CREATOR,
                                               agent.config["env"])
            env_context = EnvContext(
                agent.config["env_config"] or {}, worker_index=0)
            env = env_creator(env_context)
        multiagent = False
        try:
            policy_map = {DEFAULT_POLICY_ID: agent.policy}
        except AttributeError:
            raise AttributeError(
                "Agent ({}) does not have a `policy` property! This is needed "
                "for performing (trained) agent rollouts.".format(agent))
        use_lstm = {DEFAULT_POLICY_ID: False}

    action_init = {
        p: flatten_to_single_ndarray(m.action_space.sample())
        for p, m in policy_map.items()
    }

    # If monitoring has been requested, manually wrap our environment with a
    # gym monitor, which is set to record every episode.
    if video_dir:
        env = gym_wrappers.Monitor(
            env=env,
            directory=video_dir,
            video_callable=lambda x: True,
            force=True)

    steps = 0
    episodes = 0
    simulation_rewards = []
    simulation_rewards_normalized = []
    simulation_percentage_complete = []
    simulation_steps = []
    start = time.time()
    times = []

    while keep_going(steps, num_steps, episodes, num_episodes):
        # print("policies: ")
        # for p, m in policy_map.items():
        #     print(p)
        #     print(m)
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
                    a_action = flatten_to_single_ndarray(a_action)

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
                reward_total += sum(
                    r for r in reward.values() if r is not None)
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

        # print("Episode #{}: reward: {}".format(episodes, reward_total))
        print(f"Episode #{episodes}: "
              f"score: {episode_score:.5f} "
              f"({np.mean(simulation_rewards):.5f}), "
              f"normalized score: {simulation_rewards_normalized[-1]:.5f} "
              f"({np.mean(simulation_rewards_normalized):.5f}), "
              f"percentage_complete: {simulation_percentage_complete[-1]:.5f} "
              f"({np.mean(simulation_percentage_complete):.5f})")
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
    # print(video_dir)
    if args.video_dir:
        wandb.save(video_dir)

if __name__ == "__main__":
    parser = create_parser()
    args = parser.parse_args()

    # Old option: monitor, use video-dir instead.
    if args.monitor:
        deprecation_warning("--monitor", "--video-dir=[some dir]")
    # User tries to record videos, but no-render is set: Error.
    if (args.monitor or args.video_dir) and args.no_render:
        raise ValueError(
            "You have --no-render set, but are trying to record rollout videos"
            " (via options --video-dir/--monitor)! "
            "Either unset --no-render or do not use --video-dir/--monitor.")
    # --use_shelve w/o --out option.
    if args.use_shelve and not args.out:
        raise ValueError(
            "If you set --use-shelve, you must provide an output file via "
            "--out as well!")
    # --track-progress w/o --out option.
    if args.track_progress and not args.out:
        raise ValueError(
            "If you set --track-progress, you must provide an output file via "
            "--out as well!")

    run(args, parser)
    