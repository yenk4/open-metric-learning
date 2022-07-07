from random import choice
from typing import Dict, Optional, Tuple

from torch import (
    Tensor,
    abs,
    arange,
    cartesian_prod,
    cat,
    clip,
    combinations,
    flip,
    no_grad,
    randint,
    randperm,
    tensor,
    unique,
    zeros,
)

from oml.interfaces.miners import ITripletsMiner


@no_grad()
def get_pos_pairs(lbl2idx: Dict[Tensor, Tensor], n: int = None) -> Tensor:
    pos_batch_pairs = zeros(0, 2)

    if n is not None:
        while len(pos_batch_pairs) < n:
            pos_ii = choice(list(lbl2idx.values()))
            combs = combinations(pos_ii, r=2)
            pos_batch_pairs = cat([pos_batch_pairs, combs, flip(combs, [1])])
    else:
        for pos_ii in lbl2idx.values():
            combs = combinations(pos_ii, r=2)
            pos_batch_pairs = cat([pos_batch_pairs, combs, flip(combs, [1])])

    return pos_batch_pairs.long()[randperm(len(pos_batch_pairs))[:n]]


class TripletMinerWithMemory(ITripletsMiner):
    def __init__(self, bank_size_in_batches: int, tri_expand_k: int):
        assert tri_expand_k >= 1

        self.bank_size_in_batches = bank_size_in_batches
        self.tri_expand_k = tri_expand_k

        self.bank_features: Optional[Tensor] = None
        self.bank_labels: Optional[Tensor] = None
        self.bs = -1
        self.bank_size = -1
        self.ptr = 0

    @no_grad()
    def __allocate_if_needed(self, features: Tensor, labels: Tensor) -> None:
        if self.bank_features is None:
            assert len(features) == len(labels)

            self.bs = len(features)
            self.feat_dim = features.shape[-1]
            self.bank_size = self.bank_size_in_batches * self.bs

            # let's init our bank with the following labels: -1, -2, -3, -4 ...
            # and use one-hot encoding for their features
            self.bank_labels = -1 * arange(1, self.bs + 1).repeat(self.bank_size_in_batches).long()
            self.bank_features = zeros([self.bank_size, self.feat_dim], dtype=features.dtype).to(features.device)
            self.bank_features[arange(self.bank_size), clip(abs(self.bank_labels), max=self.feat_dim)] = 1

    @no_grad()
    def update_bank(self, features: Tensor, labels: Tensor) -> None:
        self.bank_features[self.ptr : self.ptr + self.bs] = features.clone().detach()
        self.bank_labels[self.ptr : self.ptr + self.bs] = labels.clone()
        self.ptr = (self.ptr + self.bs) % self.bank_size

    def sample(self, features: Tensor, labels: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
        labels = tensor(labels).long()
        self.__allocate_if_needed(features=features, labels=labels)

        assert len(features) == len(labels) == self.bs

        # todo: optimize performance?
        lbl2idx_bank = {lb: arange(self.bank_size)[self.bank_labels == lb] for lb in unique(self.bank_labels)}
        lbl2idx_batch = {lb: arange(self.bs)[labels == lb] for lb in unique(labels)}

        # part1: anchor + positive + negative come from batch
        ii_anch_pos_1 = get_pos_pairs(lbl2idx_batch)
        ii_all = arange(self.bs)
        ii_pos_pairs_1, ii_neg_1 = cartesian_prod(arange(len(ii_anch_pos_1)), ii_all).T
        ii_anch_1, ii_pos_1 = ii_anch_pos_1[ii_pos_pairs_1].T

        ii_anch_1, ii_pos_1, ii_neg_1 = self.take_tri_by_mask(
            ii_anch_1, ii_pos_1, ii_neg_1, mask=labels[ii_anch_1] != labels[ii_neg_1]
        )
        n_batch_tri = len(ii_anch_1)

        # part2: anchor + positive come from bank, negative comes from batch
        n_tri_positives_from_bank = int(n_batch_tri * (self.tri_expand_k - 1) / 2)
        ii_anch_2, ii_pos_2 = get_pos_pairs(lbl2idx_bank, n_tri_positives_from_bank).T
        ii_neg_2 = randint(0, self.bs, size=(len(ii_anch_2),))

        ii_anch_2, ii_pos_2, ii_neg_2 = self.take_tri_by_mask(
            ii_anch_2, ii_pos_2, ii_neg_2, mask=self.bank_labels[ii_anch_2] != labels[ii_neg_2]
        )

        # part3: anchor + positive come from batch, negative comes from bank
        # we try to make size of this part equals to part2
        n_tri_negatives_from_bank = n_tri_positives_from_bank
        ii_anch_3, ii_pos_3 = get_pos_pairs(lbl2idx_batch).T
        ii_neg_3 = randint(0, self.bank_size, size=(n_tri_negatives_from_bank,))
        ii_3 = randint(0, len(ii_anch_3), size=(n_tri_negatives_from_bank,))

        ii_anch_3 = ii_anch_3[ii_3]
        ii_pos_3 = ii_pos_3[ii_3]
        ii_anch_3, ii_pos_3, ii_neg_3 = self.take_tri_by_mask(
            ii_anch_3, ii_pos_3, ii_neg_3, mask=labels[ii_anch_3] != self.bank_labels[ii_neg_3]
        )

        features_anch = cat([features[ii_anch_3], self.bank_features[ii_anch_2], features[ii_anch_1]])
        features_pos = cat([features[ii_pos_3], self.bank_features[ii_pos_2], features[ii_pos_1]])
        features_neg = cat([self.bank_features[ii_neg_3], features[ii_neg_2], features[ii_neg_1]])
        assert len(features_anch) == len(features_pos) == len(features_neg)

        self.update_bank(features=features, labels=labels)

        # print(len(ii_anch_1), len(ii_anch_2), len(ii_anch_3))

        return features_anch, features_pos, features_neg

    @staticmethod
    def take_tri_by_mask(ii_a: Tensor, ii_p: Tensor, ii_n: Tensor, mask: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
        ii_a = ii_a[mask]
        ii_p = ii_p[mask]
        ii_n = ii_n[mask]
        return ii_a, ii_p, ii_n