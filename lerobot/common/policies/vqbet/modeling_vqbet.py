import math
from collections import deque
from typing import Callable, List

import einops
import numpy as np
import torch
import torch.nn.functional as F  # noqa: N812
import torchvision
from huggingface_hub import PyTorchModelHubMixin
from torch import Tensor, nn

from lerobot.common.policies.normalize import Normalize, Unnormalize
from lerobot.common.policies.utils import get_device_from_parameters, populate_queues
from lerobot.common.policies.vqbet.configuration_vqbet import VQBeTConfig
from lerobot.common.policies.vqbet.vqvae_utils import ResidualVQ

# ruff: noqa: N806

class VQBeTPolicy(nn.Module, PyTorchModelHubMixin):
    """
    VQ-BeT Policy as per "Behavior Generation with Latent Actions"
    """

    name = "vqbet"
    def __init__(
        self,
        config: VQBeTConfig | None = None,
        dataset_stats: dict[str, dict[str, Tensor]] | None = None,
    ):
        """
        Args:
            config: Policy configuration class instance or None, in which case the default instantiation of
                the configuration class is used.
            dataset_stats: Dataset statistics to be used for normalization. If not passed here, it is expected
                that they will be passed with a call to `load_state_dict` before the policy is used.
        """
        super().__init__()
        if config is None:
            config = VQBeTConfig()
        self.config = config
        self.normalize_inputs = Normalize(
            config.input_shapes, config.input_normalization_modes, dataset_stats
        )
        self.normalize_targets = Normalize(
            config.output_shapes, config.output_normalization_modes, dataset_stats
        )
        self.unnormalize_outputs = Unnormalize(
            config.output_shapes, config.output_normalization_modes, dataset_stats
        )


        # queues are populated during rollout of the policy, they contain the n latest observations and actions
        self._queues = None

        self.vqbet = VQBeTModel(config)

        self.reset()

    def reset(self):
        """
        Clear observation and action queues. Should be called on `env.reset()`
        """
        self._queues = {
            "observation.image": deque(maxlen=self.config.n_obs_steps),
            "observation.state": deque(maxlen=self.config.n_obs_steps),
            "action": deque(maxlen=self.config.action_chunk_size),
        }

    @torch.no_grad
    def select_action(self, batch: dict[str, Tensor]) -> Tensor:
        """Select a single action given environment observations.

        This method wraps `select_actions` in order to return one action at a time for execution in the
        environment. It works by managing the actions in a queue and only calling `select_actions` when the
        queue is empty.
        """


        batch = self.normalize_inputs(batch)
        self._queues = populate_queues(self._queues, batch)

        assert self.vqbet.action_head.vqvae_model.discretized.item(), "To evaluate in the environment, your VQ-BeT model should contain a pretrained Residual VQ."
        assert "observation.image" in batch
        assert "observation.state" in batch

        if len(self._queues["action"]) == 0:

            batch = {key: torch.stack(list(self._queues[key]), dim=1) for key in batch}
            actions = self.vqbet(batch, rollout=True)[:, : self.config.action_chunk_size]

            # the dimension of returned action is (batch_size, action_chunk_size, action_dim)
            actions = self.unnormalize_outputs({"action": actions})["action"]
            # since the data in the action queue's dimension is (action_chunk_size, batch_size, action_dim), we transpose the action and fill the queue
            self._queues["action"].extend(actions.transpose(0, 1))

        action = self._queues["action"].popleft()
        return action

    def forward(self, batch: dict[str, Tensor]) -> dict[str, Tensor]:
        """Run the batch through the model and compute the loss for training or validation."""
        batch = self.normalize_inputs(batch)
        batch = self.normalize_targets(batch)
        # VQ-BeT discretizes action using VQ-VAE before training BeT (please refer to section 3.2 in the VQ-BeT paper https://arxiv.org/pdf/2403.03181)
        if not self.vqbet.action_head.vqvae_model.discretized.item():
            # loss: total loss of training RVQ
            # n_different_codes: how many of total possible codes are being used (max: vqvae_n_embed).
            # n_different_combinations: how many different code combinations you are using out of all possible code combinations (max: vqvae_n_embed ^ vqvae_groups).
            loss, n_different_codes, n_different_combinations = self.vqbet.discretize(self.config.n_vqvae_training_steps, batch['action'])
            return {"loss": loss, "n_different_codes": n_different_codes, "n_different_combinations": n_different_combinations}
        # if Residual VQ is already trained, VQ-BeT trains its GPT and bin prediction head / offset prediction head parts.
        _, loss_dict = self.vqbet(batch, rollout=False)

        return loss_dict

