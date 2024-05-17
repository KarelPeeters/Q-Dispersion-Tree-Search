from basic_types import Action, ActionDistribution, HiddenValue, Interval, IntervalLike
from info_set import InfoSet
from model import Model
from utils import VisitCounter, perturb_prob_simplex

import numpy as np

import abc
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import logging


class Constants:
    EPS = 0.05
    c_PUCT = 1.0


CHEAT = True  # evaluate model for child nodes immediately, so we don't need Vc
trees = []
visit_counter = VisitCounter()


def to_interval(i: IntervalLike) -> Interval:
    if isinstance(i, Interval):
        return i
    return np.array([i, i])


class Node(abc.ABC):
    def __init__(self, info_set: InfoSet, tree_owner: Optional[int] = None, Q: IntervalLike=0):
        self.info_set = info_set
        self.cp = info_set.get_current_player()
        self.game_outcome = info_set.get_game_outcome()
        self.Q: Optional[Interval] = to_interval(Q)
        self.N = 0
        self.tree_owner = tree_owner
        self.children: Dict[int, Edge] = {}
        self.residual_Q_to_V = 0

    def add_child(self, key: int, node: 'Node'):
        self.children[key] = Edge(len(self.children), node)
        logging.debug(f'  - {key}: {node}')

    def get_Qc(self) -> np.ndarray:
        return np.array([edge.node.Q for edge in self.children.values()])

    def get_Vc(self) -> np.ndarray:
        return np.array([edge.node.V for edge in self.children.values()])

    def terminal(self) -> bool:
        return self.game_outcome is not None

    @abc.abstractmethod
    def visit(self, model: Model):
        pass

    def calc_union_interval(self, probs) -> Interval:
        Vc = self.get_Vc()
        Vc_intervals = np.tile(Vc[:, np.newaxis], (1, 2))
        child_keys = np.array(list(self.children.keys()))
        union_interval = perturb_prob_simplex(Vc_intervals, probs[child_keys], eps=Constants.EPS)
        return union_interval


@dataclass
class Edge:
    index: int
    node: Node


class ActionNode(Node):
    def __init__(self, info_set: InfoSet, tree_owner: Optional[int] = None, initQ: IntervalLike=0):
        super().__init__(info_set, tree_owner=tree_owner, Q=initQ)

        self.actions = info_set.get_actions()
        self.P = None
        self.V = None  #self.game_outcome if self.terminal() else None
        self.Vc = None
        self.spawned_tree: Optional[Tree] = None

        if self.terminal():
            self.V = self.game_outcome[self.tree_owner]
            self.Q = to_interval(self.V)

        self._expanded = False

    def __str__(self):
        return f'Action({self.info_set}, tree_owner={self.tree_owner}, N={self.N}, Q={self.Q}, V={self.V})'

    def eval_model(self, model: Model):
        if self.P is not None or self.terminal():
            # already evaluated
            return

        self.P, self.V, self.Vc = model.action_eval(self.tree_owner, self.info_set)
        self.Q = to_interval(self.V)

    def expand(self, model: Model):
        logging.debug(f'- expanding {self}')

        self._expanded = True
        if self.terminal():
            return
        self.eval_model(model)
        for a in self.actions:
            info_set = self.info_set.apply(a)
            if self.cp != info_set.get_current_player() and info_set.has_hidden_info():
                node = SamplingNode(info_set, tree_owner=self.tree_owner, initQ=self.Vc[a])
            else:
                node = ActionNode(info_set, tree_owner=self.tree_owner, initQ=self.Vc[a])

            self.add_child(a, node)
            if CHEAT:
                node.eval_model(model)

        self.Q = to_interval(self.V)

    def computePUCT(self):
        c = len(self.children)
        actions = np.zeros(c, dtype=int)
        Q = np.zeros((c, 2))  # mins and maxes
        P = self.P
        N = np.zeros(c)
        for a, edge in self.children.items():
            i = edge.index
            child = edge.node
            actions[i] = a
            Q[i] = child.Q
            N[i] = child.N

        PUCT = Constants.c_PUCT * P * np.sqrt(np.sum(N)) / (N + 1)
        PUCT = Q + PUCT[:, np.newaxis]

        # check for pure case
        max_lower_bound_index = np.argmax(PUCT[:, 0])
        max_lower_bound = PUCT[max_lower_bound_index, 0]

        logging.debug(f'-- PUCT:')
        for q, n, puct in zip(Q, N, PUCT):
            logging.debug(f'Q: {q}, N: {n}, PUCT: {puct}')

        return Q, np.where(PUCT[:, 1] >= max_lower_bound - 1e-8)[0]

    def get_mixing_distribution(self, action_indices):
        mask = np.zeros_like(self.P)
        mask[action_indices] = 1
        P = self.P * mask

        s = np.sum(P)
        assert s > 0, (self.P, mask)
        return P / s

    def visit(self, model: Model):
        logging.debug(f'= Visiting {self}:')
        self.N += 1

        if self.terminal():
            logging.debug(f'= end visit {self} hit terminal, return Q: {self.Q}')
            return

        if not self._expanded:
            self.expand(model)
            logging.debug(f'= end visit {self} expand, return self.Q: {self.Q}')
            return

        if self.spawned_tree is not None:
            self.spawned_visit(model)
        else:
            return self.unspawned_visit(model)

    def spawned_visit(self, model: Model):
        logging.debug(f'======= get action distr from spawn tree: {self.spawned_tree}')
        if self.spawned_tree.root.N == 0:
            self.spawned_tree.root.visit(model)

        action = self.spawned_tree.root.visit(model)
        child = self.children[action].node
        child.visit(model)

        union_interval = self.calc_union_interval(self.P)
        self.Q = union_interval + child.residual_Q_to_V
        self.residual_Q_to_V = (self.residual_Q_to_V * (self.N - 1) + self.Q - self.V) / self.N

    def unspawned_visit(self, model: Model):
        Qc, action_indices = self.computePUCT()
        if len(action_indices) == 1:  # pure case
            action_index = action_indices[0]
        else:  # mixed case
            mixing_distr = self.get_mixing_distribution(action_indices)
            action_index = np.random.choice(len(self.P), p=mixing_distr)

        action = self.actions[action_index]
        child = self.children[action].node
        child.visit(model)

        union_interval = self.calc_union_interval(self.P)
        self.Q = union_interval + child.residual_Q_to_V
        self.residual_Q_to_V = (self.residual_Q_to_V * (self.N - 1) + self.Q - self.V) / self.N

        logging.debug(f'= end visit {self}')
        return action

