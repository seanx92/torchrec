#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

#!/usr/bin/env python3

import unittest
from typing import List

import hypothesis.strategies as st

import torch
from caffe2.torch.fb.model_transform.splitting.split_dispatcher import SplitDispatchMode
from hypothesis import given, settings
from torchrec import EmbeddingBagConfig, EmbeddingCollection, EmbeddingConfig
from torchrec.distributed.embedding_types import ShardingType
from torchrec.distributed.planner import ParameterConstraints
from torchrec.distributed.planner.planners import HeteroEmbeddingShardingPlanner
from torchrec.distributed.planner.types import CustomTopologyData, Topology
from torchrec.distributed.quant_embedding import (
    QuantEmbeddingCollectionSharder,
    ShardedQuantEmbeddingCollection,
)
from torchrec.distributed.quant_embeddingbag import QuantEmbeddingBagCollectionSharder
from torchrec.distributed.shard import _shard_modules
from torchrec.distributed.sharding.rw_sharding import InferCPURwSparseFeaturesDist
from torchrec.distributed.sharding.tw_sharding import InferTwSparseFeaturesDist
from torchrec.distributed.sharding_plan import (
    construct_module_sharding_plan,
    row_wise,
    table_wise,
)
from torchrec.distributed.test_utils.infer_utils import KJTInputWrapper, quantize
from torchrec.distributed.types import ShardingEnv, ShardingPlan
from torchrec.modules.embedding_modules import EmbeddingBagCollection