class SpatialSoftmax(nn.Module):
    """
    Spatial Soft Argmax operation described in "Deep Spatial Autoencoders for Visuomotor Learning" by Finn et al.
    (https://arxiv.org/pdf/1509.06113). A minimal port of the robomimic implementation.

    At a high level, this takes 2D feature maps (from a convnet/ViT) and returns the "center of mass"
    of activations of each channel, i.e., keypoints in the image space for the policy to focus on.

    Example: take feature maps of size (512x10x12). We generate a grid of normalized coordinates (10x12x2):
    -----------------------------------------------------
    | (-1., -1.)   | (-0.82, -1.)   | ... | (1., -1.)   |
    | (-1., -0.78) | (-0.82, -0.78) | ... | (1., -0.78) |
    | ...          | ...            | ... | ...         |
    | (-1., 1.)    | (-0.82, 1.)    | ... | (1., 1.)    |
    -----------------------------------------------------
    This is achieved by applying channel-wise softmax over the activations (512x120) and computing the dot
    product with the coordinates (120x2) to get expected points of maximal activation (512x2).

    The example above results in 512 keypoints (corresponding to the 512 input channels). We can optionally
    provide num_kp != None to control the number of keypoints. This is achieved by a first applying a learnable
    linear mapping (in_channels, H, W) -> (num_kp, H, W).
    """

    def __init__(self, input_shape, num_kp=None):
        """
        Args:
            input_shape (list): (C, H, W) input feature map shape.
            num_kp (int): number of keypoints in output. If None, output will have the same number of channels as input.
        """
        super().__init__()

        assert len(input_shape) == 3
        self._in_c, self._in_h, self._in_w = input_shape

        if num_kp is not None:
            self.nets = torch.nn.Conv2d(self._in_c, num_kp, kernel_size=1)
            self._out_c = num_kp
        else:
            self.nets = None
            self._out_c = self._in_c

        # we could use torch.linspace directly but that seems to behave slightly differently than numpy
        # and causes a small degradation in pc_success of pre-trained models.
        pos_x, pos_y = np.meshgrid(np.linspace(-1.0, 1.0, self._in_w), np.linspace(-1.0, 1.0, self._in_h))
        pos_x = torch.from_numpy(pos_x.reshape(self._in_h * self._in_w, 1)).float()
        pos_y = torch.from_numpy(pos_y.reshape(self._in_h * self._in_w, 1)).float()
        # register as buffer so it's moved to the correct device.
        self.register_buffer("pos_grid", torch.cat([pos_x, pos_y], dim=1))

    def forward(self, features: Tensor) -> Tensor:
        """
        Args:
            features: (B, C, H, W) input feature maps.
        Returns:
            (B, K, 2) image-space coordinates of keypoints.
        """
        if self.nets is not None:
            features = self.nets(features)

        # [B, K, H, W] -> [B * K, H * W] where K is number of keypoints
        features = features.reshape(-1, self._in_h * self._in_w)
        # 2d softmax normalization
        attention = F.softmax(features, dim=-1)
        # [B * K, H * W] x [H * W, 2] -> [B * K, 2] for spatial coordinate mean in x and y dimensions
        expected_xy = attention @ self.pos_grid
        # reshape to [B, K, 2]
        feature_keypoints = expected_xy.view(-1, self._out_c, 2)

        return feature_keypoints

class VQBeTModel(nn.Module):
    """VQ-BeT: The underlying neural network for VQ-BeT

    Note: In this code we use the terms `rgb_encoder`, 'policy', `action_head`. The meanings are as follows.
        - The `rgb_encoder` process rgb-style image observations to one-dimensional embedding vectors
        - A `policy` is a minGPT architecture, that takes observation sequences and action query tokens to generate `features`.
        - These `features` pass through the action head, which passes through the code prediction, offset prediction head, 
        and finally generates a prediction for the action chunks.

        -------------------------------** legend **-------------------------------
        │   n = n_obs_steps, p = n_action_pred_token, c = action_chunk_size)   │
        │   o_{t} : visual observation at timestep {t}                           │
        │   s_{t} : state observation at timestep {t}                            │
        │   a_{t} : action at timestep {t}                                       │
        │   A_Q : action_query_token                                             │
        --------------------------------------------------------------------------

        
        Training Phase 1. Discretize action using Residual VQ (for config.n_vqvae_training_steps steps)


        ┌─────────────────┐            ┌─────────────────┐            ┌─────────────────┐
        │                 │            │                 │            │                 │
        │   RVQ encoder   │    ─►      │     Residual    │    ─►      │   RVQ Decoder   │
        │ (a_{t}~a_{t+p}) │            │  Code Quantizer │            │                 │
        │                 │            │                 │            │                 │
        └─────────────────┘            └─────────────────┘            └─────────────────┘

        Training Phase 2.
        
          timestep {t-n+1}   timestep {t-n+2}                timestep {t}
            ┌─────┴─────┐     ┌─────┴─────┐                 ┌─────┴─────┐

        o_{t-n+1}         o_{t-n+2}           ...         o_{t}
            │                 │                             │ 
            │ s_{t-n+1}       │ s_{t-n+2}         ...       │   s_{t}           p
            │     │           │     │                       │     │     ┌───────┴───────┐
            │     │    A_Q    │     │    A_Q          ...   │     │    A_Q     ...     A_Q
            │     │     │     │     │     │                 │     │     │               │
        ┌───▼─────▼─────▼─────▼─────▼─────▼─────────────────▼─────▼─────▼───────────────▼───┐
        │                                                                                   │
        │                                       GPT                                         │       =>    policy
        │                                                                                   │
        └───────────────▼─────────────────▼─────────────────────────────▼───────────────▼───┘
                        │                 │                             │               │
                    ┌───┴───┐         ┌───┴───┐                     ┌───┴───┐       ┌───┴───┐
                  code    offset    code    offset                code    offset  code    offset
                    ▼       │         ▼       │                     ▼       │       ▼       │       =>    action_head
               RVQ Decoder  │    RVQ Decoder  │                RVQ Decoder  │  RVQ Decoder  │
                    └── + ──┘         └── + ──┘                     └── + ──┘       └── + ──┘
                        ▼                 ▼                             ▼               ▼
                   action chunk      action chunk                  action chunk     action chunk
                    a_{t-n+1} ~       a_{t-n+2} ~                   a_{t} ~     ...  a_{t+p-1} ~ 
                     a_{t-n+c}         a_{t-n+c+1}                   a_{t+c-1}        a_{t+p+c-1}

                                                                        ▼
                                                      ONLY this chunk is used in rollout!
    """
    def __init__(self, config: VQBeTConfig):
        super().__init__()
        self.config = config

        self.rgb_encoder = VQBeTRgbEncoder(config)

        # This action query token is used as a prompt for querying action chunks. Please refer to "A_Q" in the image above.
        # Note: During the forward pass, this token is repeated as many times as needed. The authors also experimented with initializing the necessary number of tokens independently and observed inferior results.
        self._action_token = nn.Parameter(torch.randn(1, 1, self.config.gpt_input_dim))

        # To input state and observation features into GPT layers, we first project the features to fit the shape of input size of GPT.
        self.state_projector = MLP(
                config.output_shapes["action"][0], 
                hidden_channels=[self.config.gpt_input_dim]
            )
        self.rgb_feature_projector = MLP(
                self.rgb_encoder.feature_dim, 
                hidden_channels=[self.config.gpt_input_dim]
            )
        
        # GPT part of VQ-BeT
        self.policy = GPT(config)
        # bin prediction head / offset prediction head part of VQ-BeT
        self.action_head = VQBeTHead(config)

        num_tokens = self.config.n_action_pred_token + self.config.action_chunk_size - 1
        self.register_buffer(
                        "select_target_actions_indices",
                        torch.row_stack(
                            [torch.arange(i, i + self.config.action_chunk_size) for i in range(num_tokens)]
                        ),
                    )

    def discretize(self, n_vqvae_training_steps, actions):
        return self.action_head.discretize(n_vqvae_training_steps, actions)

    def forward(self, batch: dict[str, Tensor], rollout: bool) -> Tensor:
        # Input validation.
        assert set(batch).issuperset({"observation.state", "observation.image"})
        batch_size, n_obs_steps = batch["observation.state"].shape[:2]
        assert n_obs_steps == self.config.n_obs_steps

        # Extract image feature (first combine batch and sequence dims).
        img_features = self.rgb_encoder(einops.rearrange(batch["observation.image"], "b n ... -> (b n) ..."))
        # Separate batch and sequence dims.
        img_features = einops.rearrange(img_features, "(b n) ... -> b n ...", b=batch_size)

        # Arrange prior and current observation step tokens as shown in the class docstring.
        # First project features to token dimension.
        rgb_tokens = self.rgb_feature_projector(img_features)  # (batch, obs_step, d)
        state_tokens = self.state_projector(batch["observation.state"])  # (batch, obs_step, d)
        history_action_tokens = einops.repeat(
            self._action_token, "1 1 d -> b n d", b=batch_size, n=n_obs_steps
        )
        # Interleave tokens by stacking and rearranging.
        input_tokens = torch.stack([rgb_tokens, state_tokens, history_action_tokens], dim=2)
        input_tokens = einops.rearrange(input_tokens, "b n t d -> b (n t) d")

        len_additional_action_token = self.config.n_action_pred_token-1
        future_action_tokens = self._action_token.repeat(batch_size, len_additional_action_token, 1)

        # add additional action query tokens for predicting future action chunks
        input_tokens = torch.cat([input_tokens, future_action_tokens], dim=1)

        
        # get action features (pass through GPT)
        features = self.policy(input_tokens)
        # len(self.config.input_shapes) is the number of different observation modes. this line gets the index of action prompt tokens.
        historical_act_pred_index = np.arange(0, n_obs_steps) * (len(self.config.input_shapes)+1) + len(self.config.input_shapes)

        # only extract the output tokens at the position of action query:
        # Behavior Transformer (BeT), and VQ-BeT are both sequence-to-sequence prediction models, mapping sequential observation to sequential action (please refer to section 2.2 in BeT paper https://arxiv.org/pdf/2206.11251).
        # Thus, it predict historical action sequence, in addition to current and future actions (predicting future actions : optional).
        features = torch.cat([
            features[:, historical_act_pred_index],
            features[:, -len_additional_action_token:]
        ], dim=1)
        # pass through action head
        pred_action = self.action_head(features)
        # if rollout, VQ-BeT don't calculate loss
        if rollout:
            return pred_action["predicted_action"][:, n_obs_steps-1, :].reshape(batch_size, self.config.action_chunk_size, -1)
        # else, it calculate overall loss (bin prediction loss, and offset loss)
        else:
            output = batch["action"][:, self.select_target_actions_indices]
            loss = self.action_head.loss_fn(pred_action, output, reduction="mean")
            return pred_action, loss


