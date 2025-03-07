#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

#!/usr/bin/env python3

import argparse
import copy
import logging
import os
import time
from functools import partial
from typing import List, Optional, Tuple

import torch

from torchrec.distributed.benchmark.benchmark_utils import (
    benchmark_module,
    BenchmarkResult,
    CompileMode,
    DLRM_NUM_EMBEDDINGS_PER_FEATURE,
    EMBEDDING_DIM,
    get_tables,
    init_argparse_and_args,
    write_report,
)
from torchrec.distributed.embedding_types import EmbeddingComputeKernel, ShardingType
from torchrec.distributed.test_utils.test_model import TestEBCSharder
from torchrec.distributed.types import DataType
from torchrec.modules.embedding_modules import EmbeddingBagCollection
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor


logger: logging.Logger = logging.getLogger()


BENCH_SHARDING_TYPES: List[ShardingType] = [
    ShardingType.TABLE_WISE,
    ShardingType.ROW_WISE,
    ShardingType.COLUMN_WISE,
]

BENCH_COMPILE_MODES: List[CompileMode] = [
    CompileMode.EAGER,
    # CompileMode.FX_SCRIPT,
]

TABLE_SIZES: List[Tuple[int, int]] = [
    (num_embeddings, EMBEDDING_DIM)
    for num_embeddings in DLRM_NUM_EMBEDDINGS_PER_FEATURE
]


def training_func_to_benchmark(
    model: torch.nn.Module,
    bench_inputs: List[KeyedJaggedTensor],
    optimizer: Optional[torch.optim.Optimizer],
) -> None:
    for bench_input in bench_inputs:
        pooled_embeddings = model(bench_input)
        vals = []
        for _name, param in pooled_embeddings.to_dict().items():
            vals.append(param)
        torch.cat(vals, dim=1).sum().backward()
        if optimizer:
            optimizer.step()
            optimizer.zero_grad()


def benchmark_ebc(
    tables: List[Tuple[int, int]], args: argparse.Namespace, output_dir: str
) -> List[BenchmarkResult]:
    table_configs = get_tables(tables, data_type=DataType.FP32)
    sharder = TestEBCSharder(
        sharding_type="",  # sharding_type gets populated during benchmarking
        kernel_type=EmbeddingComputeKernel.DENSE.value,
    )

    module = EmbeddingBagCollection(
        # pyre-ignore [6]
        tables=table_configs,
        is_weighted=False,
        device=torch.device("cpu"),
    )

    optimizer = torch.optim.SGD(module.parameters(), lr=0.02)
    args_kwargs = {
        argname: getattr(args, argname)
        for argname in dir(args)
        # Don't include output_dir since output_dir was modified
        if not argname.startswith("_") and argname != "output_dir"
    }

    return benchmark_module(
        module=module,
        sharder=sharder,
        sharding_types=BENCH_SHARDING_TYPES,
        compile_modes=BENCH_COMPILE_MODES,
        tables=table_configs,
        output_dir=output_dir,
        func_to_benchmark=training_func_to_benchmark,
        benchmark_func_kwargs={"optimizer": optimizer},
        **args_kwargs,
    )


def main() -> None:
    args: argparse.Namespace = init_argparse_and_args()

    num_requests = args.bench_iters * args.batch_size * args.num_benchmarks
    datetime_sfx: str = time.strftime("%Y%m%dT%H%M%S")

    output_dir = args.output_dir
    if not os.path.exists(output_dir):
        # Create output directory if not exist
        os.mkdir(output_dir)

    benchmark_results_per_module = []
    write_report_funcs_per_module = []
    shrunk_table_sizes = []

    for i in range(len(TABLE_SIZES)):
        if TABLE_SIZES[i][0] > 1000000:
            shrunk_table_sizes.append((1000000, TABLE_SIZES[i][1]))
        else:
            shrunk_table_sizes.append(TABLE_SIZES[i])

    for module_name in ["EmbeddingBagCollection"]:
        output_dir = args.output_dir + f"/run_{datetime_sfx}"
        output_dir += "_ebc"
        benchmark_func = benchmark_ebc

        if not os.path.exists(output_dir):
            # Place all outputs under the datetime folder
            os.mkdir(output_dir)

        tables_info = "\nTABLE SIZES:"
        for i, (num, dim) in enumerate(shrunk_table_sizes):
            # FP32 is 4 bytes
            mb = int(float(num * dim) / 1024 / 1024) * 4
            tables_info += f"\nTABLE[{i}][{num:9}, {dim:4}] {mb:6}Mb"

        report: str = (
            f"REPORT BENCHMARK {datetime_sfx} world_size:{args.world_size} batch_size:{args.batch_size}\n"
        )
        report += f"Module: {module_name}\n"
        report += tables_info
        report += "\n"

        report += f"num_requests:{num_requests:8}\n"
        report_file: str = f"{output_dir}/run.report"

        # Save results to output them once benchmarking is all done
        benchmark_results_per_module.append(
            benchmark_func(shrunk_table_sizes, args, output_dir)
        )
        write_report_funcs_per_module.append(
            partial(
                write_report,
                report_file=report_file,
                report_str=report,
                num_requests=num_requests,
            )
        )

    for i, write_report_func in enumerate(write_report_funcs_per_module):
        write_report_func(benchmark_results_per_module[i])


def invoke_main() -> None:
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)

    main()


if __name__ == "__main__":
    invoke_main()  # pragma: no cover
