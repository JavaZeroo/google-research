# coding=utf-8
# Copyright 2024 The Google Research Authors.
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

"""Blocking checkpoint loading loops with flax/training/checkpoints.py.

Checkpointing helper functions.

Handles saving and restoring optimizer checkpoints based on step-number or
other numerical metric in filename.  Cleans up older / worse-performing
checkpoint files.
"""

import os
import shutil
import logging
import re
import time
import glob
from absl import logging
from flax import serialization
import pickle

# Single-group reg-exps for int or float numerical substrings.
# captures sign:
SIGNED_FLOAT_RE = re.compile(
    r'([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)')
# does not capture sign:
UNSIGNED_FLOAT_RE = re.compile(
    r'[-+]?((?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)')


def _checkpoint_path(ckpt_dir, step, prefix):
  return os.path.join(ckpt_dir, f'{prefix}{step}')


def natural_sort(file_list, signed=True):
  """Natural sort for filenames with numerical substrings.

  Args:
    file_list: List[str]: list of paths to sort containing numerical
      substrings.
    signed: bool: if leading '-' (or '+') signs should be included in
      numerical substrings as a sign or treated as a separator.
  Returns:
    List of filenames sorted 'naturally', not lexicographically: any
    integer substrings are used to subsort numerically. e.g.
    file_1, file_10, file_2  -->  file_1, file_2, file_10
    file_0.1, file_-0.2, file_2.0  -->  file_-0.2, file_0.1, file_2.0
  """
  float_re = SIGNED_FLOAT_RE if signed else UNSIGNED_FLOAT_RE
  def maybe_num(s):
    if float_re.match(s):
      return float(s)
    else:
      return s
  def split_keys(s):
    return [maybe_num(c) for c in float_re.split(s)]
  return sorted(file_list, key=split_keys)


def save_checkpoint(ckpt_dir, target, step, prefix='checkpoint_', keep=1, overwrite=False):
    """保存模型的检查点。
    
    尝试通过先写入临时文件然后重命名并清理旧文件的方式，确保操作的抗中断性。
    
    参数:
      ckpt_dir: str: 存储检查点文件的路径。
      target: 可序列化的对象，通常是一个优化器。
      step: int 或 float: 训练步骤号或其他度量编号。
      prefix: str: 检查点文件名前缀。
      keep: 保留过去检查点文件的数量。
      overwrite: bool: 是否允许覆盖写入检查点。
    
    返回:
      保存的检查点的文件名。
    """
    # 创建检查点目录
    if not os.path.exists(ckpt_dir):
        os.makedirs(ckpt_dir)
    logging.info('Saving checkpoint at step: %s', step)

    # 构建临时检查点路径和最终路径
    ckpt_tmp_path = os.path.join(ckpt_dir, f'{prefix}tmp')
    ckpt_path = os.path.join(ckpt_dir, f'{prefix}{step}')

    logging.info('Writing to temporary checkpoint location: %s', ckpt_tmp_path)
    # 将对象序列化并写入临时文件
    with open(ckpt_tmp_path, 'wb') as fp:
        pickle.dump(target, fp)

    # 重命名临时文件为最终检查点文件
    if os.path.exists(ckpt_path):
        if overwrite:
            os.remove(ckpt_path)
        else:
            raise FileExistsError(f"The file {ckpt_path} already exists and overwrite is set to False.")
    shutil.move(ckpt_tmp_path, ckpt_path)
    logging.info('Saved checkpoint at %s', ckpt_path)

    # 清理旧的检查点文件
    base_path = os.path.join(ckpt_dir, f'{prefix}')
    checkpoint_files = sorted([p for p in os.listdir(ckpt_dir) if p.startswith(prefix)])
    if len(checkpoint_files) > keep:
        old_ckpts = checkpoint_files[:-keep]
        for path in old_ckpts:
            full_path = os.path.join(ckpt_dir, path)
            logging.info('Removing checkpoint at %s', full_path)
            os.remove(full_path)

    return ckpt_path

def latest_checkpoint_path(ckpt_dir, prefix):
  glob_path = os.path.join(ckpt_dir, f'{prefix}*')
  checkpoint_files = natural_sort(glob.glob(glob_path))
  ckpt_tmp_path = _checkpoint_path(ckpt_dir, 'tmp', prefix)
  checkpoint_files = [f for f in checkpoint_files if f != ckpt_tmp_path]
  return checkpoint_files[-1] if checkpoint_files else None


def restore_from_path(ckpt_path, target):
    logging.info('Restoring checkpoint from %s', ckpt_path)
    with open(ckpt_path, 'rb') as fp:
        return pickle.load(fp)

def restore_checkpoint(ckpt_dir, target, step=None, prefix='checkpoint_'):
  if step:
    ckpt_path = _checkpoint_path(ckpt_dir, step, prefix)
    if not os.path.exists(ckpt_path):
      raise ValueError(f'Matching checkpoint not found: {ckpt_path}')
  else:
    ckpt_path = latest_checkpoint_path(ckpt_dir, prefix)
    if ckpt_path is None:
      return target
  return restore_from_path(ckpt_path, target)


def wait_for_new_checkpoint(ckpt_dir,
                            last_ckpt_path=None,
                            seconds_to_sleep=1,
                            timeout=None,
                            prefix='checkpoint_'):
  """Waits until a new checkpoint file is found.

  Args:
    ckpt_dir: The directory in which checkpoints are saved.
    last_ckpt_path: The last checkpoint path used or `None` if we're expecting
      a checkpoint for the first time.
    seconds_to_sleep: The number of seconds to sleep for before looking for a
      new checkpoint.
    timeout: The maximum number of seconds to wait. If left as `None`, then the
      process will wait indefinitely.
    prefix: str: name prefix of checkpoint files.

  Returns:
    a new checkpoint path, or None if the timeout was reached.
  """
  logging.info('Waiting for new checkpoint at %s', ckpt_dir)
  stop_time = time.time() + timeout if timeout is not None else None
  while True:
    ckpt_path = latest_checkpoint_path(ckpt_dir, prefix)
    if ckpt_path is None or ckpt_path == last_ckpt_path:
      if stop_time is not None and time.time() + seconds_to_sleep > stop_time:
        return None
      time.sleep(seconds_to_sleep)
    else:
      logging.info('Found new checkpoint at %s', ckpt_path)
      return ckpt_path


def checkpoints_iterator(ckpt_dir,
                         target,
                         timeout=None,
                         min_interval_secs=0,
                         prefix='checkpoint_'):
  """Repeatedly yield new checkpoints as they appear.

  Args:
    ckpt_dir: str: directory in which checkpoints are saved.
    target: matching object to rebuild via deserialized state-dict.
    timeout: int: maximum number of seconds to wait. If left as `None`, then the
      process will wait indefinitely.
    min_interval_secs: int: minimum number of seconds between yielding
      checkpoints.
    prefix: str: name prefix of checkpoint files.

  Yields:
    new checkpoint path if `target` is None, otherwise `target` updated from
    the new checkpoint path.
  """
  ckpt_path = None
  while True:
    new_ckpt_path = wait_for_new_checkpoint(
        ckpt_dir, ckpt_path, timeout=timeout, prefix=prefix)
    if new_ckpt_path is None:
      # timed out
      logging.info('Timed-out waiting for a checkpoint.')
      return
    start = time.time()
    ckpt_path = new_ckpt_path

    yield ckpt_path if target is None else restore_from_path(ckpt_path, target)

    time_to_next_eval = start + min_interval_secs - time.time()
    if time_to_next_eval > 0:
      time.sleep(time_to_next_eval)
