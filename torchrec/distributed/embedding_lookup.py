#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import logging
from abc import ABC
from collections import OrderedDict
from typing import Any, cast, Dict, Iterator, List, Optional, Tuple, Union

import torch
import torch.distributed as dist
from fbgemm_gpu.split_table_batched_embeddings_ops_inference import (
    IntNBitTableBatchedEmbeddingBagsCodegen,
)
from fbgemm_gpu.split_table_batched_embeddings_ops_training import (
    SplitTableBatchedEmbeddingBagsCodegen,
)
from torch import nn

from torch.autograd.function import FunctionCtx
from torch.nn.modules.module import _IncompatibleKeys
from torchrec.distributed.batched_embedding_kernel import (
    BaseBatchedEmbedding,
    BaseBatchedEmbeddingBag,
    BatchedDenseEmbedding,
    BatchedDenseEmbeddingBag,
    BatchedFusedEmbedding,
    BatchedFusedEmbeddingBag,
)
from torchrec.distributed.comm_ops import get_gradient_division
from torchrec.distributed.composable.table_batched_embedding_slice import (
    TableBatchedEmbeddingSlice,
)
from torchrec.distributed.embedding_kernel import BaseEmbedding
from torchrec.distributed.embedding_types import (
    BaseEmbeddingLookup,
    BaseGroupedFeatureProcessor,
    EmbeddingComputeKernel,
    GroupedEmbeddingConfig,
    KJTList,
)
from torchrec.distributed.fused_params import (
    get_tbes_to_register_from_iterable,
    TBEToRegisterMixIn,
)
from torchrec.distributed.quant_embedding_kernel import (
    QuantBatchedEmbedding,
    QuantBatchedEmbeddingBag,
)
from torchrec.distributed.types import ShardedTensor
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor

logger: logging.Logger = logging.getLogger(__name__)


@torch.fx.wrap
def fx_wrap_tensor_view2d(x: torch.Tensor, dim0: int, dim1: int) -> torch.Tensor:
    return x.view(dim0, dim1)


def _load_state_dict(
    emb_modules: "nn.ModuleList",
    state_dict: "OrderedDict[str, Union[torch.Tensor, ShardedTensor]]",
) -> Tuple[List[str], List[str]]:
    missing_keys = []
    unexpected_keys = list(state_dict.keys())
    for emb_module in emb_modules:
        for key, dst_param in emb_module.state_dict().items():
            if key in state_dict:
                src_param = state_dict[key]
                if isinstance(dst_param, ShardedTensor):
                    assert isinstance(src_param, ShardedTensor)
                    assert len(dst_param.local_shards()) == len(
                        src_param.local_shards()
                    )
                    for dst_local_shard, src_local_shard in zip(
                        dst_param.local_shards(), src_param.local_shards()
                    ):
                        assert (
                            dst_local_shard.metadata.shard_offsets
                            == src_local_shard.metadata.shard_offsets
                        )
                        assert (
                            dst_local_shard.metadata.shard_sizes
                            == src_local_shard.metadata.shard_sizes
                        )

                        dst_local_shard.tensor.detach().copy_(src_local_shard.tensor)
                else:
                    assert isinstance(src_param, torch.Tensor) and isinstance(
                        dst_param, torch.Tensor
                    )
                    dst_param.detach().copy_(src_param)
                unexpected_keys.remove(key)
            else:
                missing_keys.append(cast(str, key))
    return missing_keys, unexpected_keys


@torch.fx.wrap
def embeddings_cat_empty_rank_handle(
    embeddings: List[torch.Tensor],
    dummy_embs_tensor: torch.Tensor,
    dim: int = 0,
) -> torch.Tensor:
    if len(embeddings) == 0:
        # a hack for empty ranks
        return dummy_embs_tensor
    elif len(embeddings) == 1:
        return embeddings[0]
    else:
        return torch.cat(embeddings, dim=dim)


