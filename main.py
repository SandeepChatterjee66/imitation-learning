from collections import deque

import hydra
import numpy as np
from omegaconf import DictConfig
import torch
from torch import optim
from tqdm import tqdm

from environments import ENVS
from evaluation import evaluate_agent
from models import Actor, ActorCritic, AIRLDiscriminator, GAILDiscriminator, GMMILDiscriminator, REDDiscriminator
from training import TransitionDataset, adversarial_imitation_update, behavioural_cloning_update, indicate_absorbing, ppo_update, target_estimation_update
from utils import flatten_list_dicts, lineplot

# TODO: Sweeper: New strat: We sweep all the PPO parameters TOGETHER WITH IL algorithm, hence move hydra ax sweeper param in hydra opt to IL.yaml (for each algo)
# TODO: Sweeper: Add all IL sweeper parameter to hyper_param_opt folder, AND REMOVE PPO AND MOVE IT IN TO THOSE
# TODO: rename agent_learning_rate to agent_learning_rate & learning_rate to imitation_learning_rate
# Setup
"""
parser = argparse.ArgumentParser(description='IL')
parser.add_argument('--seed', type=int, default=1, metavar='S', help='Random seed')
parser.add_argument('--steps', type=int, default=100000, metavar='T', help='Number of environment steps')
parser.add_argument('--hidden-size', type=int, default=32, metavar='H', help='Hidden size')
parser.add_argument('--discount', type=float, default=0.99, metavar='γ', help='Discount')
parser.add_argument('--trace-decay', type=float, default=0.95, metavar='λ', help='GAE trace decay')
parser.add_argument('--ppo-clip', type=float, default=0.2, metavar='ε', help='PPO clip ratio')
parser.add_argument('--ppo-epochs', type=int, default=4, metavar='K', help='PPO epochs')
parser.add_argument('--value-loss-coeff', type=float, default=0.5, metavar='c1', help='Value loss coefficient')
parser.add_argument('--entropy-loss-coeff', type=float, default=0, metavar='c2', help='Entropy regularisation coefficient')
parser.add_argument('--learning-rate', type=float, default=0.001, metavar='η', help='Learning rate')
parser.add_argument('--batch-size', type=int, default=2048, metavar='B', help='Minibatch size')
parser.add_argument('--max-grad-norm', type=float, default=1, metavar='N', help='Maximum gradient L2 norm')
parser.add_argument('--evaluation-interval', type=int, default=10000, metavar='EI', help='Evaluation interval')
parser.add_argument('--evaluation-episodes', type=int, default=50, metavar='EE', help='Evaluation episodes')
parser.add_argument('--save-trajectories', action='store_true', default=False, help='Store trajectories from agent after training')
parser.add_argument('--imitation', type=str, default='', choices=['AIRL', 'BC', 'DRIL', 'FAIRL', 'GAIL', 'GMMIL', 'PUGAIL', 'RED'], metavar='I', help='Imitation learning algorithm')
parser.add_argument('--state-only', action='store_true', default=False, help='State-only imitation learning')
parser.add_argument('--absorbing', action='store_true', default=False, help='Indicate absorbing states')
parser.add_argument('--imitation-epochs', type=int, default=5, metavar='IE', help='Imitation learning epochs')
parser.add_argument('--imitation-batch-size', type=int, default=128, metavar='IB', help='Imitation learning minibatch size')
parser.add_argument('--imitation-replay-size', type=int, default=4, metavar='IRS', help='Imitation learning trajectory replay size')
parser.add_argument('--r1-reg-coeff', type=float, default=1, metavar='γ', help='R1 gradient regularisation coefficient')
parser.add_argument('--pos-class-prior', type=float, default=0.5, metavar='η', help='Positive class prior')
parser.add_argument('--nonnegative-margin', type=float, default=0, metavar='β', help='Non-negative margin')
#args = parser.parse_args()
"""

