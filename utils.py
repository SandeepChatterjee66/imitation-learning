from matplotlib import pyplot as plt
import numpy as np
import seaborn as sns
import torch

sns.set(style='white')


# Cycles over an iterable without caching the order (unlike itertools.cycle)
def cycle(iterable):
  while True:
    for x in iterable:
      yield x


# Flattens a list of dicts with torch Tensors
def flatten_list_dicts(list_dicts):
  return {k: torch.cat([d[k] for d in list_dicts], dim=0) for k in list_dicts[-1].keys()}


# Makes a lineplot with scalar x and statistics of vector y
def lineplot(x, y, filename, xaxis='Steps', yaxis='Returns', algo='', env=''):
  y = np.array(y)
  y_mean, y_std = y.mean(axis=1), y.std(axis=1)
  sns.lineplot(x=x, y=y_mean, color='coral')
  plt.fill_between(x, y_mean - y_std, y_mean + y_std, color='coral', alpha=0.3)
  plt.xlim(left=0, right=x[-1])
  plt.xlabel(xaxis)
  plt.ylabel(yaxis)
  plt.title(f'{env} : {algo}')
  plt.savefig(f'{filename}.png')
  plt.close()