@torch.fx.wrap
def embeddings_cat_empty_rank_handle_inference(
    embeddings: List[torch.Tensor],
    dim: int = 0,
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
) -> torch.Tensor:
    if len(embeddings) == 0:
        # return a dummy empty tensor when grouped_configs is empty
        return torch.empty([0], dtype=dtype, device=device)
    elif len(embeddings) == 1:
        return embeddings[0]
    else:
        return torch.cat(embeddings, dim=dim)


class GroupedEmbeddingsLookup(BaseEmbeddingLookup[KeyedJaggedTensor, torch.Tensor]):
    """
    Lookup modules for Sequence embeddings (i.e Embeddings)
    """

    def __init__(
        self,
        grouped_configs: List[GroupedEmbeddingConfig],
        pg: Optional[dist.ProcessGroup] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        # TODO rename to _create_embedding_kernel
        def _create_lookup(
            config: GroupedEmbeddingConfig,
        ) -> BaseEmbedding:
            for table in config.embedding_tables:
                if table.compute_kernel == EmbeddingComputeKernel.FUSED_UVM_CACHING:
                    self._need_prefetch = True
            if config.compute_kernel == EmbeddingComputeKernel.DENSE:
                return BatchedDenseEmbedding(
                    config=config,
                    pg=pg,
                    device=device,
                )
            elif config.compute_kernel == EmbeddingComputeKernel.FUSED:
                return BatchedFusedEmbedding(
                    config=config,
                    pg=pg,
                    device=device,
                )
            else:
                raise ValueError(
                    f"Compute kernel not supported {config.compute_kernel}"
                )

        super().__init__()
        self._emb_modules: nn.ModuleList = nn.ModuleList()
        self._need_prefetch: bool = False
        for config in grouped_configs:
            self._emb_modules.append(_create_lookup(config))

        self._feature_splits: List[int] = []
        for config in grouped_configs:
            self._feature_splits.append(config.num_features())

        # return a dummy empty tensor when grouped_configs is empty
        self.register_buffer(
            "_dummy_embs_tensor",
            torch.empty(
                [0],
                dtype=torch.float32,
                device=device,
                requires_grad=True,
            ),
        )

        self.grouped_configs = grouped_configs

    def prefetch(
        self,
        sparse_features: KeyedJaggedTensor,
        forward_stream: Optional[torch.cuda.Stream] = None,
    ) -> None:
        if not self._need_prefetch:
            return
        if len(self._emb_modules) > 0:
            assert sparse_features is not None
            features_by_group = sparse_features.split(
                self._feature_splits,
            )
            for emb_op, features in zip(self._emb_modules, features_by_group):
                if (
                    isinstance(emb_op.emb_module, SplitTableBatchedEmbeddingBagsCodegen)
                    and not emb_op.emb_module.prefetch_pipeline
                ):
                    logging.error(
                        "Invalid setting on SplitTableBatchedEmbeddingBagsCodegen modules. prefetch_pipeline must be set to True.\n"
                        "If you don’t turn on prefetch_pipeline, cache locations might be wrong in backward and can cause wrong results.\n"
                    )
                if hasattr(emb_op.emb_module, "prefetch"):
                    emb_op.emb_module.prefetch(
                        indices=features.values(),
                        offsets=features.offsets(),
                        forward_stream=forward_stream,
                    )

    def forward(
        self,
        sparse_features: KeyedJaggedTensor,
    ) -> torch.Tensor:
        embeddings: List[torch.Tensor] = []
        features_by_group = sparse_features.split(
            self._feature_splits,
        )
        for emb_op, features in zip(self._emb_modules, features_by_group):
            embeddings.append(emb_op(features).view(-1))

        return embeddings_cat_empty_rank_handle(embeddings, self._dummy_embs_tensor)

    # pyre-fixme[14]: `state_dict` overrides method defined in `Module` inconsistently.
    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()

        for emb_module in self._emb_modules:
            emb_module.state_dict(destination, prefix, keep_vars)

        return destination

    # pyre-fixme[14]: `load_state_dict` overrides method defined in `Module`
    #  inconsistently.
    def load_state_dict(
        self,
        state_dict: "OrderedDict[str, Union[torch.Tensor, ShardedTensor]]",
        strict: bool = True,
    ) -> _IncompatibleKeys:
        m, u = _load_state_dict(self._emb_modules, state_dict)
        return _IncompatibleKeys(missing_keys=m, unexpected_keys=u)

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.nn.Parameter]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_parameters for"
            "GroupedEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_parameters(prefix, recurse)

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_buffers for"
            "GroupedEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_buffers(prefix, recurse)

    def named_parameters_by_table(
        self,
    ) -> Iterator[Tuple[str, TableBatchedEmbeddingSlice]]:
        """
        Like named_parameters(), but yields table_name and embedding_weights which are wrapped in TableBatchedEmbeddingSlice.
        For a single table with multiple shards (i.e CW) these are combined into one table/weight.
        Used in composability.
        """
        for embedding_kernel in self._emb_modules:
            for (
                table_name,
                tbe_slice,
            ) in embedding_kernel.named_parameters_by_table():
                yield (table_name, tbe_slice)

    def flush(self) -> None:
        for emb_module in self._emb_modules:
            emb_module.flush()

    def purge(self) -> None:
        for emb_module in self._emb_modules:
            emb_module.purge()