class VQBeTHead(nn.Module):
    def __init__(self, config: VQBeTConfig):
        """
        VQBeTHead takes output of GPT layers, and pass the feature through bin prediction head (`self.map_to_cbet_preds_bin`), and offset prediction head (`self.map_to_cbet_preds_offset`)

        self.map_to_cbet_preds_bin: outputs probability of each code (for each layer).
            The input dimension of `self.map_to_cbet_preds_bin` is same with the output of GPT, 
            and the output dimension of `self.map_to_cbet_preds_bin` is `self.config.vqvae_groups * self.config.vqvae_n_embed`.
            if the agent select the code sequentially, we use self.map_to_cbet_preds_primary_bin and self.map_to_cbet_preds_secondary_bin instead of self._map_to_cbet_preds_bin.

        self.map_to_cbet_preds_offset: output the predicted offsets for all the codes in all the layers. 
            The input dimension of ` self.map_to_cbet_preds_offset` is same with the output of GPT, 
            and the output dimension of ` self.map_to_cbet_preds_offset` is `self.config.vqvae_groups * self.config.vqvae_n_embed * config.action_chunk_size * config.output_shapes["action"][0]`
        """

        super().__init__()
        self.config = config

        if config.sequentially_select:
            self.map_to_cbet_preds_primary_bin = MLP(
                in_channels=config.gpt_output_dim,
                hidden_channels=[self.config.vqvae_n_embed],
            )
            self.map_to_cbet_preds_secondary_bin = MLP(
                in_channels=config.gpt_output_dim + self.config.vqvae_n_embed,
                hidden_channels=[self.config.vqvae_n_embed],
            )
        else:
            self.map_to_cbet_preds_bin = MLP(
                in_channels=config.gpt_output_dim,
                hidden_channels=[self.config.vqvae_groups * self.config.vqvae_n_embed],
            )
        self.map_to_cbet_preds_offset = MLP(
            in_channels=config.gpt_output_dim,
            hidden_channels=[
                self.config.vqvae_groups * self.config.vqvae_n_embed * config.action_chunk_size * config.output_shapes["action"][0],
            ],
        )
        # init vqvae
        self.vqvae_model = VqVae(config)
        # loss
        self._focal_loss_fn = FocalLoss(gamma=2.0)

    def discretize(self, n_vqvae_training_steps, actions):
        loss, n_different_codes, n_different_combinations = pretrain_vqvae(self.vqvae_model, n_vqvae_training_steps, actions)
        return loss, n_different_codes, n_different_combinations

    def forward(self, x, **kwargs):
        # N is the batch size, and T is number of action query tokens, which are process through same GPT
        N, T, _ = x.shape
        # we calculate N and T side parallely. Thus, the dimensions would be 
        # (batch size * number of action query tokens, action chunk size, action dimension)
        x = einops.rearrange(x, "N T WA -> (N T) WA")

        # sample offsets
        cbet_offsets = self.map_to_cbet_preds_offset(x)
        cbet_offsets = einops.rearrange(
            cbet_offsets, "(NT) (G C WA) -> (NT) G C WA", G=self.config.vqvae_groups, C=self.config.vqvae_n_embed
        )
        # if self.config.sequentially_select is True, bin prediction head first sample the primary code, and then sample secondary code
        if self.config.sequentially_select:
            cbet_primary_logits = self.map_to_cbet_preds_primary_bin(x)

            # select primary bin first
            cbet_primary_probs = torch.softmax(cbet_primary_logits / self.config.bet_softmax_temperature, dim=-1)
            NT, choices = cbet_primary_probs.shape
            sampled_primary_centers = einops.rearrange(
                torch.multinomial(cbet_primary_probs.view(-1, choices), num_samples=1),
                "(NT) 1 -> NT",
                NT=NT,
            )

            cbet_secondary_logits = self.map_to_cbet_preds_secondary_bin(
                torch.cat(
                    (x, F.one_hot(sampled_primary_centers, num_classes=self.config.vqvae_n_embed)),
                    axis=1,
                )
            )
            cbet_secondary_probs = torch.softmax(cbet_secondary_logits / self.config.bet_softmax_temperature, dim=-1)
            sampled_secondary_centers = einops.rearrange(
                torch.multinomial(cbet_secondary_probs.view(-1, choices), num_samples=1),
                "(NT) 1 -> NT",
                NT=NT,
            )
            sampled_centers = torch.stack(
                (sampled_primary_centers, sampled_secondary_centers), axis=1
            )
            cbet_logits = torch.stack([cbet_primary_logits, cbet_secondary_logits], dim=1)
        # if self.config.sequentially_select is False, bin prediction head samples primary and secondary code at once.
        else:
            cbet_logits = self.map_to_cbet_preds_bin(x)
            cbet_logits = einops.rearrange(
                cbet_logits, "(NT) (G C) -> (NT) G C", G=self.config.vqvae_groups
            )
            cbet_probs = torch.softmax(cbet_logits / self.config.bet_softmax_temperature, dim=-1)
            NT, G, choices = cbet_probs.shape
            sampled_centers = einops.rearrange(
                torch.multinomial(cbet_probs.view(-1, choices), num_samples=1),
                "(NT G) 1 -> NT G",
                NT=NT,
            )
            
        device = get_device_from_parameters(self)
        indices = (
            torch.arange(NT, device=device).unsqueeze(1),
            torch.arange(self.config.vqvae_groups, device=device).unsqueeze(0),
            sampled_centers,
        )
        # Use advanced indexing to sample the values (Extract the only offsets corresponding to the sampled codes.)
        sampled_offsets = cbet_offsets[indices]
        # Then, sum the offsets over the RVQ layers to get a net offset for the bin prediction
        sampled_offsets = sampled_offsets.sum(dim=1)
        with torch.no_grad():
            # Get the centroids (= vectors corresponding to the codes) of each layer to pass it through RVQ decoder
            return_decoder_input = self.vqvae_model.get_embeddings_from_code(sampled_centers).clone().detach()
            # pass the centroids through decoder to get actions.
            decoded_action = (
                self.vqvae_model.get_action_from_latent(return_decoder_input)
                .clone()
                .detach()
            )
        # reshaped extracted offset to match with decoded centroids
        sampled_offsets = einops.rearrange(
            sampled_offsets, "NT (W A) -> NT W A", W=self.config.action_chunk_size
        )
        # add offset and decoded centroids
        predicted_action = decoded_action + sampled_offsets
        predicted_action = einops.rearrange(
            predicted_action,
            "(N T) W A -> N T (W A)",
            N=N,
            T=T,
            W=self.config.action_chunk_size,
        )

        return {
            "cbet_logits": cbet_logits,
            "predicted_action": predicted_action,
            "sampled_centers": sampled_centers,
            "decoded_action": decoded_action, 
        }

    def loss_fn(self, pred, target, **kwargs):
        """
        for given ground truth action values (target), and prediction (pred) this function calculates the overall loss.

        predicted_action: predicted action chunk (offset + decoded centroids)
        sampled_centers: sampled centroids (code of RVQ)
        decoded_action: decoded action, which is produced by passing sampled_centers through RVQ decoder
        NT: batch size * T
        T: number of action query tokens, which are process through same GPT
        cbet_logits: probability of all codes in each layer
        """
        action_seq = target
        predicted_action = pred["predicted_action"]
        sampled_centers = pred["sampled_centers"]
        decoded_action = pred["decoded_action"]
        NT = predicted_action.shape[0] * predicted_action.shape[1]

        cbet_logits = pred["cbet_logits"]

        predicted_action = einops.rearrange(
            predicted_action, "N T (W A) -> (N T) W A", W=self.config.action_chunk_size
        )

        action_seq = einops.rearrange(action_seq, "N T W A -> (N T) W A")
        # Figure out the loss for the actions.
        # First, we need to find the closest cluster center for each ground truth action.
        with torch.no_grad():
            state_vq, action_bins = self.vqvae_model.get_code(
                action_seq
            )  # action_bins: NT, G

        # Now we can compute the loss.

        # offset loss is L1 distance between the predicted action and ground truth action
        offset_loss = F.l1_loss(action_seq, predicted_action)

        # calculate primary code prediction loss
        cbet_loss1 = self._focal_loss_fn(
            cbet_logits[:, 0, :],
            action_bins[:, 0],
        )
        # calculate secondary code prediction loss (if there are more than 2 layers in RVQ, then this part will calculate all the loss for remaining layers together)
        cbet_loss2 = self._focal_loss_fn(
            cbet_logits[:, 1:, :],
            action_bins[:, 1:],
        )
        # add all the prediction loss
        cbet_loss = cbet_loss1 * self.config.primary_code_loss_weight + cbet_loss2 * self.config.secondary_code_loss_weight

        equal_primary_code_rate = torch.sum(
            (action_bins[:, 0] == sampled_centers[:, 0]).int()
        ) / (NT)
        equal_secondary_code_rate = torch.sum(
            (action_bins[:, 1] == sampled_centers[:, 1]).int()
        ) / (NT)

        action_mse_error = torch.mean((action_seq - predicted_action) ** 2)
        vq_action_error = torch.mean(torch.abs(action_seq - decoded_action))
        offset_action_error = torch.mean(torch.abs(action_seq - predicted_action))
        action_error_max = torch.max(torch.abs(action_seq - predicted_action))

        loss = cbet_loss + self.config.offset_loss_weight * offset_loss

        loss_dict = {
            "loss": loss,
            "classification_loss": cbet_loss.detach().cpu().item(),
            "offset_loss": offset_loss.detach().cpu().item(),
            "equal_primary_code_rate": equal_primary_code_rate.detach().cpu().item(),
            "equal_secondary_code_rate": equal_secondary_code_rate.detach().cpu().item(),
            "vq_action_error": vq_action_error.detach().cpu().item(),
            "offset_action_error": offset_action_error.detach().cpu().item(),
            "action_error_max": action_error_max.detach().cpu().item(),
            "action_mse_error": action_mse_error.detach().cpu().item(),

        }
        return loss_dict

