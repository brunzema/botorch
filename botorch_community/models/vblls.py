#!/usr/bin/env python3
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
This file contains an implemenation of a Variational Bayesian Last Layer (VBLL) model that can be used within BoTorch
for Bayesian optimization.

References:

[1] P. Brunzema, M. Jordahn, J. Willes, S. Trimpe, J. Snoek, J. Harrison.
    Bayesian Optimization via Contrinual Variational Last Layer Training.
    International Conference on Learning Representations, 2025.

Contributor: brunzema
"""

from typing import Dict, Optional, Type
from abc import ABC, abstractmethod

import torch
import torch.nn as nn
from torch import Tensor
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from gpytorch.distributions import MultivariateNormal

from botorch.posteriors import Posterior
from botorch.models.model import Model
from botorch.posteriors.gpytorch import GPyTorchPosterior

from botorch_community.posteriors.bll_posterior import BLLPosterior

import vbll


torch.set_default_dtype(torch.float64)


class SampleModel(nn.Module):
    def __init__(self, backbone: nn.Module, sampled_params: Tensor):
        super().__init__()
        self.backbone = backbone
        self.sampled_params = sampled_params

    def forward(self, x: Tensor) -> Tensor:
        x = self.backbone(x)

        if self.sampled_params.dim() == 2:
            return (self.sampled_params @ x[..., None]).squeeze(-1)

        x_expanded = x.unsqueeze(0).expand(self.sampled_params.shape[0], -1, -1)
        return torch.matmul(self.sampled_params, x_expanded.transpose(-1, -2))


class VBLLNetwork(nn.Module):
    """
    A model with a Variational Bayesian Linear Last (VBLL) layer.

    Args:
        in_features (int, optional):
            Number of input features. Defaults to 2.
        hidden_features (int, optional):
            Number of hidden units per layer. Defaults to 50.
        out_features (int, optional):
            Number of output features. Defaults to 1.
        num_layers (int, optional):
            Number of hidden layers in the MLP. Defaults to 3.
        prior_scale (float, optional):
            Scaling factor for the prior distribution in the Bayesian last layer. Defaults to 1.0.
        wishart_scale (float, optional):
            Scaling factor for the Wishart prior in the Bayesian last layer. Defaults to 0.01.
        kl_scale (float, optional):
            Weighting factor for the Kullback-Leibler (KL) divergence term in the loss. Defaults to 1.0.
        backbone (nn.Module, optional):
            A predefined feature extractor to be used before the MLP layers. If None,
            a default MLP structure is used. Defaults to None.
        activation (nn.Module, optional):
            Activation function applied between hidden layers. Defaults to `nn.ELU()`.

    Notes:
        - If a `backbone` module is provided, it is applied before the variational last layer. If not, we use a default MLP structure.
    """

    def __init__(
        self,
        in_features: int = 2,
        hidden_features: int = 64,
        out_features: int = 1,
        num_layers: int = 3,
        prior_scale: float = 1.0,
        wishart_scale: float = 0.01,
        kl_scale: float = 1.0,
        backbone: nn.Module = None,
        activation: nn.Module = nn.ELU(),
        device=None,
    ):
        super(VBLLNetwork, self).__init__()
        self.num_inputs = in_features
        self.num_outputs = out_features

        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = device

        self.activation = activation
        self.kl_scale = kl_scale

        if backbone is None:
            self.backbone = nn.Sequential(
                nn.Linear(in_features, hidden_features),
                self.activation,
                *[
                    nn.Sequential(
                        nn.Linear(hidden_features, hidden_features), self.activation
                    )
                    for _ in range(num_layers)
                ],
            )
        else:
            self.backbone = backbone

        # could be changed to other regression layers in vbll package
        self.head = vbll.Regression(
            hidden_features,
            out_features,
            regularization_weight=1.0,  # will be adjusted dynamically at each iteration based on the number of data points
            prior_scale=prior_scale,
            wishart_scale=wishart_scale,
            parameterization="dense_precision",
        )

    def forward(self, x: Tensor) -> Tensor:
        x = self.backbone(x)
        return self.head(x)

    def sample_posterior_function(
        self, sample_shape: Optional[torch.Size] = None
    ) -> nn.Module:
        """
        Samples a posterior function by drawing parameters from the model's learned distribution.

        Args:
            sample_shape (Optional[torch.Size], optional):
                The desired shape for the sampled parameters. If None, a single sample is drawn.
                Defaults to None.

        Returns:
            nn.Module[[Tensor], Tensor]:
                A nn.Module that takes an input tensor `x` and returns the corresponding
                model output tensor. The function applies the backbone transformation
                and computes the final output using the sampled parameters.

        Notes:
            - If `sample_shape` is None, a single set of parameters is sampled.
            - If `sample_shape` is provided, multiple parameter samples are drawn, and the function
              will return a batched output where the first dimension corresponds to different samples.
        """
        sampled_params = (
            self.head.W().rsample(sample_shape).to(self.device)
            if sample_shape
            else self.head.W().rsample().to(self.device)
        )
        return SampleModel(self.backbone, sampled_params)


def _get_optimizer(
    optimizer_class: Type[Optimizer],
    model_parameters,
    lr: float = 1e-3,
    **kwargs,
) -> Optimizer:
    """
    Creates and returns an optimizer.

    Args:
        optimizer_class (Type[Optimizer]): The optimizer class (e.g., torch.optim.AdamW).
        model_parameters: Parameters to be optimized.
        lr (float): Learning rate.
        **kwargs: Additional arguments to be passed to the optimizer.

    Returns:
        Optimizer: The initialized optimizer.
    """
    return optimizer_class(model_parameters, lr=lr, **kwargs)


class AbstractBLLModel(Model, ABC):
    def __init__(self):
        super().__init__()
        self.model = None
        self.old_model = None  # Used for continual learning

    @property
    def num_outputs(self) -> int:
        return self.model.num_outputs

    @property
    def num_inputs(self):
        return self.model.num_inputs

    @property
    def device(self):
        return self.model.device

    @property
    def backbone(self):
        return self.model.backbone

    def fit(
        self,
        train_X: Tensor,
        train_y: Tensor,
        optimization_settings: Optional[Dict] = None,
        initialization_params: Optional[Dict] = None,
    ):
        """
        Fits the model to the given training data. Note that for continual learning, we assume that the last point in the training data is the new point.

        Args:
            train_X (Tensor):
                The input training data, expected to be a PyTorch tensor of shape (num_samples, num_features).

            train_y (Tensor):
                The target values for training, expected to be a PyTorch tensor of shape (num_samples, num_outputs).

            optimization_settings (dict, optional):
                A dictionary containing optimization-related settings. If a key is missing, default values will be used.
                Available settings:
                    - "num_epochs" (int, default=100): The maximum number of training epochs.
                    - "patience" (int, default=10): Number of epochs to wait before early stopping.
                    - "freeze_backbone" (bool, default=False): If True, the backbone of the model is frozen.
                    - "batch_size" (int, default=32): Batch size for the training.
                    - "optimizer" (torch.optim.Optimizer, default=torch.optim.AdamW): Optimizer for training.
                    - "wd" (float, default=1e-4): Weight decay (L2 regularization) coefficient.
                    - "clip_val" (float, default=1.0): Gradient clipping threshold.

            initialization_params (dict, optional):
                A dictionary containing the initial parameters of the model for feature reuse.
                If None, the optimization will start from from the random initialization in the __init__ method.

        Returns:
            None: The function trains the model in place and does not return a value.
        """

        # Default settings
        default_opt_settings = {
            "num_epochs": 10_000,
            "freeze_backbone": False,
            "patience": 100,
            "batch_size": 32,
            "optimizer": torch.optim.AdamW,  # Now uses a class, not an instance
            "lr": 1e-3,
            "wd": 1e-4,
            "clip_val": 1.0,
            "optimizer_kwargs": {},  # Extra optimizer-specific args (e.g., betas for Adam)
        }

        # Merge defaults with provided settings
        optimization_settings = (
            default_opt_settings
            if optimization_settings is None
            else {**default_opt_settings, **optimization_settings}
        )

        # Make dataloader based on train_X, train_y
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        dataset = [[train_X[i], train_y[i]] for i, _ in enumerate(train_X)]

        dataloader = DataLoader(
            dataset, shuffle=True, batch_size=optimization_settings["batch_size"]
        )

        if initialization_params is not None:
            self.model.load_state_dict(initialization_params)

        self.model.to(device)
        self.set_reg_weight(self.model.kl_scale / len(train_y))
        param_list = [
            {
                "params": self.model.head.parameters(),
                "weight_decay": 0.0,
            },
        ]

        # freeze backbone
        if not optimization_settings["freeze_backbone"]:
            param_list.append(
                {
                    "params": self.model.backbone.parameters(),
                    "weight_decay": optimization_settings["wd"],
                }
            )

        # Extract settings
        optimizer_class = optimization_settings["optimizer"]
        optimizer_kwargs = optimization_settings.get("optimizer_kwargs", {})

        # Initialize optimizer using helper function
        optimizer = _get_optimizer(
            optimizer_class,
            model_parameters=param_list,
            lr=optimization_settings["lr"],
            **optimizer_kwargs,
        )

        best_loss = float("inf")
        epochs_no_improve = 0
        early_stop = False
        best_model_state = None  # To store the best model parameters

        self.model.train()

        for epoch in range(1, optimization_settings["num_epochs"] + 1):
            # early stopping
            if early_stop:
                break

            running_loss = []

            for train_step, (x, y) in enumerate(dataloader):
                x, y = x.to(device), y.to(device)
                optimizer.zero_grad()
                out = self.model(x)
                loss = out.train_loss_fn(y)  # vbll layer will calculate the loss

                loss.backward()

                if optimization_settings["clip_val"] is not None:
                    torch.nn.utils.clip_grad_norm_(
                        self.model.parameters(), optimization_settings["clip_val"]
                    )

                optimizer.step()
                running_loss.append(loss.item())

            # Calculate average loss over the epoch
            avg_loss = sum(running_loss[-len(dataloader) :]) / len(dataloader)

            # Early stopping logic
            if avg_loss < best_loss:
                best_loss = avg_loss
                best_model_state = self.model.state_dict()
                epochs_no_improve = 0
            else:
                epochs_no_improve += 1

            if epochs_no_improve >= optimization_settings["patience"]:
                early_stop = True

        # load best model
        if best_model_state is not None:
            self.model.load_state_dict(best_model_state)
            print("Early stopping at epoch ", epoch, " with loss ", best_loss)

    def set_reg_weight(self, new_weight: float):
        self.model.head.regularization_weight = new_weight

    def posterior(
        self,
        X: Tensor,
        output_indices=None,
        observation_noise=None,
        posterior_transform=None,
    ) -> Posterior:
        # Determine if the input is batched
        batched = X.dim() == 3

        if not batched:
            N, D = X.shape
            B = 1
        else:
            B, N, D = X.shape
            X = X.reshape(B * N, D)

        posterior = self.model(X).predictive

        # Extract mean and variance
        mean = posterior.mean.squeeze()
        variance = posterior.variance.squeeze()
        cov = torch.diag_embed(variance)

        K = self.num_outputs
        mean = mean.reshape(B, N * K)

        # Cov must be `(B, Q*K, Q*K)`
        cov = cov.reshape(B, N, K, B, N, K)
        cov = torch.einsum("bqkbrl->bqkrl", cov)  # (B, Q, K, Q, K)
        cov = cov.reshape(B, N * K, N * K)

        # Remove fake batch dimension if not batched
        if not batched:
            mean = mean.squeeze(0)
            cov = cov.squeeze(0)

        # pass as MultivariateNormal to GPyTorchPosterior
        mvn_dist = MultivariateNormal(mean, cov)
        post_pred = GPyTorchPosterior(mvn_dist)
        return BLLPosterior(post_pred, self, X, self.num_outputs)

    @abstractmethod
    def sample(self, sample_shape: Optional[torch.Size] = None):
        raise NotImplementedError


class VBLLModel(AbstractBLLModel):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.model = VBLLNetwork(*args, **kwargs)

    def sample(self, sample_shape: Optional[torch.Size] = None):
        return self.model.sample_posterior_function(sample_shape)

    def __str__(self):
        return self.model.__str__()