class CommOpGradientScaling(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx: FunctionCtx, input_tensor: torch.Tensor, scale_gradient_factor: int
    ) -> torch.Tensor:
        # pyre-ignore
        ctx.scale_gradient_factor = scale_gradient_factor
        return input_tensor

    @staticmethod
    # pyre-ignore[14]: `forward` overrides method defined in `Function` inconsistently.
    def backward(
        ctx: FunctionCtx, grad_output: torch.Tensor
    ) -> Tuple[torch.Tensor, None]:
        # When gradient division is on, we scale down the gradient by world size
        # at alltoall backward for model parallelism. However weights
        # is controlled by DDP so it already has gradient division, so we scale
        # the gradient back up
        # pyre-ignore[16]: `FunctionCtx` has no attribute `scale_gradient_factor`
        grad_output.mul_(ctx.scale_gradient_factor)
        return grad_output, None


class GroupedPooledEmbeddingsLookup(
    BaseEmbeddingLookup[KeyedJaggedTensor, torch.Tensor]
):
    """
    Lookup modules for Pooled embeddings (i.e EmbeddingBags)
    """

    def __init__(
        self,
        grouped_configs: List[GroupedEmbeddingConfig],
        device: Optional[torch.device] = None,
        pg: Optional[dist.ProcessGroup] = None,
        feature_processor: Optional[BaseGroupedFeatureProcessor] = None,
        scale_weight_gradients: bool = True,
    ) -> None:
        # TODO rename to _create_embedding_kernel
        def _create_lookup(
            config: GroupedEmbeddingConfig,
            device: Optional[torch.device] = None,
        ) -> BaseEmbedding:
            if config.compute_kernel == EmbeddingComputeKernel.DENSE:
                return BatchedDenseEmbeddingBag(
                    config=config,
                    pg=pg,
                    device=device,
                )
            elif config.compute_kernel == EmbeddingComputeKernel.FUSED:
                return BatchedFusedEmbeddingBag(
                    config=config,
                    pg=pg,
                    device=device,
                )
            else:
                raise ValueError(
                    f"Compute kernel not supported {config.compute_kernel}"
                )

        super().__init__()
        self._emb_modules: nn.ModuleList = nn.ModuleList()
        for config in grouped_configs:
            self._emb_modules.append(_create_lookup(config, device))

        self._feature_splits: List[int] = []
        for config in grouped_configs:
            self._feature_splits.append(config.num_features())

        # return a dummy empty tensor when grouped_configs is empty
        self.register_buffer(
            "_dummy_embs_tensor",
            torch.empty(
                [0],
                dtype=torch.float32,
                device=device,
                requires_grad=True,
            ),
        )

        self.grouped_configs = grouped_configs
        self._feature_processor = feature_processor

        self._scale_gradient_factor: int = (
            dist.get_world_size(pg)
            if scale_weight_gradients and get_gradient_division()
            else 1
        )

    def prefetch(
        self,
        sparse_features: KeyedJaggedTensor,
        forward_stream: Optional[torch.cuda.Stream] = None,
    ) -> None:
        def _need_prefetch(config: GroupedEmbeddingConfig) -> bool:
            for table in config.embedding_tables:
                if table.compute_kernel == EmbeddingComputeKernel.FUSED_UVM_CACHING:
                    return True
            return False

        if len(self._emb_modules) > 0:
            assert sparse_features is not None
            features_by_group = sparse_features.split(
                self._feature_splits,
            )
            for emb_op, features in zip(self._emb_modules, features_by_group):
                if not _need_prefetch(emb_op.config):
                    continue
                if (
                    isinstance(emb_op.emb_module, SplitTableBatchedEmbeddingBagsCodegen)
                    and not emb_op.emb_module.prefetch_pipeline
                ):
                    logging.error(
                        "Invalid setting on SplitTableBatchedEmbeddingBagsCodegen modules. prefetch_pipeline must be set to True.\n"
                        "If you don't turn on prefetch_pipeline, cache locations might be wrong in backward and can cause wrong results.\n"
                    )
                if hasattr(emb_op.emb_module, "prefetch"):
                    emb_op.emb_module.prefetch(
                        indices=features.values(),
                        offsets=features.offsets(),
                        forward_stream=forward_stream,
                    )

    def forward(
        self,
        sparse_features: KeyedJaggedTensor,
    ) -> torch.Tensor:
        embeddings: List[torch.Tensor] = []
        if len(self._emb_modules) > 0:
            assert sparse_features is not None
            features_by_group = sparse_features.split(
                self._feature_splits,
            )
            for config, emb_op, features in zip(
                self.grouped_configs, self._emb_modules, features_by_group
            ):
                if (
                    config.has_feature_processor
                    and self._feature_processor is not None
                    and isinstance(self._feature_processor, BaseGroupedFeatureProcessor)
                ):
                    features = self._feature_processor(features)

                if config.is_weighted:
                    features._weights = CommOpGradientScaling.apply(
                        features._weights, self._scale_gradient_factor
                    )

                embeddings.append(emb_op(features))

        dummy_embedding = (
            self._dummy_embs_tensor
            if sparse_features.variable_stride_per_key()
            else fx_wrap_tensor_view2d(
                self._dummy_embs_tensor, sparse_features.stride(), 0
            )
        )
        return embeddings_cat_empty_rank_handle(
            embeddings,
            dummy_embedding,
            dim=1,
        )

    # pyre-fixme[14]: `state_dict` overrides method defined in `Module` inconsistently.
    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()

        for emb_module in self._emb_modules:
            emb_module.state_dict(destination, prefix, keep_vars)

        return destination

    # pyre-fixme[14]: `load_state_dict` overrides method defined in `Module`
    #  inconsistently.
    def load_state_dict(
        self,
        state_dict: "OrderedDict[str, Union[ShardedTensor, torch.Tensor]]",
        strict: bool = True,
    ) -> _IncompatibleKeys:
        m, u = _load_state_dict(self._emb_modules, state_dict)
        return _IncompatibleKeys(missing_keys=m, unexpected_keys=u)

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.nn.Parameter]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_parameters for"
            "GroupedPooledEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_parameters(prefix, recurse)

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_buffers for"
            "GroupedPooledEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_buffers(prefix, recurse)

    def named_parameters_by_table(
        self,
    ) -> Iterator[Tuple[str, TableBatchedEmbeddingSlice]]:
        """
        Like named_parameters(), but yields table_name and embedding_weights which are wrapped in TableBatchedEmbeddingSlice.
        For a single table with multiple shards (i.e CW) these are combined into one table/weight.
        Used in composability.
        """
        for embedding_kernel in self._emb_modules:
            for (
                table_name,
                tbe_slice,
            ) in embedding_kernel.named_parameters_by_table():
                yield (table_name, tbe_slice)

    def flush(self) -> None:
        for emb_module in self._emb_modules:
            emb_module.flush()

    def purge(self) -> None:
        for emb_module in self._emb_modules:
            emb_module.purge()