class VQBeTOptimizer(torch.optim.Adam):
    def __init__(self, policy, cfg):
        vqvae_params = (
            list(policy.vqbet.action_head.vqvae_model.encoder.parameters())
            + list(policy.vqbet.action_head.vqvae_model.decoder.parameters())
            + list(policy.vqbet.action_head.vqvae_model.vq_layer.parameters())
        )
        decay_params, no_decay_params = policy.vqbet.policy.configure_parameters()
        decay_params = (
            decay_params
            + list(policy.vqbet.rgb_encoder.parameters())
            + list(policy.vqbet.state_projector.parameters())
            + list(policy.vqbet.rgb_feature_projector.parameters())
            + [policy.vqbet._action_token]
            + list(policy.vqbet.action_head.map_to_cbet_preds_offset.parameters())
        )

        if cfg.policy.sequentially_select:
            decay_params = (
                decay_params
                + list(policy.vqbet.action_head.map_to_cbet_preds_primary_bin.parameters())
                + list(policy.vqbet.action_head.map_to_cbet_preds_secondary_bin.parameters())
            )
        else:
            decay_params = (
                decay_params
                + list(policy.vqbet.action_head.map_to_cbet_preds_bin.parameters())
            )

        optim_groups = [
            {
                "params": decay_params,
                "weight_decay": cfg.training.adam_weight_decay,
                "lr": cfg.training.lr,
            },
            {
                "params": vqvae_params,
                "weight_decay": 0.0001,
                "lr": cfg.training.vqvae_lr,
            },
            {
                "params": no_decay_params,
                "weight_decay": 0.0,
                "lr": cfg.training.lr,
            },
        ]
        super(VQBeTOptimizer, self).__init__(
            optim_groups,
            cfg.training.lr,
            cfg.training.adam_betas,
            cfg.training.adam_eps,
        )

