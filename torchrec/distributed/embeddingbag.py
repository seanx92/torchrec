#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import copy
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import (
    Any,
    cast,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

import torch
from torch import nn, Tensor
from torch.nn.modules.module import _IncompatibleKeys
from torch.nn.parallel import DistributedDataParallel
from torchrec.distributed.embedding_sharding import (
    EmbeddingSharding,
    EmbeddingShardingContext,
    EmbeddingShardingInfo,
    KJTListSplitsAwaitable,
    Multistreamable,
)
from torchrec.distributed.embedding_types import (
    BaseEmbeddingSharder,
    EmbeddingComputeKernel,
    KJTList,
    ShardedEmbeddingModule,
)
from torchrec.distributed.sharding.cw_sharding import CwPooledEmbeddingSharding
from torchrec.distributed.sharding.dp_sharding import DpPooledEmbeddingSharding
from torchrec.distributed.sharding.rw_sharding import RwPooledEmbeddingSharding
from torchrec.distributed.sharding.tw_sharding import TwPooledEmbeddingSharding
from torchrec.distributed.sharding.twcw_sharding import TwCwPooledEmbeddingSharding
from torchrec.distributed.sharding.twrw_sharding import TwRwPooledEmbeddingSharding
from torchrec.distributed.types import (
    Awaitable,
    EmbeddingModuleShardingPlan,
    EnumerableShardingSpec,
    LazyAwaitable,
    NullShardedModuleContext,
    ParameterSharding,
    QuantizedCommCodecs,
    ShardedTensor,
    ShardingEnv,
    ShardingType,
    ShardMetadata,
)
from torchrec.distributed.utils import (
    add_params_from_parameter_sharding,
    append_prefix,
    convert_to_fbgemm_types,
    merge_fused_params,
    optimizer_type_to_emb_opt_type,
    PermutePooledEmbeddings,
)
from torchrec.modules.embedding_configs import (
    EmbeddingBagConfig,
    EmbeddingTableConfig,
    PoolingType,
)
from torchrec.modules.embedding_modules import (
    EmbeddingBagCollection,
    EmbeddingBagCollectionInterface,
)
from torchrec.optim.fused import EmptyFusedOptimizer, FusedOptimizerModule
from torchrec.optim.keyed import CombinedOptimizer, KeyedOptimizer
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor, KeyedTensor

try:
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops")
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops_cpu")
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu/codegen:index_select_ops")
except OSError:
    pass


# OSS
try:
    pass
except ImportError:
    pass


def _pin_and_move(tensor: torch.Tensor, device: torch.device) -> torch.Tensor:
    return (
        tensor
        if device.type == "cpu"
        else tensor.pin_memory().to(device=device, non_blocking=True)
    )


def replace_placement_with_meta_device(
    sharding_infos: List[EmbeddingShardingInfo],
) -> None:
    """Placement device and tensor device could be unmatched in some
    scenarios, e.g. passing meta device to DMP and passing cuda
    to EmbeddingShardingPlanner. We need to make device consistent
    after getting sharding planner.
    """
    for info in sharding_infos:
        sharding_spec = info.param_sharding.sharding_spec
        if sharding_spec is None:
            continue
        if isinstance(sharding_spec, EnumerableShardingSpec):
            for shard_metadata in sharding_spec.shards:
                placement = shard_metadata.placement
                if isinstance(placement, str):
                    placement = torch.distributed._remote_device(placement)
                assert isinstance(placement, torch.distributed._remote_device)
                placement._device = torch.device("meta")
                shard_metadata.placement = placement
        else:
            # We only support EnumerableShardingSpec at present.
            raise RuntimeError(
                f"Unsupported ShardingSpec {type(sharding_spec)} with meta device"
            )


def create_embedding_bag_sharding(
    sharding_type: str,
    sharding_infos: List[EmbeddingShardingInfo],
    env: ShardingEnv,
    device: Optional[torch.device] = None,
    permute_embeddings: bool = False,
    qcomm_codecs_registry: Optional[Dict[str, QuantizedCommCodecs]] = None,
) -> EmbeddingSharding[
    EmbeddingShardingContext, KeyedJaggedTensor, torch.Tensor, torch.Tensor
]:
    if device is not None and device.type == "meta":
        replace_placement_with_meta_device(sharding_infos)
    if sharding_type == ShardingType.TABLE_WISE.value:
        return TwPooledEmbeddingSharding(
            sharding_infos,
            env,
            device,
            qcomm_codecs_registry=qcomm_codecs_registry,
        )
    elif sharding_type == ShardingType.ROW_WISE.value:
        return RwPooledEmbeddingSharding(
            sharding_infos,
            env,
            device,
            qcomm_codecs_registry=qcomm_codecs_registry,
        )
    elif sharding_type == ShardingType.DATA_PARALLEL.value:
        return DpPooledEmbeddingSharding(sharding_infos, env, device)
    elif sharding_type == ShardingType.TABLE_ROW_WISE.value:
        return TwRwPooledEmbeddingSharding(
            sharding_infos,
            env,
            device,
            qcomm_codecs_registry=qcomm_codecs_registry,
        )
    elif sharding_type == ShardingType.COLUMN_WISE.value:
        return CwPooledEmbeddingSharding(
            sharding_infos,
            env,
            device,
            permute_embeddings=permute_embeddings,
            qcomm_codecs_registry=qcomm_codecs_registry,
        )
    elif sharding_type == ShardingType.TABLE_COLUMN_WISE.value:
        return TwCwPooledEmbeddingSharding(
            sharding_infos,
            env,
            device,
            permute_embeddings=permute_embeddings,
            qcomm_codecs_registry=qcomm_codecs_registry,
        )
    else:
        raise ValueError(f"Sharding type not supported {sharding_type}")


def create_sharding_infos_by_sharding(
    module: EmbeddingBagCollectionInterface,
    table_name_to_parameter_sharding: Dict[str, ParameterSharding],
    prefix: str,
    fused_params: Optional[Dict[str, Any]],
    suffix: Optional[str] = "weight",
) -> Dict[str, List[EmbeddingShardingInfo]]:

    if fused_params is None:
        fused_params = {}

    shared_feature: Dict[str, bool] = {}
    for embedding_config in module.embedding_bag_configs():
        if not embedding_config.feature_names:
            embedding_config.feature_names = [embedding_config.name]
        for feature_name in embedding_config.feature_names:
            if feature_name not in shared_feature:
                shared_feature[feature_name] = False
            else:
                shared_feature[feature_name] = True

    sharding_type_to_sharding_infos: Dict[str, List[EmbeddingShardingInfo]] = {}

    # state_dict returns parameter.Tensor, which loses parameter level attributes
    parameter_by_name = dict(module.named_parameters())
    # QuantEBC registers weights as buffers (since they are INT8), and so we need to grab it there
    state_dict = module.state_dict()

    for config in module.embedding_bag_configs():
        table_name = config.name
        assert (
            table_name in table_name_to_parameter_sharding
        ), f"{table_name} not in table_name_to_parameter_sharding"
        parameter_sharding = table_name_to_parameter_sharding[table_name]
        if parameter_sharding.compute_kernel not in [
            kernel.value for kernel in EmbeddingComputeKernel
        ]:
            raise ValueError(
                f"Compute kernel not supported {parameter_sharding.compute_kernel}"
            )
        embedding_names: List[str] = []
        for feature_name in config.feature_names:
            if shared_feature[feature_name]:
                embedding_names.append(feature_name + "@" + config.name)
            else:
                embedding_names.append(feature_name)

        param_name = prefix + table_name
        if suffix is not None:
            param_name = f"{param_name}.{suffix}"

        assert param_name in parameter_by_name or param_name in state_dict
        param = parameter_by_name.get(param_name, state_dict[param_name])

        if parameter_sharding.sharding_type not in sharding_type_to_sharding_infos:
            sharding_type_to_sharding_infos[parameter_sharding.sharding_type] = []

        optimizer_params = getattr(param, "_optimizer_kwargs", [{}])
        optimizer_classes = getattr(param, "_optimizer_classes", [None])

        assert (
            len(optimizer_classes) == 1 and len(optimizer_params) == 1
        ), f"Only support 1 optimizer, given {len(optimizer_classes)} optimizer classes \
        and {len(optimizer_params)} optimizer kwargs."

        optimizer_class = optimizer_classes[0]
        optimizer_params = optimizer_params[0]
        if optimizer_class:
            optimizer_params["optimizer"] = optimizer_type_to_emb_opt_type(
                optimizer_class
            )

        per_table_fused_params = merge_fused_params(fused_params, optimizer_params)
        per_table_fused_params = add_params_from_parameter_sharding(
            per_table_fused_params, parameter_sharding
        )
        per_table_fused_params = convert_to_fbgemm_types(per_table_fused_params)

        sharding_type_to_sharding_infos[parameter_sharding.sharding_type].append(
            EmbeddingShardingInfo(
                embedding_config=EmbeddingTableConfig(
                    num_embeddings=config.num_embeddings,
                    embedding_dim=config.embedding_dim,
                    name=config.name,
                    data_type=config.data_type,
                    feature_names=copy.deepcopy(config.feature_names),
                    pooling=config.pooling,
                    is_weighted=module.is_weighted(),
                    has_feature_processor=False,
                    embedding_names=embedding_names,
                    weight_init_max=config.weight_init_max,
                    weight_init_min=config.weight_init_min,
                    pruning_indices_remapping=config.pruning_indices_remapping,
                ),
                param_sharding=parameter_sharding,
                param=param,
                fused_params=per_table_fused_params,
            )
        )
    return sharding_type_to_sharding_infos


def construct_output_kt(
    embeddings: List[torch.Tensor],
    embedding_names: List[str],
    embedding_dims: List[int],
) -> KeyedTensor:
    cat_embeddings: torch.Tensor
    if len(embeddings) == 1:
        cat_embeddings = embeddings[0]
    else:
        cat_embeddings = torch.cat(embeddings, dim=1)
    return KeyedTensor(
        keys=embedding_names,
        length_per_key=embedding_dims,
        values=cat_embeddings,
        key_dim=1,
    )


class VariableBatchEmbeddingBagCollectionAwaitable(LazyAwaitable[KeyedTensor]):
    def __init__(
        self,
        awaitables: List[Awaitable[torch.Tensor]],
        inverse_indices: Tuple[List[str], torch.Tensor],
        inverse_indices_permute_indices: torch.Tensor,
        batch_size_per_feature_pre_a2a: List[int],
        uncombined_embedding_dims: List[int],
        embedding_names: List[str],
        embedding_dims: List[int],
        permute_op: PermutePooledEmbeddings,
    ) -> None:
        super().__init__()
        self._awaitables = awaitables
        self._inverse_indices = inverse_indices
        self._inverse_indices_permute_indices = inverse_indices_permute_indices
        self._batch_size_per_feature_pre_a2a = batch_size_per_feature_pre_a2a
        self._uncombined_embedding_dims = uncombined_embedding_dims
        self._embedding_names = embedding_names
        self._embedding_dims = embedding_dims
        self._permute_op = permute_op

    def _wait_impl(self) -> KeyedTensor:
        embeddings = [w.wait() for w in self._awaitables]
        batch_size = self._inverse_indices[1].numel() // len(self._inverse_indices[0])
        indices = torch.index_select(
            self._inverse_indices[1], 0, self._inverse_indices_permute_indices
        )
        reindex_output = torch.ops.fbgemm.batch_index_select_dim0(
            inputs=embeddings[0] if len(embeddings) == 1 else torch.cat(embeddings),
            indices=indices.view(-1),
            input_num_indices=[batch_size] * len(self._uncombined_embedding_dims),
            input_rows=self._batch_size_per_feature_pre_a2a,
            input_columns=self._uncombined_embedding_dims,
            permute_output_dim_0_1=True,
        ).view(batch_size, -1)
        return construct_output_kt(
            embeddings=[self._permute_op(reindex_output)],
            embedding_names=self._embedding_names,
            embedding_dims=self._embedding_dims,
        )


class EmbeddingBagCollectionAwaitable(LazyAwaitable[KeyedTensor]):
    def __init__(
        self,
        awaitables: List[Awaitable[torch.Tensor]],
        embedding_dims: List[int],
        embedding_names: List[str],
    ) -> None:
        super().__init__()
        self._awaitables = awaitables
        self._embedding_dims = embedding_dims
        self._embedding_names = embedding_names

    def _wait_impl(self) -> KeyedTensor:
        return construct_output_kt(
            embeddings=[w.wait() for w in self._awaitables],
            embedding_names=self._embedding_names,
            embedding_dims=self._embedding_dims,
        )


@dataclass
class EmbeddingBagCollectionContext(Multistreamable):
    sharding_contexts: List[Optional[EmbeddingShardingContext]] = field(
        default_factory=list
    )
    inverse_indices: Optional[Tuple[List[str], torch.Tensor]] = None
    variable_batch_per_feature: bool = False

    def record_stream(self, stream: torch.cuda.streams.Stream) -> None:
        for ctx in self.sharding_contexts:
            if ctx:
                ctx.record_stream(stream)
        if self.inverse_indices is not None:
            self.inverse_indices[1].record_stream(stream)


class ShardedEmbeddingBagCollection(
    ShardedEmbeddingModule[
        KJTList,
        List[torch.Tensor],
        KeyedTensor,
        EmbeddingBagCollectionContext,
    ],
    # TODO remove after compute_kernel X sharding decoupling
    FusedOptimizerModule,
):
    """
    Sharded implementation of EmbeddingBagCollection.
    This is part of the public API to allow for manual data dist pipelining.
    """

    def __init__(
        self,
        module: EmbeddingBagCollectionInterface,
        table_name_to_parameter_sharding: Dict[str, ParameterSharding],
        env: ShardingEnv,
        fused_params: Optional[Dict[str, Any]] = None,
        device: Optional[torch.device] = None,
        qcomm_codecs_registry: Optional[Dict[str, QuantizedCommCodecs]] = None,
    ) -> None:
        super().__init__(qcomm_codecs_registry=qcomm_codecs_registry)
        self._embedding_bag_configs: List[EmbeddingBagConfig] = (
            module.embedding_bag_configs()
        )
        self._table_names: List[str] = [
            config.name for config in self._embedding_bag_configs
        ]

        self._table_name_to_config: Dict[str, EmbeddingBagConfig] = {
            config.name: config for config in self._embedding_bag_configs
        }

        self.module_sharding_plan: EmbeddingModuleShardingPlan = cast(
            EmbeddingModuleShardingPlan,
            {
                table_name: parameter_sharding
                for table_name, parameter_sharding in table_name_to_parameter_sharding.items()
                if table_name in self._table_names
            },
        )
        self._env = env

        sharding_type_to_sharding_infos = create_sharding_infos_by_sharding(
            module,
            table_name_to_parameter_sharding,
            "embedding_bags.",
            fused_params,
        )
        self._sharding_type_to_sharding: Dict[
            str,
            EmbeddingSharding[
                EmbeddingShardingContext,
                KeyedJaggedTensor,
                torch.Tensor,
                torch.Tensor,
            ],
        ] = {
            sharding_type: create_embedding_bag_sharding(
                sharding_type,
                embedding_configs,
                env,
                device,
                permute_embeddings=True,
                qcomm_codecs_registry=self.qcomm_codecs_registry,
            )
            for sharding_type, embedding_configs in sharding_type_to_sharding_infos.items()
        }

        self._is_weighted: bool = module.is_weighted()
        self._device = device
        self._input_dists: List[nn.Module] = []
        self._lookups: List[nn.Module] = []
        self._create_lookups()
        self._output_dists: List[nn.Module] = []
        self._embedding_names: List[str] = []
        self._embedding_dims: List[int] = []
        self._feature_splits: List[int] = []
        self._features_order: List[int] = []
        self._uncombined_embedding_names: List[str] = []
        self._uncombined_embedding_dims: List[int] = []
        self._inverse_indices_permute_indices: Optional[torch.Tensor] = None
        # to support the FP16 hook
        self._create_output_dist()

        # forward pass flow control
        self._has_uninitialized_input_dist: bool = True
        self._has_features_permute: bool = True
        # Get all fused optimizers and combine them.
        optims = []
        for lookup in self._lookups:
            for _, tbe_module in lookup.named_modules():
                if isinstance(tbe_module, FusedOptimizerModule):
                    # modify param keys to match EmbeddingBagCollection
                    params: Mapping[str, Union[torch.Tensor, ShardedTensor]] = {}
                    for param_key, weight in tbe_module.fused_optimizer.params.items():
                        # pyre-fixme[16]: `Mapping` has no attribute `__setitem__`
                        params["embedding_bags." + param_key] = weight
                    tbe_module.fused_optimizer.params = params
                    optims.append(("", tbe_module.fused_optimizer))
        self._optim: CombinedOptimizer = CombinedOptimizer(optims)

        for index, (sharding, lookup) in enumerate(
            zip(
                self._sharding_type_to_sharding.values(),
                self._lookups,
            )
        ):
            # TODO: can move this into DpPooledEmbeddingSharding once all modules are composable
            if isinstance(sharding, DpPooledEmbeddingSharding):
                self._lookups[index] = DistributedDataParallel(
                    module=lookup,
                    device_ids=(
                        [device]
                        if self._device and (self._device.type in {"cuda", "mtia"})
                        else None
                    ),
                    process_group=env.process_group,
                    gradient_as_bucket_view=True,
                    broadcast_buffers=True,
                    static_graph=True,
                )
        self._initialize_torch_state()

        # TODO[zainhuda]: support module device coming from CPU
        if module.device not in ["meta", "cpu"] and module.device.type not in [
            "meta",
            "cpu",
        ]:
            self.load_state_dict(module.state_dict(), strict=False)

    @staticmethod
    def _pre_state_dict_hook(
        self: "ShardedEmbeddingBagCollection",
        prefix: str = "",
        keep_vars: bool = False,
    ) -> None:
        for lookup in self._lookups:
            while isinstance(lookup, DistributedDataParallel):
                lookup = lookup.module
            lookup.flush()

    @staticmethod
    def _pre_load_state_dict_hook(
        self: "ShardedEmbeddingBagCollection",
        state_dict: Dict[str, Any],
        prefix: str,
        *args: Any,
    ) -> None:
        """
        Modify the destination state_dict for model parallel
        to transform from ShardedTensors into tensors
        """
        for (
            table_name,
            model_shards,
        ) in self._model_parallel_name_to_local_shards.items():
            key = f"{prefix}embedding_bags.{table_name}.weight"

            # If state_dict[key] is already a ShardedTensor, use its local shards
            if isinstance(state_dict[key], ShardedTensor):
                local_shards = state_dict[key].local_shards()
                if len(local_shards) == 0:
                    state_dict[key] = torch.empty(0)
                else:
                    dim = state_dict[key].metadata().shards_metadata[0].shard_sizes[1]
                    # CW multiple shards are merged
                    if len(local_shards) > 1:
                        state_dict[key] = torch.cat(
                            [s.tensor.view(-1) for s in local_shards], dim=0
                        ).view(-1, dim)
                    else:
                        state_dict[key] = local_shards[0].tensor.view(-1, dim)
            elif isinstance(state_dict[key], torch.Tensor):
                local_shards = []
                for shard in model_shards:
                    # Extract shard size and offsets for splicing
                    shard_sizes = shard.metadata.shard_sizes
                    shard_offsets = shard.metadata.shard_offsets

                    # Prepare tensor by splicing and placing on appropriate device
                    spliced_tensor = state_dict[key][
                        shard_offsets[0] : shard_offsets[0] + shard_sizes[0],
                        shard_offsets[1] : shard_offsets[1] + shard_sizes[1],
                    ]

                    # Append spliced tensor into local shards
                    local_shards.append(spliced_tensor)
                state_dict[key] = (
                    torch.empty(0)
                    if not local_shards
                    else torch.cat(local_shards, dim=0)
                )
            else:
                raise RuntimeError(
                    f"Unexpected state_dict key type {type(state_dict[key])} found for {key}"
                )

        for lookup in self._lookups:
            while isinstance(lookup, DistributedDataParallel):
                lookup = lookup.module
            lookup.purge()

    def _initialize_torch_state(self) -> None:  # noqa
        """
        This provides consistency between this class and the EmbeddingBagCollection's
        nn.Module API calls (state_dict, named_modules, etc)
        """
        self.embedding_bags: nn.ModuleDict = nn.ModuleDict()
        for table_name in self._table_names:
            self.embedding_bags[table_name] = nn.Module()
        self._model_parallel_name_to_local_shards = OrderedDict()
        self._model_parallel_name_to_sharded_tensor = OrderedDict()
        model_parallel_name_to_compute_kernel: Dict[str, str] = {}
        for (
            table_name,
            parameter_sharding,
        ) in self.module_sharding_plan.items():
            if parameter_sharding.sharding_type == ShardingType.DATA_PARALLEL.value:
                continue
            self._model_parallel_name_to_local_shards[table_name] = []
            model_parallel_name_to_compute_kernel[table_name] = (
                parameter_sharding.compute_kernel
            )

        self._name_to_table_size = {}
        for table in self._embedding_bag_configs:
            self._name_to_table_size[table.name] = (
                table.num_embeddings,
                table.embedding_dim,
            )

        for sharding_type, lookup in zip(
            self._sharding_type_to_sharding.keys(), self._lookups
        ):
            if sharding_type == ShardingType.DATA_PARALLEL.value:
                # unwrap DDP
                lookup = lookup.module
            else:
                # save local_shards for transforming MP params to shardedTensor
                for key, v in lookup.state_dict().items():
                    table_name = key[: -len(".weight")]
                    self._model_parallel_name_to_local_shards[table_name].extend(
                        v.local_shards()
                    )
            for (
                table_name,
                tbe_slice,
            ) in lookup.named_parameters_by_table():
                self.embedding_bags[table_name].register_parameter("weight", tbe_slice)
        for (
            table_name,
            local_shards,
        ) in self._model_parallel_name_to_local_shards.items():
            # for shards that don't exist on this rank, register with empty tensor
            if not hasattr(self.embedding_bags[table_name], "weight"):
                self.embedding_bags[table_name].register_parameter(
                    "weight", nn.Parameter(torch.empty(0))
                )
                if (
                    model_parallel_name_to_compute_kernel[table_name]
                    != EmbeddingComputeKernel.DENSE.value
                ):
                    self.embedding_bags[table_name].weight._in_backward_optimizers = [
                        EmptyFusedOptimizer()
                    ]
            # created ShardedTensors once in init, use in post_state_dict_hook
            self._model_parallel_name_to_sharded_tensor[table_name] = (
                ShardedTensor._init_from_local_shards(
                    local_shards,
                    self._name_to_table_size[table_name],
                    process_group=self._env.process_group,
                )
            )

        def post_state_dict_hook(
            module: ShardedEmbeddingBagCollection,
            destination: Dict[str, torch.Tensor],
            prefix: str,
            _local_metadata: Dict[str, Any],
        ) -> None:
            # Adjust dense MP
            for (
                table_name,
                sharded_t,
            ) in module._model_parallel_name_to_sharded_tensor.items():
                destination_key = f"{prefix}embedding_bags.{table_name}.weight"
                destination[destination_key] = sharded_t

        self.register_state_dict_pre_hook(self._pre_state_dict_hook)
        self._register_state_dict_hook(post_state_dict_hook)
        self._register_load_state_dict_pre_hook(
            self._pre_load_state_dict_hook, with_module=True
        )
        self.reset_parameters()

    def reset_parameters(self) -> None:
        if self._device and self._device.type == "meta":
            return

        # Initialize embedding bags weights with init_fn
        for table_config in self._embedding_bag_configs:
            assert table_config.init_fn is not None
            param = self.embedding_bags[f"{table_config.name}"].weight
            # pyre-ignore
            table_config.init_fn(param)

    def _create_input_dist(
        self,
        input_feature_names: List[str],
    ) -> None:
        feature_names: List[str] = []
        for sharding in self._sharding_type_to_sharding.values():
            self._input_dists.append(sharding.create_input_dist())
            feature_names.extend(sharding.feature_names())
            self._feature_splits.append(len(sharding.feature_names()))

        if feature_names == input_feature_names:
            self._has_features_permute = False
        else:
            for f in feature_names:
                self._features_order.append(input_feature_names.index(f))
            self.register_buffer(
                "_features_order_tensor",
                torch.tensor(
                    self._features_order, device=self._device, dtype=torch.int32
                ),
                persistent=False,
            )

    def _create_lookups(
        self,
    ) -> None:
        for sharding in self._sharding_type_to_sharding.values():
            self._lookups.append(sharding.create_lookup())

    def _create_output_dist(self) -> None:
        embedding_shard_metadata: List[Optional[ShardMetadata]] = []
        for sharding in self._sharding_type_to_sharding.values():
            self._output_dists.append(sharding.create_output_dist(device=self._device))
            self._embedding_names.extend(sharding.embedding_names())
            self._embedding_dims.extend(sharding.embedding_dims())
            self._uncombined_embedding_names.extend(
                sharding.uncombined_embedding_names()
            )
            self._uncombined_embedding_dims.extend(sharding.uncombined_embedding_dims())
            embedding_shard_metadata.extend(sharding.embedding_shard_metadata())
        embedding_shard_offsets: List[int] = [
            meta.shard_offsets[1] if meta is not None else 0
            for meta in embedding_shard_metadata
        ]
        embedding_name_order: Dict[str, int] = {}
        for i, name in enumerate(self._uncombined_embedding_names):
            embedding_name_order.setdefault(name, i)

        def sort_key(input: Tuple[int, str]) -> Tuple[int, int]:
            index, name = input
            return (embedding_name_order[name], embedding_shard_offsets[index])

        permute_indices = [
            i
            for i, _ in sorted(
                enumerate(self._uncombined_embedding_names), key=sort_key
            )
        ]
        self._permute_op: PermutePooledEmbeddings = PermutePooledEmbeddings(
            self._uncombined_embedding_dims, permute_indices, self._device
        )

    def _create_inverse_indices_permute_indices(
        self, inverse_indices: Optional[Tuple[List[str], torch.Tensor]]
    ) -> None:
        assert (
            inverse_indices is not None
        ), "inverse indices must be provided from KJT if using variable batch size per feature."
        index_per_name = {name: i for i, name in enumerate(inverse_indices[0])}
        permute_indices = [
            index_per_name[name.split("@")[0]]
            for name in self._uncombined_embedding_names
        ]
        self._inverse_indices_permute_indices = _pin_and_move(
            torch.tensor(permute_indices),
            inverse_indices[1].device,
        )

    # pyre-ignore [14]
    def input_dist(
        self, ctx: EmbeddingBagCollectionContext, features: KeyedJaggedTensor
    ) -> Awaitable[Awaitable[KJTList]]:
        if self._has_uninitialized_input_dist:
            self._create_input_dist(features.keys())
            self._has_uninitialized_input_dist = False
        ctx.variable_batch_per_feature = features.variable_stride_per_key()
        ctx.inverse_indices = features.inverse_indices_or_none()
        if (
            ctx.variable_batch_per_feature
            and self._inverse_indices_permute_indices is None
        ):
            self._create_inverse_indices_permute_indices(ctx.inverse_indices)
        with torch.no_grad():
            if self._has_features_permute:
                features = features.permute(
                    self._features_order,
                    self._features_order_tensor,
                )
            features_by_shards = features.split(
                self._feature_splits,
            )
            awaitables = []
            for input_dist, features_by_shard in zip(
                self._input_dists, features_by_shards
            ):
                awaitables.append(input_dist(features_by_shard))
                ctx.sharding_contexts.append(
                    EmbeddingShardingContext(
                        batch_size_per_feature_pre_a2a=features_by_shard.stride_per_key(),
                        variable_batch_per_feature=features_by_shard.variable_stride_per_key(),
                    )
                )
            return KJTListSplitsAwaitable(awaitables, ctx)

    def compute(
        self,
        ctx: EmbeddingBagCollectionContext,
        dist_input: KJTList,
    ) -> List[torch.Tensor]:
        return [lookup(features) for lookup, features in zip(self._lookups, dist_input)]

    def output_dist(
        self,
        ctx: EmbeddingBagCollectionContext,
        output: List[torch.Tensor],
    ) -> LazyAwaitable[KeyedTensor]:
        batch_size_per_feature_pre_a2a = []
        awaitables = []
        for dist, sharding_context, embeddings in zip(
            self._output_dists,
            ctx.sharding_contexts,
            output,
        ):
            awaitables.append(dist(embeddings, sharding_context))
            if sharding_context:
                batch_size_per_feature_pre_a2a.extend(
                    sharding_context.batch_size_per_feature_pre_a2a
                )

        if ctx.variable_batch_per_feature:
            assert (
                ctx.inverse_indices is not None
            ), "inverse indices must be provided from KJT if using variable batch size per feature."
            return VariableBatchEmbeddingBagCollectionAwaitable(
                awaitables=awaitables,
                inverse_indices=ctx.inverse_indices,
                inverse_indices_permute_indices=self._inverse_indices_permute_indices,
                batch_size_per_feature_pre_a2a=batch_size_per_feature_pre_a2a,
                uncombined_embedding_dims=self._uncombined_embedding_dims,
                embedding_names=self._embedding_names,
                embedding_dims=self._embedding_dims,
                permute_op=self._permute_op,
            )
        else:
            return EmbeddingBagCollectionAwaitable(
                awaitables=awaitables,
                embedding_dims=self._embedding_dims,
                embedding_names=self._embedding_names,
            )

    def compute_and_output_dist(
        self, ctx: EmbeddingBagCollectionContext, input: KJTList
    ) -> LazyAwaitable[KeyedTensor]:
        batch_size_per_feature_pre_a2a = []
        awaitables = []

        # No usage of zip for dynamo
        for i in range(len(self._lookups)):
            lookup = self._lookups[i]
            dist = self._output_dists[i]
            sharding_context = ctx.sharding_contexts[i]
            features = input[i]
            awaitables.append(dist(lookup(features), sharding_context))
            if sharding_context:
                batch_size_per_feature_pre_a2a.extend(
                    sharding_context.batch_size_per_feature_pre_a2a
                )

        if ctx.variable_batch_per_feature:
            assert (
                ctx.inverse_indices is not None
            ), "inverse indices must be provided from KJT if using variable batch size per feature."
            return VariableBatchEmbeddingBagCollectionAwaitable(
                awaitables=awaitables,
                inverse_indices=ctx.inverse_indices,
                inverse_indices_permute_indices=self._inverse_indices_permute_indices,
                batch_size_per_feature_pre_a2a=batch_size_per_feature_pre_a2a,
                uncombined_embedding_dims=self._uncombined_embedding_dims,
                embedding_names=self._embedding_names,
                embedding_dims=self._embedding_dims,
                permute_op=self._permute_op,
            )
        else:
            return EmbeddingBagCollectionAwaitable(
                awaitables=awaitables,
                embedding_dims=self._embedding_dims,
                embedding_names=self._embedding_names,
            )

    @property
    def fused_optimizer(self) -> KeyedOptimizer:
        return self._optim

    def create_context(self) -> EmbeddingBagCollectionContext:
        return EmbeddingBagCollectionContext()


class EmbeddingBagCollectionSharder(BaseEmbeddingSharder[EmbeddingBagCollection]):
    """
    This implementation uses non-fused `EmbeddingBagCollection`
    """

    def shard(
        self,
        module: EmbeddingBagCollection,
        params: Dict[str, ParameterSharding],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
    ) -> ShardedEmbeddingBagCollection:
        return ShardedEmbeddingBagCollection(
            module=module,
            table_name_to_parameter_sharding=params,
            env=env,
            fused_params=self.fused_params,
            device=device,
            qcomm_codecs_registry=self.qcomm_codecs_registry,
        )

    def shardable_parameters(
        self, module: EmbeddingBagCollection
    ) -> Dict[str, nn.Parameter]:
        return {
            name.split(".")[0]: param
            for name, param in module.embedding_bags.named_parameters()
        }

    @property
    def module_type(self) -> Type[EmbeddingBagCollection]:
        return EmbeddingBagCollection


class EmbeddingAwaitable(LazyAwaitable[torch.Tensor]):
    def __init__(
        self,
        awaitable: Awaitable[torch.Tensor],
    ) -> None:
        super().__init__()
        self._awaitable = awaitable

    def _wait_impl(self) -> torch.Tensor:
        embedding = self._awaitable.wait()
        return embedding


class ShardedEmbeddingBag(
    ShardedEmbeddingModule[
        KeyedJaggedTensor, torch.Tensor, torch.Tensor, NullShardedModuleContext
    ],
    FusedOptimizerModule,
):
    """
    Sharded implementation of `nn.EmbeddingBag`.
    This is part of the public API to allow for manual data dist pipelining.
    """

    def __init__(
        self,
        module: nn.EmbeddingBag,
        table_name_to_parameter_sharding: Dict[str, ParameterSharding],
        env: ShardingEnv,
        fused_params: Optional[Dict[str, Any]] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()

        assert (
            len(table_name_to_parameter_sharding) == 1
        ), "expect 1 table, but got len(table_name_to_parameter_sharding)"
        assert module.mode == "sum", "ShardedEmbeddingBag only supports sum pooling"

        self._dummy_embedding_table_name = "dummy_embedding_table_name"
        self._dummy_feature_name = "dummy_feature_name"
        self.parameter_sharding: ParameterSharding = next(
            iter(table_name_to_parameter_sharding.values())
        )
        embedding_table_config = EmbeddingTableConfig(
            num_embeddings=module.num_embeddings,
            embedding_dim=module.embedding_dim,
            name=self._dummy_embedding_table_name,
            feature_names=[self._dummy_feature_name],
            pooling=PoolingType.SUM,
            # We set is_weighted to True for now,
            # if per_sample_weights is None in forward(),
            # we could assign a all-one vector to per_sample_weights
            is_weighted=True,
            embedding_names=[self._dummy_feature_name],
        )

        if self.parameter_sharding.sharding_type == ShardingType.TABLE_WISE.value:
            # TODO: enable it with correct semantics, see T104397332
            raise RuntimeError(
                "table-wise sharding on a single EmbeddingBag is not supported yet"
            )

        self._embedding_sharding: EmbeddingSharding[
            EmbeddingShardingContext, KeyedJaggedTensor, torch.Tensor, torch.Tensor
        ] = create_embedding_bag_sharding(
            sharding_type=self.parameter_sharding.sharding_type,
            sharding_infos=[
                EmbeddingShardingInfo(
                    embedding_config=embedding_table_config,
                    param_sharding=self.parameter_sharding,
                    param=next(iter(module.parameters())),
                    fused_params=fused_params,
                ),
            ],
            env=env,
            device=device,
            permute_embeddings=True,
        )
        self._input_dist: nn.Module = self._embedding_sharding.create_input_dist()
        self._lookup: nn.Module = self._embedding_sharding.create_lookup()
        self._output_dist: nn.Module = self._embedding_sharding.create_output_dist()

        # Get all fused optimizers and combine them.
        optims = []
        for _, module in self._lookup.named_modules():
            if isinstance(module, FusedOptimizerModule):
                # modify param keys to match EmbeddingBag
                params: Mapping[str, Union[torch.Tensor, ShardedTensor]] = {}
                for param_key, weight in module.fused_optimizer.params.items():
                    # pyre-fixme[16]: `Mapping` has no attribute `__setitem__`.
                    params[param_key.split(".")[-1]] = weight
                module.fused_optimizer.params = params
                optims.append(("", module.fused_optimizer))
        self._optim: CombinedOptimizer = CombinedOptimizer(optims)

    # pyre-ignore [14]
    def input_dist(
        self,
        ctx: NullShardedModuleContext,
        input: Tensor,
        offsets: Optional[Tensor] = None,
        per_sample_weights: Optional[Tensor] = None,
    ) -> Awaitable[Awaitable[KeyedJaggedTensor]]:
        if per_sample_weights is None:
            per_sample_weights = torch.ones_like(input, dtype=torch.float)
        features = KeyedJaggedTensor(
            keys=[self._dummy_feature_name],
            values=input,
            offsets=offsets,
            weights=per_sample_weights,
        )
        return self._input_dist(features)

    def compute(
        self, ctx: NullShardedModuleContext, dist_input: KeyedJaggedTensor
    ) -> torch.Tensor:
        return self._lookup(dist_input)

    def output_dist(
        self, ctx: NullShardedModuleContext, output: torch.Tensor
    ) -> LazyAwaitable[torch.Tensor]:
        return EmbeddingAwaitable(
            awaitable=self._output_dist(output),
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
        # pyre-fixme[19]: Expected 0 positional arguments.
        lookup_state_dict = self._lookup.state_dict(None, "", keep_vars)
        # update key to match embeddingBag state_dict key
        for key, item in lookup_state_dict.items():
            new_key = prefix + key.split(".")[-1]
            destination[new_key] = item
        return destination

    def named_modules(
        self,
        memo: Optional[Set[nn.Module]] = None,
        prefix: str = "",
        remove_duplicate: bool = True,
    ) -> Iterator[Tuple[str, nn.Module]]:
        yield from [(prefix, self)]

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, nn.Parameter]]:
        # TODO: add remove_duplicate
        for name, parameter in self._lookup.named_parameters("", recurse):
            # update name to match embeddingBag parameter name
            yield append_prefix(prefix, name.split(".")[-1]), parameter

    def sharded_parameter_names(self, prefix: str = "") -> Iterator[str]:
        if self.parameter_sharding.sharding_type == ShardingType.DATA_PARALLEL.value:
            yield from []
        else:
            for name, _ in self._lookup.named_parameters(""):
                yield append_prefix(prefix, name.split(".")[-1])

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        # TODO: add remove_duplicate
        for name, buffer in self._lookup.named_buffers("", recurse):
            yield append_prefix(prefix, name.split(".")[-1]), buffer

    # pyre-fixme[14]: `load_state_dict` overrides method defined in `Module`
    #  inconsistently.
    def load_state_dict(
        self,
        state_dict: "OrderedDict[str, torch.Tensor]",
        strict: bool = True,
    ) -> _IncompatibleKeys:
        missing_keys = []
        unexpected_keys = []
        # update key to match  embeddingBag state_dict key
        for key, value in state_dict.items():
            new_key = ".".join([self._dummy_embedding_table_name, key])
            state_dict[new_key] = value
            state_dict.pop(key)
        missing, unexpected = self._lookup.load_state_dict(
            state_dict,
            strict,
        )
        missing_keys.extend(missing)
        unexpected_keys.extend(unexpected)

        return _IncompatibleKeys(
            missing_keys=missing_keys, unexpected_keys=unexpected_keys
        )

    @property
    def fused_optimizer(self) -> KeyedOptimizer:
        return self._optim

    def create_context(self) -> NullShardedModuleContext:
        return NullShardedModuleContext()


class EmbeddingBagSharder(BaseEmbeddingSharder[nn.EmbeddingBag]):
    """
    This implementation uses non-fused `nn.EmbeddingBag`
    """

    def shard(
        self,
        module: nn.EmbeddingBag,
        params: Dict[str, ParameterSharding],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
    ) -> ShardedEmbeddingBag:
        return ShardedEmbeddingBag(module, params, env, self.fused_params, device)

    def shardable_parameters(self, module: nn.EmbeddingBag) -> Dict[str, nn.Parameter]:
        return {name: param for name, param in module.named_parameters()}

    @property
    def module_type(self) -> Type[nn.EmbeddingBag]:
        return nn.EmbeddingBag