class MetaInferGroupedEmbeddingsLookup(
    BaseEmbeddingLookup[KeyedJaggedTensor, torch.Tensor], TBEToRegisterMixIn
):
    """
    meta embedding lookup module for inference since inference lookup has references
    for multiple TBE ops over all gpu workers.
    inference grouped embedding lookup module contains meta modules allocated over gpu workers.
    """

    def __init__(
        self,
        grouped_configs: List[GroupedEmbeddingConfig],
        device: Optional[torch.device] = None,
        fused_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        # TODO rename to _create_embedding_kernel
        def _create_lookup(
            config: GroupedEmbeddingConfig,
            device: Optional[torch.device] = None,
            fused_params: Optional[Dict[str, Any]] = None,
        ) -> BaseBatchedEmbedding[
            Tuple[torch.Tensor, Optional[torch.Tensor], Optional[torch.Tensor]]
        ]:
            return QuantBatchedEmbedding(
                config=config,
                device=device,
                fused_params=fused_params,
            )

        super().__init__()
        self._emb_modules: nn.ModuleList = nn.ModuleList()
        for config in grouped_configs:
            self._emb_modules.append(_create_lookup(config, device, fused_params))

        self._feature_splits: List[int] = [
            config.num_features() for config in grouped_configs
        ]

        self.grouped_configs = grouped_configs
        self.device: Optional[torch.device] = device
        self.output_dtype: torch.dtype = (
            fused_params["output_dtype"].as_dtype()
            if fused_params and "output_dtype" in fused_params
            else torch.float16
        )

    def get_tbes_to_register(
        self,
    ) -> Dict[IntNBitTableBatchedEmbeddingBagsCodegen, GroupedEmbeddingConfig]:
        return get_tbes_to_register_from_iterable(self._emb_modules)

    def forward(
        self,
        sparse_features: KeyedJaggedTensor,
    ) -> torch.Tensor:
        embeddings: List[torch.Tensor] = []
        features_by_group = (
            [sparse_features]
            if len(self._feature_splits) == 1
            else sparse_features.split(
                self._feature_splits,
            )
        )
        for i in range(len(self._emb_modules)):
            # 2d embedding by nature
            embeddings.append(self._emb_modules[i].forward(features_by_group[i]))

        return embeddings_cat_empty_rank_handle_inference(
            embeddings, device=self.device, dtype=self.output_dtype
        )

    # pyre-ignore [14]
    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()

        for emb_module in self._emb_modules:
            emb_module.state_dict(destination, prefix, keep_vars)

        return destination

    # pyre-fixme[14]: `load_state_dict` overrides method defined in `Module`
    #  inconsistently.
    def load_state_dict(
        self,
        state_dict: "OrderedDict[str, Union[ShardedTensor, torch.Tensor]]",
        strict: bool = True,
    ) -> _IncompatibleKeys:
        m, u = _load_state_dict(self._emb_modules, state_dict)
        return _IncompatibleKeys(missing_keys=m, unexpected_keys=u)

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.nn.Parameter]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_buffers for"
            "MetaInferGroupedEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_parameters(prefix, recurse)

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_buffers for"
            "MetaInferGroupedEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_buffers(prefix, recurse)

    def flush(self) -> None:
        # not implemented
        pass

    def purge(self) -> None:
        # not implemented
        pass


