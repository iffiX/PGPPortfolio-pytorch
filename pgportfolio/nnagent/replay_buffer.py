import torch as t
import numpy as np
from typing import Union


class PGPBuffer:
    def __init__(self,
                 coin_features: np.ndarray,
                 batch_size=50,
                 window_size=50,
                 test_portion=0.15,
                 validation_portion=0.1,
                 sample_bias=0.1,
                 portion_reversed=False,
                 is_unordered=False,
                 device="cpu"):
        """
        Args:
            coin_features: Coin features in shape [feature, coin, time].
            window_size: Periods of input data
            test_portion: Portion of testing set, training portion is
                `1 - test_portion-validation_portion`.
            validation_portion: Portion of validation set.
            is_unordered: If False, the sample inside a mini-batch is in order.
            portion_reversed: If False, the order of sets is (train, test)
                              else the order is (test, train).
            device: Pytorch device to store information on.
        """
        assert coin_features.ndim == 3
        coin_num = coin_features.shape[1]
        period_num = coin_features.shape[2]

        self._coin_features = t.tensor(coin_features, device=device)

        # portfolio vector memory
        self._pvm = t.full([period_num, coin_num], 1.0 / coin_num,
                           device=device)

        self._batch_size = batch_size
        self._window_size = window_size
        self._sample_bias = sample_bias
        self._portion_reversed = portion_reversed
        self._is_unordered = is_unordered

        self._train_idx, self._test_idx, self._val_idx = \
            self._divide_data(period_num, window_size, test_portion,
                              validation_portion, portion_reversed)

        # the count of appended experiences
        self._new_exp_count = 0

    @property
    def train_num(self):
        return len(self._train_idx)

    @property
    def test_num(self):
        return len(self._test_idx)

    @property
    def val_num(self):
        return len(self._val_idx)

    def get_training_set(self):
        """
        Returns:
            All samples from the train set.
        """
        return self._pack_samples(self._train_idx)

    def get_test_set(self):
        """
        Returns:
            All samples from the test set.
        """
        return self._pack_samples(self._test_idx)

    def get_val_set(self):
        """
        Returns:
            All samples from the validation set.
        """
        return self._pack_samples(self._val_idx)

    def append_experience(self,
                          coin_features: np.ndarray,
                          pvm: Union[t.tensor, None] = None):
        """
        Used in online training. Append new experience and coin features
        to the current buffer.

        Args:
            coin_features: New coin features following the current features,
            shape is [feature, coin, time].
            pvm: New pvm weights, shape is [time, coin], let it be
            None if in the back-test case.
        """
        if not self._portion_reversed:
            raise RuntimeError("Cannot append experience to training set "
                               "when portions of data are not in"
                               "the reverse order.")
        self._new_exp_count += coin_features.shape[-1]
        self._train_idx += list(range(
            self._train_idx[-1], self._train_idx[-1] + coin_features.shape[-1]
        ))

        device = self._coin_features.device
        self._coin_features = t.cat(
            [self._coin_features, t.tensor(coin_features, device=device)]
        )
        self._pvm = t.cat([self._pvm, pvm.to(device)])

    def next_batch(self):
        """
        Returns:
             The next batch of training sample.
             The sample is a dictionary with keys:
              "X": input data;
              "y": future relative price;
              "last_w:" a numpy array with shape [batch_size, assets];
              "setw": a callback function used to update the PVM memory.
        """
        start_idx = self._train_idx[0]
        end_idx = self._train_idx[-1]
        if self._is_unordered:
            batch_idx = [
                self._sample_geometric(start_idx, end_idx, self._sample_bias)
                for _ in range(self._batch_size)
            ]
        else:
            batch_start = self._sample_geometric(
                start_idx, end_idx, self._sample_bias
            )
            batch_idx = list(range(batch_start, batch_start + self._batch_size))
        batch = self._pack_samples(batch_idx)
        return batch

    def _pack_samples(self, index):
        index = np.array(index)
        last_w = self._pvm[index - 1, :]

        def setw(w):
            self._pvm[index, :] = w

        batch = t.stack([
            self._coin_features[:, :, idx:idx + self._window_size + 1]
            for idx in index
        ])
        # features
        X = batch[:, :, :, :-1]
        # price relative vector
        y = (batch[:, :, :, -1] / batch[:, 0, :, -2]).squeeze(-1)
        return {"X": X, "y": y, "last_w": last_w, "setw": setw}

    @staticmethod
    def _sample_geometric(start, end, bias):
        """
        Generate a index within [start, end) with geometric probability.

        Args:
            bias: A value in (0, 1).
        """
        ran = np.random.geometric(bias)
        while ran > end - start:
            ran = np.random.geometric(bias)
        result = end - ran
        return result

    @staticmethod
    def _divide_data(period_num,
                     window_size,
                     test_portion,
                     val_portion,
                     portion_reversed):
        """
        Divide training data into three portions, train, test and validation.

        Args:
            period_num: Number of price records in the time dimension.
            window_size: Sliding window size of history price records
            visible to the agent.
            test_portion/val_portion: Percent of these two portions.
            portion_reversed: Whether reverse the order of portions.

        Returns:
            Three np.ndarray type index arrays, train, test, validation.
        """
        train_portion = 1 - test_portion - val_portion
        indices = np.arange(period_num)

        if portion_reversed:
            split_point = np.array(
                [val_portion, val_portion + test_portion]
            )
            split_idx = (split_point * period_num).astype(int)
            val_idx, test_idx, train_idx = np.split(indices, split_idx)
        else:
            split_point = np.array(
                [train_portion, train_portion + test_portion]
            )
            split_idx = (split_point * period_num).astype(int)
            train_idx, test_idx, val_idx = np.split(indices, split_idx)

        # truncate records in the last time window, otherwise we may
        # sample insufficient samples when reaching the last window.
        train_idx = train_idx[:-(window_size + 1)]
        test_idx = test_idx[:-(window_size + 1)]
        val_idx = val_idx[:-(window_size + 1)]

        return train_idx, test_idx, val_idx