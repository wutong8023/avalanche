import numpy as np
import quadprog
import torch
from torch.utils.data import DataLoader

from avalanche.training.plugins.strategy_plugin import StrategyPlugin


class GEMPlugin(StrategyPlugin):
    """
    Gradient Episodic Memory Plugin.
    GEM projects the gradient on the current minibatch by using an external
    episodic memory of patterns from previous experiences. The gradient on
    the current minibatch is projected so that the dot product with all the
    reference gradients of previous tasks remains positive.
    This plugin does not use task identities.
    """

    def __init__(self, patterns_per_experience: int, memory_strength: float):
        """
        :param patterns_per_experience: number of patterns per experience in the
            memory.
        :param memory_strength: offset to add to the projection direction
            in order to favour backward transfer (gamma in original paper).
        """

        super().__init__()

        self.patterns_per_experience = int(patterns_per_experience)
        self.memory_strength = memory_strength

        self.memory_x, self.memory_y = {}, {}

        self.G = None

    def before_training_iteration(self, strategy, **kwargs):
        """
        Compute gradient constraints on previous memory samples from all
        experiences.
        """

        if strategy.clock.train_exp_counter > 0:
            G = []
            strategy.model.train()
            for t in range(strategy.clock.train_exp_counter):
                strategy.optimizer.zero_grad()
                xref = self.memory_x[t].to(strategy.device)
                yref = self.memory_y[t].to(strategy.device)
                out = strategy.model(xref)
                loss = strategy._criterion(out, yref)
                loss.backward()

                G.append(torch.cat([p.grad.flatten()
                                    for p in strategy.model.parameters()
                                    if p.grad is not None], dim=0))

            self.G = torch.stack(G)  # (experiences, parameters)

    @torch.no_grad()
    def after_backward(self, strategy, **kwargs):
        """
        Project gradient based on reference gradients
        """

        if strategy.clock.train_exp_counter > 0:
            g = torch.cat([p.grad.flatten()
                           for p in strategy.model.parameters()
                           if p.grad is not None], dim=0)

            to_project = (torch.mv(self.G, g) < 0).any()
        else:
            to_project = False

        if to_project:
            v_star = self.solve_quadprog(g).to(strategy.device)

            num_pars = 0  # reshape v_star into the parameter matrices
            for p in strategy.model.parameters():

                curr_pars = p.numel()

                if p.grad is None:
                    continue

                p.grad.copy_(
                    v_star[num_pars:num_pars + curr_pars].view(p.size()))
                num_pars += curr_pars

            assert num_pars == v_star.numel(), "Error in projecting gradient"

    def after_training_exp(self, strategy, **kwargs):
        """
        Save a copy of the model after each experience
        """

        self.update_memory(strategy.experience.dataset,
                           strategy.clock.train_exp_counter,
                           strategy.train_mb_size)

    @torch.no_grad()
    def update_memory(self, dataset, t, batch_size):
        """
        Update replay memory with patterns from current experience.
        """
        dataloader = DataLoader(dataset, batch_size=batch_size)
        tot = 0
        for x, y, _ in dataloader:
            if tot + x.size(0) <= self.patterns_per_experience:
                if t not in self.memory_x:
                    self.memory_x[t] = x.clone()
                    self.memory_y[t] = y.clone()
                else:
                    self.memory_x[t] = torch.cat((self.memory_x[t], x), dim=0)
                    self.memory_y[t] = torch.cat((self.memory_y[t], y), dim=0)
            else:
                diff = self.patterns_per_experience - tot
                if t not in self.memory_x:
                    self.memory_x[t] = x[:diff].clone()
                    self.memory_y[t] = y[:diff].clone()
                else:
                    self.memory_x[t] = torch.cat((self.memory_x[t], x[:diff]),
                                                 dim=0)
                    self.memory_y[t] = torch.cat((self.memory_y[t], y[:diff]),
                                                 dim=0)
                break
            tot += x.size(0)

    def solve_quadprog(self, g):
        """
        Solve quadratic programming with current gradient g and
        gradients matrix on previous tasks G.
        Taken from original code:
        https://github.com/facebookresearch/GradientEpisodicMemory/blob/master/model/gem.py
        """

        memories_np = self.G.cpu().double().numpy()
        gradient_np = g.cpu().contiguous().view(-1).double().numpy()
        t = memories_np.shape[0]
        P = np.dot(memories_np, memories_np.transpose())
        P = 0.5 * (P + P.transpose()) + np.eye(t) * 1e-3
        q = np.dot(memories_np, gradient_np) * -1
        G = np.eye(t)
        h = np.zeros(t) + self.memory_strength
        v = quadprog.solve_qp(P, q, G, h)[0]
        v_star = np.dot(v, memories_np) + gradient_np

        return torch.from_numpy(v_star).float()