class MetaInferGroupedPooledEmbeddingsLookup(
    BaseEmbeddingLookup[KeyedJaggedTensor, torch.Tensor], TBEToRegisterMixIn
):
    """
    meta embedding bag lookup module for inference since inference lookup has references
    for multiple TBE ops over all gpu workers.
    inference grouped embedding bag lookup module contains meta modules allocated over gpu workers.
    """

    def __init__(
        self,
        grouped_configs: List[GroupedEmbeddingConfig],
        device: Optional[torch.device] = None,
        feature_processor: Optional[BaseGroupedFeatureProcessor] = None,
        fused_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        # TODO rename to _create_embedding_kernel
        def _create_lookup(
            config: GroupedEmbeddingConfig,
            device: Optional[torch.device] = None,
            fused_params: Optional[Dict[str, Any]] = None,
        ) -> BaseBatchedEmbeddingBag[
            Tuple[torch.Tensor, Optional[torch.Tensor], Optional[torch.Tensor]]
        ]:
            return QuantBatchedEmbeddingBag(
                config=config,
                device=device,
                fused_params=fused_params,
            )

        super().__init__()
        self._emb_modules: nn.ModuleList = nn.ModuleList()
        for config in grouped_configs:
            self._emb_modules.append(_create_lookup(config, device, fused_params))

        self._feature_splits: List[int] = [
            config.num_features() for config in grouped_configs
        ]

        self.grouped_configs = grouped_configs
        self._feature_processor = feature_processor
        self.device: Optional[torch.device] = device
        self.output_dtype: torch.dtype = (
            fused_params["output_dtype"].as_dtype()
            if fused_params and "output_dtype" in fused_params
            else torch.float16
        )

    def get_tbes_to_register(
        self,
    ) -> Dict[IntNBitTableBatchedEmbeddingBagsCodegen, GroupedEmbeddingConfig]:
        return get_tbes_to_register_from_iterable(self._emb_modules)

    def forward(
        self,
        sparse_features: KeyedJaggedTensor,
    ) -> torch.Tensor:
        if len(self.grouped_configs) == 0:
            # return a dummy empty tensor when grouped_configs is empty
            return fx_wrap_tensor_view2d(
                torch.empty(
                    [0],
                    dtype=self.output_dtype,
                    device=self.device,
                ),
                sparse_features.stride(),
                0,
            )

        embeddings: List[torch.Tensor] = []
        features_by_group = (
            [sparse_features]
            if len(self._feature_splits) == 1
            else sparse_features.split(
                self._feature_splits,
            )
        )
        # syntax for torchscript
        for i, (config, emb_op) in enumerate(
            zip(self.grouped_configs, self._emb_modules)
        ):
            features = features_by_group[i]
            if (
                config.has_feature_processor
                and self._feature_processor is not None
                and isinstance(self._feature_processor, BaseGroupedFeatureProcessor)
            ):
                features = self._feature_processor(features)
            embeddings.append(emb_op.forward(features))

        return embeddings_cat_empty_rank_handle_inference(
            embeddings,
            dim=1,
            device=self.device,
            dtype=self.output_dtype,
        )

    # pyre-ignore [14]
    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()

        for emb_module in self._emb_modules:
            emb_module.state_dict(destination, prefix, keep_vars)

        return destination

    # pyre-fixme[14]: `load_state_dict` overrides method defined in `Module`
    #  inconsistently.
    def load_state_dict(
        self,
        state_dict: "OrderedDict[str, Union[ShardedTensor, torch.Tensor]]",
        strict: bool = True,
    ) -> _IncompatibleKeys:
        m, u = _load_state_dict(self._emb_modules, state_dict)
        return _IncompatibleKeys(missing_keys=m, unexpected_keys=u)

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.nn.Parameter]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_parameters for"
            "MetaInferGroupedPooledEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_parameters(prefix, recurse)

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        assert remove_duplicate, (
            "remove_duplicate=False in named_buffers for"
            "MetaInferGroupedPooledEmbeddingsLookup is not supported"
        )
        for emb_module in self._emb_modules:
            yield from emb_module.named_buffers(prefix, recurse)

    def flush(self) -> None:
        # not implemented
        pass

    def purge(self) -> None:
        # not implemented
        pass