class InferHeteroShardingsTest(unittest.TestCase):
    @unittest.skipIf(
        torch.cuda.device_count() <= 3,
        "Not enough GPUs available",
    )
    # pyre-ignore
    @given(
        sharding_device=st.sampled_from(["cpu"]),
    )
    @settings(max_examples=4, deadline=None)
    def test_sharder_different_world_sizes_for_qec(self, sharding_device: str) -> None:
        num_embeddings = 10
        emb_dim = 16
        world_size = 2
        local_size = 1
        tables = [
            EmbeddingConfig(
                num_embeddings=num_embeddings,
                embedding_dim=emb_dim,
                name=f"table_{i}",
                feature_names=[f"feature_{i}"],
            )
            for i in range(3)
        ]
        model = KJTInputWrapper(
            module_kjt_input=torch.nn.Sequential(
                EmbeddingCollection(
                    tables=tables,
                    device=torch.device("cpu"),
                )
            )
        )
        non_sharded_model = quantize(
            model,
            inplace=False,
            quant_state_dict_split_scale_bias=True,
            weight_dtype=torch.qint8,
        )
        sharder = QuantEmbeddingCollectionSharder()
        module_plan = construct_module_sharding_plan(
            non_sharded_model._module_kjt_input[0],
            per_param_sharding={
                "table_0": row_wise(([20, 10, 100], "cpu")),
                "table_1": table_wise(rank=0, device="cuda"),
                "table_2": table_wise(rank=1, device="cuda"),
            },
            # pyre-ignore
            sharder=sharder,
            local_size=local_size,
            world_size=world_size,
        )
        plan = ShardingPlan(plan={"_module_kjt_input.0": module_plan})
        env_dict = {
            "cpu": ShardingEnv.from_local(
                3,
                0,
            ),
            "cuda": ShardingEnv.from_local(
                2,
                0,
            ),
        }
        dummy_input = (
            ["feature_0", "feature_1", "feature_2"],
            torch.tensor([1, 1, 1]),
            None,
            torch.tensor([1, 1, 1]),
            None,
        )

        sharded_model = _shard_modules(
            module=non_sharded_model,
            # pyre-ignore
            sharders=[sharder],
            device=torch.device(sharding_device),
            plan=plan,
            env=env_dict,
        )

        self.assertTrue(hasattr(sharded_model._module_kjt_input[0], "_lookups"))
        self.assertTrue(len(sharded_model._module_kjt_input[0]._lookups) == 2)
        self.assertTrue(hasattr(sharded_model._module_kjt_input[0], "_input_dists"))

        for i, env in enumerate(env_dict.values()):
            self.assertTrue(
                hasattr(
                    sharded_model._module_kjt_input[0]._lookups[i],
                    "_embedding_lookups_per_rank",
                )
            )
            self.assertTrue(
                len(
                    sharded_model._module_kjt_input[0]
                    ._lookups[i]
                    ._embedding_lookups_per_rank
                )
                == env.world_size
            )

    # pyre-ignore
    @unittest.skipIf(
        torch.cuda.device_count() <= 3,
        "Not enough GPUs available",
    )
    def test_sharder_different_world_sizes_for_qebc(self) -> None:
        num_embeddings = 10
        emb_dim = 16
        world_size = 2
        local_size = 1
        tables = [
            EmbeddingBagConfig(
                num_embeddings=num_embeddings,
                embedding_dim=emb_dim,
                name=f"table_{i}",
                feature_names=[f"feature_{i}"],
            )
            for i in range(3)
        ]
        model = KJTInputWrapper(
            module_kjt_input=torch.nn.Sequential(
                EmbeddingBagCollection(
                    tables=tables,
                    device=torch.device("cpu"),
                )
            )
        )
        non_sharded_model = quantize(
            model,
            inplace=False,
            quant_state_dict_split_scale_bias=True,
            weight_dtype=torch.qint8,
        )
        sharder = QuantEmbeddingBagCollectionSharder()
        module_plan = construct_module_sharding_plan(
            non_sharded_model._module_kjt_input[0],
            per_param_sharding={
                "table_0": row_wise(([20, 10, 100], "cpu")),
                "table_1": table_wise(rank=0, device="cuda"),
                "table_2": table_wise(rank=1, device="cuda"),
            },
            # pyre-ignore
            sharder=sharder,
            local_size=local_size,
            world_size=world_size,
        )
        plan = ShardingPlan(plan={"_module_kjt_input.0": module_plan})
        env_dict = {
            "cpu": ShardingEnv.from_local(
                3,
                0,
            ),
            "cuda": ShardingEnv.from_local(
                2,
                0,
            ),
        }
        sharded_model = _shard_modules(
            module=non_sharded_model,
            # pyre-ignore
            sharders=[sharder],
            device=torch.device("cpu"),
            plan=plan,
            env=env_dict,
        )
        self.assertTrue(hasattr(sharded_model._module_kjt_input[0], "_lookups"))
        self.assertTrue(len(sharded_model._module_kjt_input[0]._lookups) == 2)
        for i, env in enumerate(env_dict.values()):
            self.assertTrue(
                hasattr(
                    sharded_model._module_kjt_input[0]._lookups[i],
                    "_embedding_lookups_per_rank",
                )
            )
            self.assertTrue(
                len(
                    sharded_model._module_kjt_input[0]
                    ._lookups[i]
                    ._embedding_lookups_per_rank
                )
                == env.world_size
            )

    # pyre-ignore
    @unittest.skipIf(
        torch.cuda.device_count() <= 3,
        "Not enough GPUs available",
    )
    def test_cpu_gpu_sharding_autoplanner(self) -> None:
        num_embeddings = 10
        emb_dim = 16
        tables = [
            EmbeddingConfig(
                num_embeddings=num_embeddings,
                embedding_dim=emb_dim,
                name=f"table_{i}",
                feature_names=[f"feature_{i}"],
            )
            for i in range(3)
        ]
        model = KJTInputWrapper(
            module_kjt_input=torch.nn.Sequential(
                EmbeddingCollection(
                    tables=tables,
                    device=torch.device("cpu"),
                )
            )
        )
        non_sharded_model = quantize(
            model,
            inplace=False,
            quant_state_dict_split_scale_bias=True,
            weight_dtype=torch.qint8,
        )
        sharder = QuantEmbeddingCollectionSharder()
        topo_cpu = Topology(world_size=3, compute_device="cpu")
        topo_gpu = Topology(world_size=2, compute_device="cuda")
        topo_groups = {
            "cpu": topo_cpu,
            "cuda": topo_gpu,
        }
        constraints = {
            "table_0": ParameterConstraints(device_group="cpu"),
            "table_1": ParameterConstraints(device_group="cuda"),
            "table_2": ParameterConstraints(device_group="cuda"),
        }
        planner = HeteroEmbeddingShardingPlanner(
            topology_groups=topo_groups, constraints=constraints
        )
        module_plan = planner.plan(
            non_sharded_model,
            # pyre-ignore
            sharders=[sharder],
        )
        print(module_plan)

        self.assertTrue(
            # pyre-ignore
            module_plan.plan["_module_kjt_input.0"]["table_0"]
            .sharding_spec.shards[0]
            .placement.device()
            .type,
            "cpu",
        )
        self.assertTrue(
            module_plan.plan["_module_kjt_input.0"]["table_1"]
            .sharding_spec.shards[0]
            .placement.device()
            .type,
            "cuda",
        )
        self.assertTrue(
            module_plan.plan["_module_kjt_input.0"]["table_2"]
            .sharding_spec.shards[0]
            .placement.device()
            .type,
            "cuda",
        )

    # pyre-ignore
    @unittest.skipIf(
        torch.cuda.device_count() <= 3,
        "Not enough GPUs available",
    )
    def test_cpu_gpu_sharding_shard_modules(self) -> None:
        num_embeddings = 10
        emb_dim = 16
        tables = [
            EmbeddingConfig(
                num_embeddings=num_embeddings,
                embedding_dim=emb_dim,
                name=f"table_{i}",
                feature_names=[f"feature_{i}"],
            )
            for i in range(3)
        ]
        model = KJTInputWrapper(
            module_kjt_input=torch.nn.Sequential(
                EmbeddingCollection(
                    tables=tables,
                    device=torch.device("cpu"),
                )
            )
        )
        non_sharded_model = quantize(
            model,
            inplace=False,
            quant_state_dict_split_scale_bias=True,
            weight_dtype=torch.qint8,
        )
        sharder = QuantEmbeddingCollectionSharder()
        env_dict = {
            "cpu": ShardingEnv.from_local(
                3,
                0,
            ),
            "cuda": ShardingEnv.from_local(
                2,
                0,
            ),
        }

        shard_model = _shard_modules(
            module=non_sharded_model,
            env=env_dict,
            # pyre-ignore
            sharders=[sharder],
            device=torch.device("cpu"),
        )

        self.assertTrue(
            isinstance(
                shard_model._module_kjt_input[0], ShardedQuantEmbeddingCollection
            )
        )

        self.assertEqual(len(shard_model._module_kjt_input[0]._lookups), 1)
        self.assertEqual(
            len(
                shard_model._module_kjt_input[0]._lookups[0]._embedding_lookups_per_rank
            ),
            env_dict["cpu"].world_size,
        )

    def test_cpu_gpu_uneven_sharding_shard_modules(self) -> None:
        num_embeddings = 1000
        emb_dim = 16
        tables = [
            EmbeddingConfig(
                num_embeddings=num_embeddings,
                embedding_dim=emb_dim,
                name=f"table_{i}",
                feature_names=[f"feature_{i}"],
            )
            for i in range(3)
        ]
        model = KJTInputWrapper(
            module_kjt_input=torch.nn.Sequential(
                EmbeddingCollection(
                    tables=tables,
                    device=torch.device("cpu"),
                )
            )
        )
        non_sharded_model = quantize(
            model,
            inplace=False,
            quant_state_dict_split_scale_bias=True,
            weight_dtype=torch.qint8,
        )
        sharder = QuantEmbeddingCollectionSharder()
        ddr_caps = [500, 100, 100]
        topo_cpu = Topology(
            world_size=3,
            compute_device="cpu",
            custom_topology_data=CustomTopologyData(
                data={"ddr_cap": ddr_caps}, world_size=3
            ),
        )
        topo_gpu = Topology(world_size=2, compute_device="cuda")
        topo_groups = {
            "cpu": topo_cpu,
            "cuda": topo_gpu,
        }
        constraints = {
            "table_0": ParameterConstraints(
                device_group="cpu", sharding_types=[ShardingType.ROW_WISE.value]
            ),
            "table_1": ParameterConstraints(
                device_group="cuda", sharding_types=[ShardingType.TABLE_WISE.value]
            ),
            "table_2": ParameterConstraints(
                device_group="cuda", sharding_types=[ShardingType.TABLE_WISE.value]
            ),
        }
        planner = HeteroEmbeddingShardingPlanner(
            topology_groups=topo_groups, constraints=constraints
        )
        module_plan = planner.plan(
            non_sharded_model,
            # pyre-ignore
            sharders=[sharder],
        )

        self.assertTrue(
            # pyre-ignore
            module_plan.plan["_module_kjt_input.0"]["table_0"]
            .sharding_spec.shards[0]
            .placement.device()
            .type,
            "cpu",
        )
        expected_row_sizes: List[int] = []
        total_ddr_cap = sum(ddr_caps)
        expected_row_sizes.append(int(num_embeddings * (ddr_caps[0] / total_ddr_cap)))
        expected_row_sizes.append(int(num_embeddings * (ddr_caps[1] / total_ddr_cap)))
        expected_row_sizes.append(num_embeddings - sum(expected_row_sizes))

        for i in range(len(ddr_caps)):
            self.assertEqual(
                module_plan.plan["_module_kjt_input.0"]["table_0"]
                .sharding_spec.shards[i]
                .shard_sizes[0],
                expected_row_sizes[i],
            )

        self.assertTrue(
            module_plan.plan["_module_kjt_input.0"]["table_1"]
            .sharding_spec.shards[0]
            .placement.device()
            .type,
            "cuda",
        )
        self.assertTrue(
            module_plan.plan["_module_kjt_input.0"]["table_2"]
            .sharding_spec.shards[0]
            .placement.device()
            .type,
            "cuda",
        )