@hydra.main(config_path='conf', config_name='config')
def main(cfg: DictConfig) -> None:
  # Configuration check
  assert cfg.env_type in ENVS.keys()
  assert cfg.imitation in ['AIRL', 'DRIL', 'FAIRL', 'GAIL', 'GMMIL', 'PUGAIL', 'RED', 'BC', 'PPO']

  # General setup
  np.random.seed(cfg.seed)
  torch.manual_seed(cfg.seed)

  # Set up environment
  env = ENVS[cfg.env_type](cfg.env_name)
  env.seed(cfg.seed)
  expert_trajectories = env.get_dataset()  # Load expert trajectories dataset
  state_size, action_size = env.observation_space.shape[0], env.action_space.shape[0]
  
  # Set up agent
  agent = ActorCritic(state_size, action_size, cfg.hidden_size, log_std_dev_init=cfg.log_std_dev_init)
  agent_optimiser = optim.RMSprop(agent.parameters(), lr=cfg.agent_learning_rate, alpha=0.9)
  # Set up imitation learning components
  if cfg.imitation in ['AIRL', 'DRIL', 'FAIRL', 'GAIL', 'GMMIL', 'PUGAIL', 'RED']:
    if cfg.imitation == 'AIRL':
      discriminator = AIRLDiscriminator(state_size + (1 if cfg.absorbing else 0), action_size, cfg.hidden_size, cfg.discount, state_only=cfg.state_only)
    elif cfg.imitation == 'DRIL':
      discriminator = Actor(state_size, action_size, cfg.hidden_size, dropout=0.1)
    elif cfg.imitation in ['FAIRL', 'GAIL', 'PUGAIL']:
      discriminator = GAILDiscriminator(state_size + (1 if cfg.absorbing else 0), action_size, cfg.hidden_size, state_only=cfg.state_only, forward_kl=cfg.imitation == 'FAIRL')
    elif cfg.imitation == 'GMMIL':
      discriminator = GMMILDiscriminator(state_size + (1 if cfg.absorbing else 0), action_size, state_only=cfg.state_only)
    elif cfg.imitation == 'RED':
      discriminator = REDDiscriminator(state_size + (1 if cfg.absorbing else 0), action_size, cfg.hidden_size, state_only=cfg.state_only)
    if cfg.imitation in ['AIRL', 'DRIL', 'FAIRL', 'GAIL', 'PUGAIL', 'RED']:
      discriminator_optimiser = optim.RMSprop(discriminator.parameters(), lr=cfg.il_learning_rate)  # TODO: il_learning_rate

  # Metrics
  metrics = dict(train_steps=[], train_returns=[], test_steps=[], test_returns=[])
  recent_returns = deque(maxlen=cfg.evaluation.average_window)  # Stores most recent evaluation returns


  # Main training loop
  state, terminal, train_return, trajectories, policy_trajectory_replay_buffer = env.reset(), False, 0, [], deque(maxlen=cfg.imitation_replay_size)
  pbar = tqdm(range(1, cfg.steps + 1), unit_scale=1, smoothing=0)
  for step in pbar:
    # Perform initial training (if needed)
    if cfg.imitation in ['BC', 'DRIL', 'RED']:
      if step == 1:
        for _ in tqdm(range(cfg.imitation_epochs), leave=False):
          if cfg.imitation == 'BC':
            # Perform behavioural cloning updates offline
            behavioural_cloning_update(agent, expert_trajectories, agent_optimiser, cfg.batch_size)
          elif cfg.imitation == 'DRIL':
            # Perform behavioural cloning updates offline on policy ensemble (dropout version)
            behavioural_cloning_update(discriminator, expert_trajectories, discriminator_optimiser, cfg.batch_size)
            with torch.no_grad():
              discriminator.set_uncertainty_threshold(expert_trajectories['states'], expert_trajectories['actions'])
          elif cfg.imitation == 'RED':
            # Train predictor network to match random target network
            target_estimation_update(discriminator, expert_trajectories, discriminator_optimiser, cfg.batch_size, cfg.absorbing)

    if cfg.imitation != 'BC':
      # Collect set of trajectories by running policy π in the environment
      with torch.no_grad():
        policy, value = agent(state)
        action = policy.sample()
        log_prob_action = policy.log_prob(action)
        next_state, reward, terminal = env.step(action)
        train_return += reward
        trajectories.append(dict(states=state, actions=action, rewards=torch.tensor([reward], dtype=torch.float32), terminals=torch.tensor([terminal], dtype=torch.float32), log_prob_actions=log_prob_action, old_log_prob_actions=log_prob_action.detach(), values=value))
        state = next_state

      if terminal:
        # Store metrics and reset environment
        metrics['train_steps'].append(step)
        metrics['train_returns'].append([train_return])
        pbar.set_description(f'Step: {step} | Return: {train_return}')
        state, train_return = env.reset(), 0

      # Update models
      if len(trajectories) >= cfg.batch_size:
        policy_trajectories = flatten_list_dicts(trajectories)  # Flatten policy trajectories (into a single batch for efficiency; valid for feedforward networks)

        if cfg.imitation in ['AIRL', 'DRIL', 'FAIRL', 'GAIL', 'GMMIL', 'PUGAIL', 'RED']:
          # Train discriminator
          if cfg.imitation in ['AIRL', 'FAIRL', 'GAIL', 'PUGAIL']:
            # Use a replay buffer of previous trajectories to prevent overfitting to current policy
            policy_trajectory_replay_buffer.append(policy_trajectories)
            policy_trajectory_replays = flatten_list_dicts(policy_trajectory_replay_buffer)
            for _ in tqdm(range(cfg.imitation_epochs), leave=False):
              adversarial_imitation_update(cfg.imitation, agent, discriminator, expert_trajectories, TransitionDataset(policy_trajectory_replays), discriminator_optimiser, cfg.batch_size, cfg.absorbing, cfg.r1_reg_coeff, cfg.pos_class_prior, cfg.nonnegative_margin)

          # Predict rewards
          states, actions, next_states, terminals = policy_trajectories['states'], policy_trajectories['actions'], torch.cat([policy_trajectories['states'][1:], next_state]), policy_trajectories['terminals']
          if cfg.absorbing: states, actions, next_states = indicate_absorbing(states, actions, terminals, next_states)
          with torch.no_grad():
            if cfg.imitation == 'AIRL':
              policy_trajectories['rewards'] = discriminator.predict_reward(states, actions, next_states, policy_trajectories['log_prob_actions'].exp(), terminals)
            elif cfg.imitation == 'DRIL':
              # TODO: By default DRIL also includes behavioural cloning online?
              policy_trajectories['rewards'] = discriminator.predict_reward(states, actions)
            elif cfg.imitation in ['FAIRL', 'GAIL', 'PUGAIL']:
              policy_trajectories['rewards'] = discriminator.predict_reward(states, actions)
            elif cfg.imitation == 'GMMIL':
              expert_states, expert_actions = expert_trajectories['states'], expert_trajectories['actions']
              if cfg.absorbing: expert_states, expert_actions = indicate_absorbing(expert_states, expert_actions, expert_trajectories['terminals'])
              policy_trajectories['rewards'] = discriminator.predict_reward(states, actions, expert_states, expert_actions)
            elif cfg.imitation == 'RED':
              policy_trajectories['rewards'] = discriminator.predict_reward(states, actions)

        # Perform PPO updates (includes GAE re-estimation with updated value function)
        for _ in tqdm(range(cfg.ppo_epochs), leave=False):
          ppo_update(agent, policy_trajectories, next_state, agent_optimiser, cfg.discount, cfg.trace_decay, cfg.ppo_clip, cfg.value_loss_coeff, cfg.entropy_loss_coeff, cfg.max_grad_norm)
        trajectories, policy_trajectories = [], None
    
    
    # Evaluate agent and plot metrics
    if step % cfg.evaluation.interval == 0:
      test_returns = evaluate_agent(agent, cfg.evaluation.episodes, ENVS[cfg.env_type], cfg.env_name, cfg.seed)
      recent_returns.append(sum(test_returns) / cfg.evaluation.episodes)
      metrics['test_steps'].append(step)
      metrics['test_returns'].append(test_returns)
      lineplot(metrics['test_steps'], metrics['test_returns'], 'test_returns')
      if cfg.imitation != 'BC': lineplot(metrics['train_steps'], metrics['train_returns'], 'train_returns')


  if cfg.save_trajectories:
    # Store trajectories from agent after training
    _, trajectories = evaluate_agent(agent, cfg.evaluation.episodes, ENVS[cfg.env_type], cfg.env_name, cfg.seed, return_trajectories=True, render=cfg.render)
    torch.save(trajectories, 'trajectories.pth')
  # Save agent and metrics
  torch.save(agent.state_dict(), 'agent.pth')
  if cfg.imitation in ['AIRL', 'DRIL', 'FAIRL', 'GAIL', 'PUGAIL', 'RED']: torch.save(discriminator.state_dict(), 'discriminator.pth')
  torch.save(metrics, 'metrics.pth')


  env.close()
  return sum(recent_returns) / float(cfg.evaluation.average_window)

if __name__ == '__main__':
  main()