class InferGroupedLookupMixin(ABC):
    def forward(
        self,
        sparse_features: KJTList,
    ) -> List[torch.Tensor]:
        embeddings: List[torch.Tensor] = []
        # syntax for torchscript
        for i, embedding_lookup in enumerate(
            # pyre-fixme[16]
            self._embedding_lookups_per_rank,
        ):
            sparse_features_rank = sparse_features[i]
            embeddings.append(embedding_lookup.forward(sparse_features_rank))
        return embeddings

    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()

        # pyre-fixme[16]
        for rank_modules in self._embedding_lookups_per_rank:
            rank_modules.state_dict(destination, prefix, keep_vars)

        return destination

    def load_state_dict(
        self,
        state_dict: "OrderedDict[str, torch.Tensor]",
        strict: bool = True,
    ) -> _IncompatibleKeys:
        missing_keys = []
        unexpected_keys = []
        # pyre-fixme[16]
        for rank_modules in self._embedding_lookups_per_rank:
            incompatible_keys = rank_modules.load_state_dict(state_dict)
            missing_keys.extend(incompatible_keys.missing_keys)
            unexpected_keys.extend(incompatible_keys.unexpected_keys)
        return _IncompatibleKeys(
            missing_keys=missing_keys, unexpected_keys=unexpected_keys
        )

    def named_parameters(
        self, prefix: str = "", recurse: bool = True
    ) -> Iterator[Tuple[str, nn.Parameter]]:
        # pyre-fixme[16]
        for rank_modules in self._embedding_lookups_per_rank:
            yield from rank_modules.named_parameters(prefix, recurse)

    def named_buffers(
        self, prefix: str = "", recurse: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        # pyre-fixme[16]
        for rank_modules in self._embedding_lookups_per_rank:
            yield from rank_modules.named_buffers(prefix, recurse)


class InferGroupedPooledEmbeddingsLookup(
    InferGroupedLookupMixin,
    BaseEmbeddingLookup[KJTList, List[torch.Tensor]],
    TBEToRegisterMixIn,
):
    def __init__(
        self,
        grouped_configs_per_rank: List[List[GroupedEmbeddingConfig]],
        world_size: int,
        fused_params: Optional[Dict[str, Any]] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        self._embedding_lookups_per_rank: List[
            MetaInferGroupedPooledEmbeddingsLookup
        ] = []

        device_type = "meta" if device is not None and device.type == "meta" else "cuda"
        for rank in range(world_size):
            self._embedding_lookups_per_rank.append(
                # TODO add position weighted module support
                MetaInferGroupedPooledEmbeddingsLookup(
                    grouped_configs=grouped_configs_per_rank[rank],
                    # syntax for torchscript
                    device=torch.device(type=device_type, index=rank),
                    fused_params=fused_params,
                )
            )

    def get_tbes_to_register(
        self,
    ) -> Dict[IntNBitTableBatchedEmbeddingBagsCodegen, GroupedEmbeddingConfig]:
        return get_tbes_to_register_from_iterable(self._embedding_lookups_per_rank)


class InferGroupedEmbeddingsLookup(
    InferGroupedLookupMixin,
    BaseEmbeddingLookup[KJTList, List[torch.Tensor]],
    TBEToRegisterMixIn,
):
    def __init__(
        self,
        grouped_configs_per_rank: List[List[GroupedEmbeddingConfig]],
        world_size: int,
        fused_params: Optional[Dict[str, Any]] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        self._embedding_lookups_per_rank: List[MetaInferGroupedEmbeddingsLookup] = []

        device_type = "meta" if device is not None and device.type == "meta" else "cuda"
        for rank in range(world_size):
            self._embedding_lookups_per_rank.append(
                MetaInferGroupedEmbeddingsLookup(
                    grouped_configs=grouped_configs_per_rank[rank],
                    # syntax for torchscript
                    device=torch.device(type=device_type, index=rank),
                    fused_params=fused_params,
                )
            )

    def get_tbes_to_register(
        self,
    ) -> Dict[IntNBitTableBatchedEmbeddingBagsCodegen, GroupedEmbeddingConfig]:
        return get_tbes_to_register_from_iterable(self._embedding_lookups_per_rank)