class VQBeTScheduler(nn.Module):
    def __init__(self, optimizer, cfg):
        super().__init__()
        # VQ-BeT use scheduler only for rgb encoder. Since we took rgb encoder part from diffusion policy, we also follow the same scheduler from it.
        from diffusers.optimization import get_scheduler
        self.n_vqvae_training_steps = cfg.training.n_vqvae_training_steps
        self.optimizing_step = 0

        self.lr_scheduler = get_scheduler(
            cfg.training.lr_scheduler,
            optimizer=optimizer,
            num_warmup_steps=cfg.training.lr_warmup_steps,
            num_training_steps=cfg.training.offline_steps,
        )


    def step(self):
        self.optimizing_step +=1
        if self.optimizing_step >= self.n_vqvae_training_steps:
            self.lr_scheduler.step()

class VQBeTRgbEncoder(nn.Module):
    """Encode an RGB image into a 1D feature vector.

    Includes the ability to normalize and crop the image first.

    Same with DiffusionRgbEncoder from modeling_diffusion.py
    """

    def __init__(self, config: VQBeTConfig):
        super().__init__()
        # Set up optional preprocessing.
        if config.crop_shape is not None:
            self.do_crop = True
            # Always use center crop for eval
            self.center_crop = torchvision.transforms.CenterCrop(config.crop_shape)
            if config.crop_is_random:
                self.maybe_random_crop = torchvision.transforms.RandomCrop(config.crop_shape)
            else:
                self.maybe_random_crop = self.center_crop
        else:
            self.do_crop = False

        # Set up backbone.
        backbone_model = getattr(torchvision.models, config.vision_backbone)(
            weights=config.pretrained_backbone_weights
        )
        # Note: This assumes that the layer4 feature map is children()[-3]
        # TODO(alexander-soare): Use a safer alternative.
        self.backbone = nn.Sequential(*(list(backbone_model.children())[:-2]))
        if config.use_group_norm:
            if config.pretrained_backbone_weights:
                raise ValueError(
                    "You can't replace BatchNorm in a pretrained model without ruining the weights!"
                )
            self.backbone = _replace_submodules(
                root_module=self.backbone,
                predicate=lambda x: isinstance(x, nn.BatchNorm2d),
                func=lambda x: nn.GroupNorm(num_groups=x.num_features // 16, num_channels=x.num_features),
            )

        # Set up pooling and final layers.
        # Use a dry run to get the feature map shape.
        # The dummy input should take the number of image channels from `config.input_shapes` and it should
        # use the height and width from `config.crop_shape` if it is provided, otherwise it should use the
        # height and width from `config.input_shapes`.
        image_keys = [k for k in config.input_shapes if k.startswith("observation.image")]
        assert len(image_keys) == 1
        image_key = image_keys[0]
        dummy_input_h_w = (
            config.crop_shape if config.crop_shape is not None else config.input_shapes[image_key][1:]
        )
        dummy_input = torch.zeros(size=(1, config.input_shapes[image_key][0], *dummy_input_h_w))
        with torch.inference_mode():
            dummy_feature_map = self.backbone(dummy_input)
        feature_map_shape = tuple(dummy_feature_map.shape[1:])
        self.pool = SpatialSoftmax(feature_map_shape, num_kp=config.spatial_softmax_num_keypoints)
        self.feature_dim = config.spatial_softmax_num_keypoints * 2
        self.out = nn.Linear(config.spatial_softmax_num_keypoints * 2, self.feature_dim)
        self.relu = nn.ReLU()

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: (B, C, H, W) image tensor with pixel values in [0, 1].
        Returns:
            (B, D) image feature.
        """
        # Preprocess: maybe crop (if it was set up in the __init__).
        if self.do_crop:
            if self.training:  # noqa: SIM108
                x = self.maybe_random_crop(x)
            else:
                # Always use center crop for eval.
                x = self.center_crop(x)
        # Extract backbone feature.
        x = torch.flatten(self.pool(self.backbone(x)), start_dim=1)
        # Final linear layer with non-linearity.
        x = self.relu(self.out(x))
        return x


def _replace_submodules(
    root_module: nn.Module, predicate: Callable[[nn.Module], bool], func: Callable[[nn.Module], nn.Module]
) -> nn.Module:
    """
    Args:
        root_module: The module for which the submodules need to be replaced
        predicate: Takes a module as an argument and must return True if the that module is to be replaced.
        func: Takes a module as an argument and returns a new module to replace it with.
    Returns:
        The root module with its submodules replaced.
    """
    if predicate(root_module):
        return func(root_module)

    replace_list = [k.split(".") for k, m in root_module.named_modules(remove_duplicate=True) if predicate(m)]
    for *parents, k in replace_list:
        parent_module = root_module
        if len(parents) > 0:
            parent_module = root_module.get_submodule(".".join(parents))
        if isinstance(parent_module, nn.Sequential):
            src_module = parent_module[int(k)]
        else:
            src_module = getattr(parent_module, k)
        tgt_module = func(src_module)
        if isinstance(parent_module, nn.Sequential):
            parent_module[int(k)] = tgt_module
        else:
            setattr(parent_module, k, tgt_module)
    # verify that all BN are replaced
    assert not any(predicate(m) for _, m in root_module.named_modules(remove_duplicate=True))
    return root_module




class VqVae(nn.Module):
    def __init__(
        self, config: VQBeTConfig,
    ):
        """
        VQ-VAE is composed of three parts: encoder, vq_layer, and decoder.
        Encoder and decoder are MLPs consisting of an input, output layer, and hidden layer, respectively.
        The vq_layer uses residual VQs.

        This class contains functions for training the encoder and decoder along with the residual VQ layer (for trainign phase 1), 
        as well as functions to help BeT training part in training phase 2.
        """

        super(VqVae, self).__init__()
        self.config = config
        # 'discretized' indicates whether the Residual VQ part is trained or not. (After finishing the training, we set discretized=True)
        self.register_buffer('discretized', torch.tensor(False))
        self.optimized_steps = 0

        self.vq_layer = ResidualVQ(
            dim=config.vqvae_embedding_dim,
            num_quantizers=config.vqvae_groups,
            codebook_size=config.vqvae_n_embed,
        )

        self.encoder = MLP(
            in_channels=self.config.output_shapes["action"][0] * self.config.action_chunk_size,
            hidden_channels=[config.vqvae_enc_hidden_dim, config.vqvae_enc_hidden_dim, config.vqvae_embedding_dim],
        )
        self.decoder = MLP(
            in_channels=config.vqvae_embedding_dim,
            hidden_channels=[config.vqvae_enc_hidden_dim, config.vqvae_enc_hidden_dim, self.config.output_shapes["action"][0] * self.config.action_chunk_size],
        )

    def get_embeddings_from_code(self, encoding_indices):
        # This function gets code indices as inputs, and outputs embedding vectors corresponding to the code indices.
        with torch.no_grad():
            z_embed = self.vq_layer.get_codebook_vector_from_indices(encoding_indices)
            # since the RVQ has multiple layers, it adds the vectors in the axis of layers to provide a vector for that code combination.
            z_embed = z_embed.sum(dim=0)
        return z_embed

    def get_action_from_latent(self, latent):
        # given latent vector, this function outputs the decoded action. 
        output = self.decoder(latent)
        if self.config.action_chunk_size == 1:
            return einops.rearrange(output, "N (T A) -> N T A", A=self.config.output_shapes["action"][0])
        else:
            return einops.rearrange(output, "N (T A) -> N T A", A=self.config.output_shapes["action"][0])

    def get_code(self, state):
        # in phase 2 of VQ-BeT training, we need a `GT code` to calculate the Focal loss for code prediction head.
        # this function outputs the `GT code` of given action using frozen encoder and quantization layers. (please refer to Figure 2. in the paper https://arxiv.org/pdf/2403.03181)
        state = einops.rearrange(state, "N T A -> N (T A)")
        with torch.no_grad():
            state_rep = self.encoder(state)
            state_rep_shape = state_rep.shape[:-1]
            state_rep_flat = state_rep.view(state_rep.size(0), -1, state_rep.size(1))
            state_rep_flat, vq_code, vq_loss_state = self.vq_layer(state_rep_flat)
            state_vq = state_rep_flat.view(*state_rep_shape, -1)
            vq_code = vq_code.view(*state_rep_shape, -1)
            vq_loss_state = torch.sum(vq_loss_state)
            return state_vq, vq_code

    def vqvae_forward(self, state):
        # This function passes the given data through Residual VQ with Encoder and Decoder. Please refer to section 3.2 in the paper https://arxiv.org/pdf/2403.03181).
        state = einops.rearrange(state, "N T A -> N (T A)")
        # We start with passing action (or action chunk) at:t+n through the encoder ϕ. 
        state_rep = self.encoder(state)
        state_rep_shape = state_rep.shape[:-1]
        state_rep_flat = state_rep.view(state_rep.size(0), -1, state_rep.size(1))
        # The resulting latent embedding vector x = ϕ(at:t+n) is then mapped to an embedding vector in the codebook of the RVQ layers by the nearest neighbor look-up. 
        state_rep_flat, vq_code, vq_loss_state = self.vq_layer(state_rep_flat)
        state_vq = state_rep_flat.view(*state_rep_shape, -1)
        vq_code = vq_code.view(*state_rep_shape, -1)
        # since the RVQ has multiple layers, it adds the vectors in the axis of layers to provide a vector for that code combination.
        vq_loss_state = torch.sum(vq_loss_state)
        # Then, the discretized vector zq(x) is reconstructed as ψ(zq(x)) by passing through the decoder ψ.
        dec_out = self.decoder(state_vq)
        # Calculate L1 reconstruction loss
        encoder_loss = (state - dec_out).abs().mean()
        # add encoder reconstruction loss and commitment loss
        rep_loss = encoder_loss + vq_loss_state * 5

        metric = (
            encoder_loss.clone().detach(),
            vq_loss_state.clone().detach(),
            vq_code,
            rep_loss.item(),
        )
        return rep_loss, metric




def pretrain_vqvae(vqvae_model, n_vqvae_training_steps, actions):
    if vqvae_model.config.action_chunk_size == 1:
        # not using action chunk
        actions = actions.reshape(-1, 1, actions.shape[-1])
    else:
        # using action chunk
        slices = []
        slices.extend([actions[:, j:j+vqvae_model.config.action_chunk_size, :] for j in range(actions.shape[1]+1-vqvae_model.config.action_chunk_size)])
        actions = torch.cat(slices, dim=0)


    actions = actions.to(get_device_from_parameters(vqvae_model))

    loss, metric = vqvae_model.vqvae_forward(
        actions
    )
    n_different_codes = len(torch.unique(metric[2]))
    n_different_combinations = len(torch.unique(metric[2], dim=0))
    vqvae_model.optimized_steps += 1
    # if we updated RVQ more than `n_vqvae_training_steps` steps, we freeze the RVQ part.
    if vqvae_model.optimized_steps >= n_vqvae_training_steps:
        vqvae_model.discretized = torch.tensor(True)
        vqvae_model.vq_layer.freeze_codebook = torch.tensor(True)
        print("Finished discretizing action data!")
        vqvae_model.eval()
        for param in vqvae_model.vq_layer.parameters():
            param.requires_grad = False
    return loss, n_different_codes, n_different_combinations


class FocalLoss(nn.Module):
    """
    From https://github.com/notmahi/miniBET/blob/main/behavior_transformer/bet.py
    """

    def __init__(self, gamma: float = 0, size_average: bool = True):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.size_average = size_average

    def forward(self, input, target):
        if len(input.shape) == 3:
            N, T, _ = input.shape
            logpt = F.log_softmax(input, dim=-1)
            logpt = logpt.gather(-1, target.view(N, T, 1)).view(N, T)
        elif len(input.shape) == 2:
            logpt = F.log_softmax(input, dim=-1)
            logpt = logpt.gather(-1, target.view(-1, 1)).view(-1)
        pt = logpt.exp()

        loss = -1 * (1 - pt) ** self.gamma * logpt
        if self.size_average:
            return loss.mean()
        else:
            return loss.sum()

class MLP(torch.nn.Sequential):

    def __init__(
        self,
        in_channels: int,
        hidden_channels: List[int],
    ):

        layers = []
        in_dim = in_channels
        for hidden_dim in hidden_channels[:-1]:
            layers.append(torch.nn.Linear(in_dim, hidden_dim))
            layers.append(torch.nn.ReLU())
            in_dim = hidden_dim

        layers.append(torch.nn.Linear(in_dim, hidden_channels[-1]))

        super().__init__(*layers)



"""
This is a part for nanoGPT that utilizes code from the following repository:

    - Andrej Karpathy's nanoGPT implementation in PyTorch.
        Original source: https://github.com/karpathy/nanoGPT

    - The nanoGPT code is licensed under the MIT License:

    MIT License

    Copyright (c) 2022 Andrej Karpathy

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

    - We've made some changes to the original code to adapt it to our needs.

        Changed variable names:
            - n_head -> gpt_n_head
            - n_embd -> gpt_hidden_dim
            - block_size -> gpt_block_size
            - n_layer -> gpt_n_layer
        
        
        class GPT(nn.Module):
            - removed unused functions `def generate`, `def estimate_mfu`, and `def from_pretrained`
            - changed the `configure_optimizers` to `def configure_parameters` and made it to return only the parameters of the model: we use an external optimizer in our training loop.
            - in the function `forward`, we removed target loss calculation parts, since it will be calculated in the training loop (after passing through bin prediction and offset prediction heads).
        
"""

class CausalSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        assert config.gpt_hidden_dim % config.gpt_n_head == 0
        # key, query, value projections for all heads, but in a batch
        self.c_attn = nn.Linear(config.gpt_hidden_dim, 3 * config.gpt_hidden_dim)
        # output projection
        self.c_proj = nn.Linear(config.gpt_hidden_dim, config.gpt_hidden_dim)
        # regularization
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        # causal mask to ensure that attention is only applied to the left in the input sequence
        self.register_buffer(
            "bias",
            torch.tril(torch.ones(config.gpt_block_size, config.gpt_block_size)).view(
                1, 1, config.gpt_block_size, config.gpt_block_size
            ),
        )
        self.gpt_n_head = config.gpt_n_head
        self.gpt_hidden_dim = config.gpt_hidden_dim

    def forward(self, x):
        (
            B,
            T,
            C,
        ) = x.size()  # batch size, sequence length, embedding dimensionality (gpt_hidden_dim)

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        q, k, v = self.c_attn(x).split(self.gpt_hidden_dim, dim=2)
        k = k.view(B, T, self.gpt_n_head, C // self.gpt_n_head).transpose(
            1, 2
        )  # (B, nh, T, hs)
        q = q.view(B, T, self.gpt_n_head, C // self.gpt_n_head).transpose(
            1, 2
        )  # (B, nh, T, hs)
        v = v.view(B, T, self.gpt_n_head, C // self.gpt_n_head).transpose(
            1, 2
        )  # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.bias[:, :, :T, :T] == 0, float("-inf"))
        att = F.softmax(att, dim=-1)
        att = self.attn_dropout(att)
        y = att @ v  # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = (
            y.transpose(1, 2).contiguous().view(B, T, C)
        )  # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y



class Block(nn.Module):
    # causual self-attention block for GPT
    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.gpt_hidden_dim)
        self.attn = CausalSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.gpt_hidden_dim)
        self.mlp = nn.Sequential(
                    nn.Linear(config.gpt_hidden_dim, 4 * config.gpt_hidden_dim),
                    nn.GELU(),
                    nn.Linear(4 * config.gpt_hidden_dim, config.gpt_hidden_dim),
                    nn.Dropout(config.dropout)
                )

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x




class GPT(nn.Module):
    """
    Original comments:
    Full definition of a GPT Language Model, all of it in this single file.
    References:
    1) the official GPT-2 TensorFlow implementation released by OpenAI:
    https://github.com/openai/gpt-2/blob/master/src/model.py
    2) huggingface/transformers PyTorch implementation:
    https://github.com/huggingface/transformers/blob/main/src/transformers/models/gpt2/modeling_gpt2.py
    """


    def __init__(self, config: VQBeTConfig):
        """
        GPT model gets hyperparameters from a config object. Please refer configuration_vqbet.py for more details.
        """
        super().__init__()
        assert config.gpt_output_dim is not None
        assert config.gpt_block_size is not None
        self.config = config

        self.transformer = nn.ModuleDict(
            {
                "wte": nn.Linear(config.gpt_input_dim, config.gpt_hidden_dim),
                "wpe": nn.Embedding(config.gpt_block_size, config.gpt_hidden_dim),
                "drop": nn.Dropout(config.dropout),
                "h": nn.ModuleList([Block(config) for _ in range(config.gpt_n_layer)]),
                "ln_f": nn.LayerNorm(config.gpt_hidden_dim),
            }
        )
        self.lm_head = nn.Linear(config.gpt_hidden_dim, config.gpt_output_dim, bias=False)
        # init all weights, and apply a special scaled init to the residual projections, per GPT-2 paper
        self.apply(self._init_weights)
        for pn, p in self.named_parameters():
            if pn.endswith("c_proj.weight"):
                torch.nn.init.normal_(
                    p, mean=0.0, std=0.02 / math.sqrt(2 * config.gpt_n_layer)
                )

        # report number of parameters
        n_params = sum(p.numel() for p in self.parameters())
        print("number of parameters: %.2fM" % (n_params / 1e6,))

    def forward(self, input, targets=None):
        device = input.device
        b, t, d = input.size()
        assert (
            t <= self.config.gpt_block_size
        ), f"Cannot forward sequence of length {t}, block size is only {self.config.gpt_block_size}"

        # positional encodings that are added to the input embeddings
        pos = torch.arange(0, t, dtype=torch.long, device=device).unsqueeze(
            0
        )  # shape (1, t)

        # forward the GPT model itself
        tok_emb = self.transformer.wte(
            input
        )  # token embeddings of shape (b, t, gpt_hidden_dim)
        pos_emb = self.transformer.wpe(
            pos
        )  # position embeddings of shape (1, t, gpt_hidden_dim)
        x = self.transformer.drop(tok_emb + pos_emb)
        for block in self.transformer.h:
            x = block(x)
        x = self.transformer.ln_f(x)
        logits = self.lm_head(x)
        return logits

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
        elif isinstance(module, nn.LayerNorm):
            torch.nn.init.zeros_(module.bias)
            torch.nn.init.ones_(module.weight)

    def crop_block_size(self, gpt_block_size):
        # model surgery to decrease the block size if necessary
        # e.g. we may load the GPT2 pretrained model checkpoint (block size 1024)
        # but want to use a smaller block size for some smaller, simpler model
        assert gpt_block_size <= self.config.gpt_block_size
        self.config.gpt_block_size = gpt_block_size
        self.transformer.wpe.weight = nn.Parameter(
            self.transformer.wpe.weight[:gpt_block_size]
        )
        for block in self.transformer.h:
            block.attn.bias = block.attn.bias[:, :, :gpt_block_size, :gpt_block_size]

    def configure_parameters(self):
        """
        This long function is unfortunately doing something very simple and is being very defensive:
        We are separating out all parameters of the model into two buckets: those that will experience
        weight decay for regularization and those that won't (biases, and layernorm/embedding weights).
        """

        # separate out all parameters to those that will and won't experience regularizing weight decay
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear,)
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.Embedding)
        for mn, m in self.named_modules():
            for pn, _p in m.named_parameters():
                fpn = "%s.%s" % (mn, pn) if mn else pn  # full param name
                if pn.endswith("bias"):
                    # all biases will not be decayed
                    no_decay.add(fpn)
                elif pn.endswith("weight") and isinstance(m, whitelist_weight_modules):
                    # weights of whitelist modules will be weight decayed
                    decay.add(fpn)
                elif pn.endswith("weight") and isinstance(m, blacklist_weight_modules):
                    # weights of blacklist modules will NOT be weight decayed
                    no_decay.add(fpn)

        # validate that we considered every parameter
        param_dict = dict(self.named_parameters())
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert (
            len(inter_params) == 0
        ), "parameters %s made it into both decay/no_decay sets!" % (str(inter_params),)
        assert (
            len(param_dict.keys() - union_params) == 0
        ), "parameters %s were not separated into either decay/no_decay set!" % (
            str(param_dict.keys() - union_params),
        )

        decay = [param_dict[pn] for pn in sorted(decay)]
        no_decay = [param_dict[pn] for pn in sorted(no_decay)]
        # return the parameters that require weight decay, and the parameters that don't separately.
        return decay, no_decay