class SamplingNode(Node):
    def __init__(self, info_set: InfoSet, tree_owner: Optional[int] = None, initQ: IntervalLike=0):
        super().__init__(info_set, tree_owner, Q=initQ)
        self.H = None
        self.V = None
        self.Vc = None
        self.H_mask = info_set.get_H_mask()
        assert np.any(self.H_mask)
        self._expanded = False

    def __str__(self):
        return f'Hidden({self.info_set}, tree_owner={self.tree_owner}, N={self.N}, Q={self.Q}), V={self.V}'

    def apply_H_mask(self):
        self.H *= self.H_mask

        H_sum = np.sum(self.H)
        if H_sum < 1e-6:
            self.H = self.H_mask / np.sum(self.H_mask)
        else:
            self.H /= H_sum

    def eval_model(self, model: Model):
        if self.H is not None:
            # already evaluated
            return
        self.H, self.V, self.Vc = model.hidden_eval(self.tree_owner, self.info_set)
        self.apply_H_mask()
        self.Q = to_interval(self.V)

    def expand(self, model: Model):
        self.eval_model(model)

        for h in np.where(self.H_mask)[0]:
            info_set = self.info_set.instantiate_hidden_state(h)
            if info_set.has_hidden_info():
                assert self.cp == info_set.get_current_player()
                node = SamplingNode(info_set, tree_owner=self.tree_owner, initQ=self.Vc[h])
            else:
                node = ActionNode(info_set, tree_owner=self.tree_owner, initQ=self.Vc[h])
                node.spawned_tree = self.create_spawned_tree(info_set, model)

            self.add_child(h, node)
            if CHEAT:
                node.eval_model(model)

            logging.debug(f'  - {h}: {node}')
            if node.spawned_tree is not None:
                logging.debug(f'  - spawned tree: {node.spawned_tree}')

        self._expanded = True

    def create_spawned_tree(self, info_set: InfoSet, model: Model):
        info_set = info_set.clone()
        cp = info_set.get_current_player()
        for i in range(len(info_set.cards)):
            if i != cp:
                info_set.cards[i] = None

        root = ActionNode(info_set)
        spawned_tree = Tree(model, root)
        return spawned_tree

    def visit(self, model: Model):
        logging.debug(f'= Visiting {self}:')
        self.N += 1

        if not self._expanded:
            logging.debug(f'- expanding {self}')
            self.expand(model)

        h = np.random.choice(len(self.H), p=self.H)
        logging.debug(f'- sampling hidden state {h} from {self.H}')

        edge = self.children[h]
        child = edge.node
        child.visit(model)

        union_interval = self.calc_union_interval(self.H)
        self.Q = union_interval + child.residual_Q_to_V
        self.residual_Q_to_V = (self.residual_Q_to_V * (self.N - 1) + self.Q - self.V) / self.N

        logging.debug(f'= end visit {self}')


class Tree:
    next_id = 0

    def __init__(self, model: Model, root: ActionNode):
        self.model = model
        self.root = root
        self.tree_owner = root.info_set.get_current_player()
        self.root.tree_owner = self.tree_owner

        self.tree_id = Tree.next_id
        Tree.next_id += 1
        trees.append(self)

    def __str__(self):
        return f'Tree(id={self.tree_id}, owner={self.tree_owner}, root={self.root})'

    def get_visit_distribution(self, n: int) -> Dict[Action, float]:
        while self.root.N <= n:
            logging.debug(f'======= visit tree: {self}')
            self.root.visit(self.model)
            visit_counter.save_visited_trees(trees, 'debug')

        n_total = self.root.N - 1
        return {action: edge.node.N / n_total for action, edge in self.root.children.items()}
