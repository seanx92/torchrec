#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple, TypeVar

import torch
import torch.distributed as dist

from torch import Tensor
from torch.autograd import Function
from torch.autograd.profiler import record_function
from torchrec.distributed.types import Awaitable, NoWait, QuantizedCommCodecs
from torchrec.distributed.utils import none_throws

try:
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops")
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops_cpu")
except OSError:
    pass


# OSS
try:
    pass
except ImportError:
    pass


try:
    from torch._dynamo import is_compiling as is_torchdynamo_compiling
except Exception:

    def is_torchdynamo_compiling() -> bool:  # type: ignore[misc]
        return False


W = TypeVar("W")

# TODO: T96382816, NE Parity Backward compatibility
GRADIENT_DIVISION: bool = True


def set_gradient_division(val: bool) -> None:
    global GRADIENT_DIVISION
    GRADIENT_DIVISION = val


def get_gradient_division() -> bool:
    global GRADIENT_DIVISION
    return GRADIENT_DIVISION


"""
Some commonly used notations for comm ops:
    B - batch size
    T - number of embedding tables
    D - embedding dimension
"""


class Request(Awaitable[W]):
    """
    Defines a collective operation request for a process group on a tensor.

    Args:
        pg (dist.ProcessGroup): The process group the request is for.
    """

    def __init__(self, pg: dist.ProcessGroup, device: torch.device) -> None:
        super().__init__()
        self.pg: dist.ProcessGroup = pg
        self.req: Optional[dist.Work] = None
        self.tensor: Optional[W] = None
        self.a2ai = None  # type: ignore
        self.qcomm_ctx = None  # type: ignore
        self.rsi = None  # type: ignore
        self.agi = None  # type: ignore
        self.wait_function = None  # type: ignore

        # This dummy tensor is used to build the autograd graph between
        # CommOp-Req and CommOp-Await. The actual forward tensors, and backwards gradient tensors
        # are stored in self.tensor
        self.dummy_tensor: torch.Tensor = torch.empty(
            1,
            requires_grad=True,
            device=device,
        )

    def _wait_impl(self) -> W:
        """
        Calls the wait function for this request.
        """

        ret = self.wait_function.apply(self.pg, self, self.dummy_tensor)
        self.req = None
        self.tensor = None
        return ret


@dataclass
class All2AllPooledInfo(object):
    """
    The data class that collects the attributes when calling the `alltoall_pooled`
    operation.

    Attributes:
        batch_size_per_rank (List[int]): batch size in each rank
        dim_sum_per_rank (List[int]): number of features (sum of dimensions) of the
            embedding in each rank.
        dim_sum_per_rank_tensor (Optional[Tensor]): the tensor version of
            `dim_sum_per_rank`, this is only used by the fast kernel of
            `_recat_pooled_embedding_grad_out`.
        cumsum_dim_sum_per_rank_tensor (Optional[Tensor]): cumulative sum of
            `dim_sum_per_rank`, this is only used by the fast kernel of
            `_recat_pooled_embedding_grad_out`.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.
    """

    batch_size_per_rank: List[int]
    dim_sum_per_rank: List[int]
    dim_sum_per_rank_tensor: Optional[Tensor]
    cumsum_dim_sum_per_rank_tensor: Optional[Tensor]
    codecs: Optional[QuantizedCommCodecs] = None


@dataclass
class VariableBatchAll2AllPooledInfo(object):
    """
    The data class that collects the attributes when calling the
    `variable_batch_alltoall_pooled` operation.

    Attributes:
        batch_size_per_rank_per_feature (List[List[int]]): batch size per rank per
            feature.
        batch_size_per_feature_pre_a2a (List[int]): local batch size before scattering.
        emb_dim_per_rank_per_feature (List[List[int]]): embedding dimension per rank
            per feature
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.
        input_splits (Optional[List[int]]): input splits of tensor all to all.
        output_splits (Optional[List[int]]): output splits of tensor all to all.
    """

    batch_size_per_rank_per_feature: List[List[int]]
    batch_size_per_feature_pre_a2a: List[int]
    emb_dim_per_rank_per_feature: List[List[int]]
    codecs: Optional[QuantizedCommCodecs] = None
    input_splits: Optional[List[int]] = None
    output_splits: Optional[List[int]] = None


@dataclass
class All2AllSequenceInfo(object):
    """
    The data class that collects the attributes when calling the `alltoall_sequence`
    operation.

    Attributes:
        embedding_dim (int): embedding dimension.
        lengths_after_sparse_data_all2all (Tensor): lengths of sparse features after
            AlltoAll.
        forward_recat_tensor (Optional[Tensor]): recat tensor for forward.
        backward_recat_tensor (Tensor): recat tensor for backward.
        input_splits (List[int]): input splits.
        output_splits (List[int]): output splits.
        variable_batch_size (bool): whether variable batch size is enabled.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.
        permuted_lengths_after_sparse_data_all2all (Optional[Tensor]): lengths of sparse
            features before AlltoAll.
    """

    embedding_dim: int
    lengths_after_sparse_data_all2all: Tensor
    forward_recat_tensor: Optional[Tensor]
    backward_recat_tensor: Tensor
    input_splits: List[int]
    output_splits: List[int]
    variable_batch_size: bool = False
    codecs: Optional[QuantizedCommCodecs] = None
    permuted_lengths_after_sparse_data_all2all: Optional[Tensor] = None


@dataclass
class All2AllVInfo(object):
    """
    The data class that collects the attributes when calling the `alltoallv` operation.

    Attributes:
        dim_sum_per_rank (List[int]): number of features (sum of dimensions) of the
            embedding in each rank.
        B_global (int): global batch size for each rank.
        B_local (int): local batch size before scattering.
        B_local_list: (List[int]): local batch sizes for each embedding table locally
            (in my current rank).
        D_local_list (List[int]): embedding dimension of each embedding table locally
            (in my current rank).
        input_split_sizes (List[int]): The input split sizes for each rank, this
            remembers how to split the input when doing the `all_to_all_single` operation.
        output_split_sizes (List[int]): The output split sizes for each rank, this
            remembers how to fill the output when doing the `all_to_all_single` operation.
    """

    dims_sum_per_rank: List[int]
    B_global: int
    B_local: int
    B_local_list: List[int]
    D_local_list: List[int]
    input_split_sizes: List[int] = field(default_factory=list)
    output_split_sizes: List[int] = field(default_factory=list)
    codecs: Optional[QuantizedCommCodecs] = None


@dataclass
class ReduceScatterInfo(object):
    """
    The data class that collects the attributes when calling the `reduce_scatter_pooled`
    operation.

    Attributes:
        input_sizes (List[torch.Size]): the sizes of the input tensors. This remembers the
            sizes of the input tensors when running the backward pass and producing the
            gradient.
    """

    input_sizes: List[torch.Size]
    codecs: Optional[QuantizedCommCodecs] = None


@dataclass
class ReduceScatterBaseInfo(object):
    """
    The data class that collects the attributes when calling the
    `reduce_scatter_base_pooled` operation.

    Attributes:
        input_sizes (torch.Size): the sizes of the input flatten tensor.
    """

    input_sizes: torch.Size
    codecs: Optional[QuantizedCommCodecs] = None


@dataclass
class AllGatherBaseInfo(object):
    """
    The data class that collects the attributes when calling the
    `all_gatther_base_pooled` operation.

    Attributes:
        input_size (int): the size of the input tensor.
    """

    input_size: torch.Size
    codecs: Optional[QuantizedCommCodecs] = None


@dataclass
class ReduceScatterVInfo(object):
    """
    The data class that collects the attributes when calling the `reduce_scatter_v_pooled`
    operation.

    Attributes:
        input_sizes (List[List[int]]): the sizes of the input tensors. This saves the
            sizes of the input tensors when running the backward pass and producing the
            gradient.
        input_splits (List[int]): the splits of the input tensors along dim 0.
        equal_splits (bool): ...
        total_input_size: (List[int]): total input size.
        codecs (Optional[QuantizedCommCodecs]): ...
    """

    input_sizes: List[List[int]]
    input_splits: List[int]
    equal_splits: bool
    total_input_size: List[int]
    codecs: Optional[QuantizedCommCodecs]


@dataclass
class All2AllDenseInfo(object):
    """
    The data class that collects the attributes when calling the `alltoall_dense`
    operation.
    """

    output_splits: List[int]
    batch_size: int
    input_shape: List[int]
    input_splits: List[int]


def _get_split_lengths_by_len(
    world_size: int, my_rank: int, n: int
) -> Tuple[int, List[int]]:
    k = n // world_size
    m = n % world_size
    splits = []
    if m == 0:
        for _ in range(world_size):
            splits.append(k)

        my_len = k
    else:
        for i in range(world_size):
            splits.append((k + 1) if i < m else k)
        my_len = splits[my_rank]
    return (my_len, splits)


def alltoall_pooled(
    a2a_pooled_embs_tensor: Tensor,
    batch_size_per_rank: List[int],
    dim_sum_per_rank: List[int],
    dim_sum_per_rank_tensor: Optional[Tensor] = None,
    cumsum_dim_sum_per_rank_tensor: Optional[Tensor] = None,
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:
    """
    Performs AlltoAll operation for a single pooled embedding tensor. Each process
    splits the input pooled embeddings tensor based on the world size, and then scatters
    the split list to all processes in the group. Then concatenates the received tensors
    from all processes in the group and returns a single output tensor.

    Args:
        a2a_pooled_embs_tensor (Tensor): input pooled embeddings. Must be pooled
            together before passing into this function. Its shape is `B x D_local_sum`,
            where `D_local_sum` is the dimension sum of all the local embedding tables.
        batch_size_per_rank (List[int]): batch size in each rank.
        dim_sum_per_rank (List[int]): number of features (sum of dimensions) of the
            embedding in each rank.
        dim_sum_per_rank_tensor (Optional[Tensor]): the tensor version of
            `dim_sum_per_rank`, this is only used by the fast kernel of
            `_recat_pooled_embedding_grad_out`.
        cumsum_dim_sum_per_rank_tensor (Optional[Tensor]): cumulative sum of
            `dim_sum_per_rank`, this is only used by the fast kernel of
            `_recat_pooled_embedding_grad_out`.
        group (Optional[dist.ProcessGroup]): the process group to work on. If None, the
            default process group will be used.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.

    Returns:
        Awaitable[Tensor]: async work handle (`Awaitable`), which can be `wait()` later to get the resulting tensor.

    .. warning::
        `alltoall_pooled` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    if group.size() <= 1:
        return NoWait(a2a_pooled_embs_tensor)

    a2ai = All2AllPooledInfo(
        batch_size_per_rank=batch_size_per_rank,
        dim_sum_per_rank=dim_sum_per_rank,
        dim_sum_per_rank_tensor=dim_sum_per_rank_tensor,
        cumsum_dim_sum_per_rank_tensor=cumsum_dim_sum_per_rank_tensor,
        codecs=codecs,
    )

    if is_torchdynamo_compiling():
        return NoWait(all2all_pooled_sync(group, a2ai, a2a_pooled_embs_tensor))

    myreq = Request(group, device=a2a_pooled_embs_tensor.device)
    All2All_Pooled_Req.apply(group, myreq, a2ai, a2a_pooled_embs_tensor)
    return myreq


def all2all_pooled_sync(
    pg: dist.ProcessGroup, a2ai: All2AllPooledInfo, input_embeddings: Tensor
) -> Tensor:
    my_rank = pg.rank()

    (B_global, D_local_sum) = input_embeddings.shape

    dim_sum_per_rank = a2ai.dim_sum_per_rank
    batch_size_per_rank = a2ai.batch_size_per_rank
    B_local = batch_size_per_rank[my_rank]

    assert B_global == sum(batch_size_per_rank)

    sharded_input_embeddings = input_embeddings.view(-1)

    if a2ai.codecs is not None:
        codecs = none_throws(a2ai.codecs)
        qcomm_ctx = codecs.forward.create_context()
        sharded_input_embeddings = codecs.forward.encode(
            sharded_input_embeddings,
            qcomm_ctx,
        )
        output_split_sizes = [
            codecs.forward.calc_quantized_size(
                B_local * D_rank_sum,
                qcomm_ctx,
            )
            for D_rank_sum in dim_sum_per_rank
        ]
        input_split_sizes = [
            codecs.forward.calc_quantized_size(
                D_local_sum * B_rank,
                qcomm_ctx,
            )
            for B_rank in batch_size_per_rank
        ]
    else:
        output_split_sizes = [B_local * D_rank_sum for D_rank_sum in dim_sum_per_rank]
        input_split_sizes = [D_local_sum * B_rank for B_rank in batch_size_per_rank]
        qcomm_ctx = None

    with record_function("## alltoall_fwd_single ##"):
        sharded_output_embeddings = dist._functional_collectives.all_to_all_single(
            sharded_input_embeddings,
            output_split_sizes,
            input_split_sizes,
            pg,
        )

    if a2ai.codecs is not None:
        codecs = none_throws(a2ai.codecs)
        sharded_output_embeddings = codecs.forward.decode(
            sharded_output_embeddings,
            qcomm_ctx,
        )

    outputs_by_rank = sharded_output_embeddings.split(output_split_sizes)
    return torch.cat([output.view(B_local, -1) for output in outputs_by_rank], dim=1)


def variable_batch_alltoall_pooled(
    a2a_pooled_embs_tensor: Tensor,
    batch_size_per_rank_per_feature: List[List[int]],
    batch_size_per_feature_pre_a2a: List[int],
    emb_dim_per_rank_per_feature: List[List[int]],
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    if dist.get_world_size(group) <= 1:
        return NoWait(a2a_pooled_embs_tensor)

    a2ai = VariableBatchAll2AllPooledInfo(
        batch_size_per_rank_per_feature=batch_size_per_rank_per_feature,
        batch_size_per_feature_pre_a2a=batch_size_per_feature_pre_a2a,
        emb_dim_per_rank_per_feature=emb_dim_per_rank_per_feature,
        codecs=codecs,
    )

    if is_torchdynamo_compiling():
        return NoWait(
            variable_batch_all2all_pooled_sync(group, a2ai, a2a_pooled_embs_tensor)
        )

    myreq = Request(group, device=a2a_pooled_embs_tensor.device)
    Variable_Batch_All2All_Pooled_Req.apply(group, myreq, a2ai, a2a_pooled_embs_tensor)
    return myreq


def variable_batch_all2all_pooled_sync(
    pg: dist.ProcessGroup,
    a2ai: VariableBatchAll2AllPooledInfo,
    input_embeddings: Tensor,
) -> Tensor:
    my_rank = pg.rank()

    # get input splits
    world_size = dist.get_world_size(pg)
    input_split_sizes = [0 for _ in range(world_size)]
    if a2ai.batch_size_per_rank_per_feature:
        for i in range(world_size):
            curr_size = 0
            for batch_size, emb_dim in zip(
                a2ai.batch_size_per_rank_per_feature[i],
                a2ai.emb_dim_per_rank_per_feature[my_rank],
            ):
                curr_size += batch_size * emb_dim
            input_split_sizes[i] = curr_size
    a2ai.input_splits = input_split_sizes

    # get output splits
    output_split_sizes = [0 for _ in range(world_size)]
    ind = 0
    for i in range(world_size):
        curr_size = 0
        for emb_dim in a2ai.emb_dim_per_rank_per_feature[i]:
            curr_size += a2ai.batch_size_per_feature_pre_a2a[ind] * emb_dim
            ind += 1
        output_split_sizes[i] = curr_size
    a2ai.output_splits = output_split_sizes

    sharded_input_embeddings = input_embeddings.view(-1)
    qcomm_ctx = None

    if a2ai.codecs is not None:
        codecs = none_throws(a2ai.codecs)
        qcomm_ctx = codecs.forward.create_context()
        sharded_input_embeddings = codecs.forward.encode(
            sharded_input_embeddings,
            qcomm_ctx,
        )
        output_split_sizes = [
            codecs.forward.calc_quantized_size(
                split,
                qcomm_ctx,
            )
            for split in output_split_sizes
        ]
        input_split_sizes = [
            codecs.forward.calc_quantized_size(
                split,
                qcomm_ctx,
            )
            for split in input_split_sizes
        ]

    with record_function("## alltoall_fwd_single ##"):
        sharded_output_embeddings = dist._functional_collectives.all_to_all_single(
            sharded_input_embeddings,
            output_split_sizes=output_split_sizes,
            input_split_sizes=input_split_sizes,
            group=pg,
        )

    if a2ai.codecs is not None:
        codecs = none_throws(a2ai.codecs)
        sharded_output_embeddings = codecs.forward.decode(
            sharded_output_embeddings,
            qcomm_ctx,
        )
    return sharded_output_embeddings


def alltoall_sequence(
    # (T, B, L_i * D) flattened
    a2a_sequence_embs_tensor: Tensor,
    forward_recat_tensor: Tensor,
    backward_recat_tensor: Tensor,
    lengths_after_sparse_data_all2all: Tensor,
    input_splits: List[int],
    output_splits: List[int],
    variable_batch_size: bool = False,
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:
    """
    Performs AlltoAll operation for sequence embeddings. Each process splits the input
    tensor based on the world size, and then scatters the split list to all processes in
    the group. Then concatenates the received tensors from all processes in the group
    and returns a single output tensor.

    NOTE:
        AlltoAll operator for Sequence embedding tensors.
        Does not support mixed dimensions.

    Args:
        a2a_sequence_embs_tensor (Tensor): input embeddings.
        forward_recat_tensor (Tensor): recat tensor for forward.
        backward_recat_tensor (Tensor): recat tensor for backward.
        lengths_after_sparse_data_all2all (Tensor): lengths of sparse features after
            AlltoAll.
        input_splits (List[int]): input splits.
        output_splits (List[int]): output splits.
        variable_batch_size (bool): whether variable batch size is enabled.
        group (Optional[dist.ProcessGroup]): the process group to work on. If None, the
            default process group will be used.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.

    Returns:
        Awaitable[List[Tensor]]: async work handle (`Awaitable`), which can be `wait()` later to get the resulting tensor.

    .. warning::
        `alltoall_sequence` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    if dist.get_world_size(group) <= 1:
        return NoWait(a2a_sequence_embs_tensor)

    a2ai = All2AllSequenceInfo(
        embedding_dim=a2a_sequence_embs_tensor.shape[1],
        lengths_after_sparse_data_all2all=lengths_after_sparse_data_all2all,
        forward_recat_tensor=forward_recat_tensor,
        backward_recat_tensor=backward_recat_tensor,
        input_splits=input_splits,
        output_splits=output_splits,
        variable_batch_size=variable_batch_size,
        codecs=codecs,
    )
    # sequence of embeddings, bags are definitely non-uniform

    if is_torchdynamo_compiling():
        return NoWait(all2all_sequence_sync(group, a2ai, a2a_sequence_embs_tensor))

    myreq = Request(group, device=a2a_sequence_embs_tensor.device)
    All2All_Seq_Req.apply(group, myreq, a2ai, a2a_sequence_embs_tensor)
    return myreq


def all2all_sequence_sync(
    pg: dist.ProcessGroup,
    a2ai: All2AllSequenceInfo,
    sharded_input_embeddings: Tensor,
) -> Tensor:
    world_size = pg.size()
    D = a2ai.embedding_dim
    forward_recat_tensor = a2ai.forward_recat_tensor
    variable_batch_size = a2ai.variable_batch_size
    lengths_after_sparse_data_all2all = a2ai.lengths_after_sparse_data_all2all * D
    input_splits = [i * D for i in a2ai.output_splits]
    output_splits = [i * D for i in a2ai.input_splits]

    a2ai.input_splits = input_splits
    a2ai.output_splits = output_splits

    local_T = lengths_after_sparse_data_all2all.shape[0]
    if local_T > 0:
        with record_function("## alltoall_seq_embedding_fwd_permute ##"):
            if not variable_batch_size:
                (
                    permuted_lengths_after_sparse_data_all2all,
                    sharded_input_embeddings,
                    _,
                ) = torch.ops.fbgemm.permute_2D_sparse_data(
                    forward_recat_tensor,
                    lengths_after_sparse_data_all2all.view(local_T * world_size, -1),
                    sharded_input_embeddings.view(-1),
                    None,
                    sharded_input_embeddings.numel(),
                )
            else:
                (
                    permuted_lengths_after_sparse_data_all2all,
                    sharded_input_embeddings,
                    _,
                ) = torch.ops.fbgemm.permute_1D_sparse_data(
                    forward_recat_tensor,
                    lengths_after_sparse_data_all2all.view(-1),
                    sharded_input_embeddings.view(-1),
                    None,
                    sharded_input_embeddings.numel(),
                )
    else:
        # Variable is not used in sync mode, left for conformity with async path
        permuted_lengths_after_sparse_data_all2all = None  # noqa: F841

    if a2ai.codecs is not None:
        codecs = none_throws(a2ai.codecs)
        qcomm_ctx = codecs.forward.create_context()
        # pyre-ignore [16]
        sharded_input_embeddings = a2ai.codecs.forward.encode(
            sharded_input_embeddings, qcomm_ctx
        )
        output_splits = [
            a2ai.codecs.forward.calc_quantized_size(x, qcomm_ctx) for x in output_splits
        ]
        input_splits = [
            a2ai.codecs.forward.calc_quantized_size(x, qcomm_ctx) for x in input_splits
        ]
    else:
        qcomm_ctx = None

    with record_function("## alltoall_seq_embedding_fwd_single ##"):
        sharded_output_embeddings = dist._functional_collectives.all_to_all_single(
            sharded_input_embeddings,
            output_split_sizes=output_splits,
            input_split_sizes=input_splits,
            group=pg,
        )

    if a2ai.codecs is not None:
        codecs = none_throws(a2ai.codecs)
        sharded_output_embeddings = codecs.forward.decode(
            sharded_output_embeddings, qcomm_ctx
        )
    return sharded_output_embeddings.view(-1, D)


def alltoallv(
    inputs: List[Tensor],
    out_split: Optional[List[int]] = None,
    per_rank_split_lengths: Optional[List[int]] = None,
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[List[Tensor]]:
    """
    Performs `alltoallv` operation for a list of input embeddings. Each process scatters
    the list to all processes in the group.

    Args:
        inputs (List[Tensor]): list of tensors to scatter, one per rank. The tensors in
            the list usually have different lengths.
        out_split (Optional[List[int]]): output split sizes (or dim_sum_per_rank), if
            not specified, we will use `per_rank_split_lengths` to construct a output
            split with the assumption that all the embs have the same dimension.
        per_rank_split_lengths (Optional[List[int]]): split lengths per rank. If not
            specified, the `out_split` must be specified.
        group (Optional[dist.ProcessGroup]): the process group to work on. If None, the
            default process group will be used.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.

    Returns:
        Awaitable[List[Tensor]]: async work handle (`Awaitable`), which can be `wait()` later to get the resulting list of tensors.

    .. warning::
        `alltoallv` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    world_size: int = group.size()
    my_rank: int = group.rank()

    B_global = inputs[0].size(0)

    D_local_list = []
    for e in inputs:
        D_local_list.append(e.size()[1])

    B_local, B_local_list = _get_split_lengths_by_len(world_size, my_rank, B_global)

    if out_split is not None:
        dims_sum_per_rank = out_split
    elif per_rank_split_lengths is not None:
        # all the embs have the same dimension
        dims_sum_per_rank = []
        for s in per_rank_split_lengths:
            dims_sum_per_rank.append(s * D_local_list[0])
    else:
        raise RuntimeError("Need to specify either out_split or per_rank_split_lengths")

    a2ai = All2AllVInfo(
        dims_sum_per_rank=dims_sum_per_rank,
        B_local=B_local,
        B_local_list=B_local_list,
        D_local_list=D_local_list,
        B_global=B_global,
        codecs=codecs,
    )

    if is_torchdynamo_compiling():
        return NoWait(all2allv_sync(group, a2ai, inputs))

    myreq = Request(group, device=inputs[0].device)
    All2Allv_Req.apply(group, myreq, a2ai, inputs)

    return myreq


def all2allv_sync(
    pg: dist.ProcessGroup,
    a2ai: All2AllVInfo,
    inputs: List[Tensor],
) -> List[Tensor]:
    input_split_sizes = []
    sum_D_local_list = sum(a2ai.D_local_list)
    for m in a2ai.B_local_list:
        input_split_sizes.append(m * sum_D_local_list)

    output_split_sizes = []
    for e in a2ai.dims_sum_per_rank:
        output_split_sizes.append(a2ai.B_local * e)

    input = torch.cat(inputs, dim=1).view([-1])
    if a2ai.codecs is not None:
        input = a2ai.codecs.forward.encode(input)

    with record_function("## alltoallv_bwd_single ##"):
        output = dist._functional_collectives.all_to_all_single(
            input,
            output_split_sizes=output_split_sizes,
            input_split_sizes=input_split_sizes,
            group=pg,
        )

    if a2ai.codecs is not None:
        output = a2ai.codecs.forward.decode(output)

    outputs = []
    for out in output.split(output_split_sizes):
        outputs.append(out.view([a2ai.B_local, -1]))
    return outputs


def reduce_scatter_pooled(
    inputs: List[Tensor],
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:
    """
    Performs reduce-scatter operation for a pooled embeddings tensor split into world
    size number of chunks. The result of the reduce operation gets scattered to all
    processes in the group.

    Args:
        inputs (List[Tensor]): list of tensors to scatter, one per rank.
        group (Optional[dist.ProcessGroup]): the process group to work on. If None, the
            default process group will be used.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.

    Returns:
        Awaitable[Tensor]: async work handle (Awaitable), which can be `wait()` later to get the resulting tensor.

    .. warning::
        `reduce_scatter_pooled` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    if group.size() <= 1:
        return NoWait(inputs[group.rank()])

    rsi = ReduceScatterInfo(
        input_sizes=[tensor.size() for tensor in inputs], codecs=codecs
    )

    if is_torchdynamo_compiling():
        return NoWait(reduce_scatter_sync(group, rsi, *inputs))

    myreq = Request(group, device=inputs[0].device)
    ReduceScatter_Req.apply(group, myreq, rsi, *inputs)
    return myreq


def reduce_scatter_sync(
    pg: dist.ProcessGroup,
    rsi: ReduceScatterInfo,
    *inputs: Any,
) -> Tensor:
    if rsi.codecs is not None:
        # pyre-ignore
        inputs = [rsi.codecs.forward.encode(input) for input in inputs]

    with record_function("## reduce_scatter ##"):
        output = dist._functional_collectives.reduce_scatter_tensor(
            torch.cat(inputs),
            reduceOp="sum",
            scatter_dim=0,
            group=pg,
        )
    if rsi.codecs is not None:
        output = rsi.codecs.forward.decode(output)
    return output


def reduce_scatter_base_pooled(
    input: Tensor,
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:
    """
    Reduces then scatters a flattened pooled embeddings tensor to all processes in a
    group.
    Input tensor is of size `output_tensor_size * world_size`.

    Args:
        input (Tensor): flattened tensor to scatter.
        group (Optional[dist.ProcessGroup]): the process group to work on. If None, the
            default process group will be used.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.

    Returns:
        Awaitable[Tensor]: async work handle (Awaitable), which can be `wait()` later to get the resulting tensor.

    .. warning::
        `reduce_scatter_base_pooled` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    if dist.get_world_size(group) <= 1:
        return NoWait(input)

    rsi = ReduceScatterBaseInfo(input_sizes=input.size(), codecs=codecs)

    if is_torchdynamo_compiling():
        return NoWait(reduce_scatter_base_sync(group, rsi, input))

    myreq = Request(group, device=input.device)
    ReduceScatterBase_Req.apply(group, myreq, rsi, input)
    return myreq


def reduce_scatter_base_sync(
    pg: dist.ProcessGroup,
    rsi: ReduceScatterBaseInfo,
    inputs: Tensor,
) -> Tensor:
    my_size = pg.size()
    assert inputs.size(0) % my_size == 0
    if rsi.codecs is not None:
        inputs = rsi.codecs.forward.encode(inputs)

    with record_function("## reduce_scatter_base ##"):
        output = dist._functional_collectives.reduce_scatter_tensor(
            inputs,
            reduceOp="sum",
            scatter_dim=0,
            group=pg,
        )
    if rsi.codecs is not None:
        output = rsi.codecs.forward.decode(output)
    return output


def all_gather_base_pooled(
    input: Tensor,
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:
    """
    All-gathers tensors from all processes in a group to form a flattened pooled
    embeddings tensor.
    Input tensor is of size `output_tensor_size / world_size`.

    Args:
        input (Tensor): tensor to gather.
        group (Optional[dist.ProcessGroup]): the process group to work on. If None, the
            default process group will be used.

    Returns:
        Awaitable[Tensor]: async work handle (Awaitable), which can be `wait()` later to get the resulting tensor.

    .. warning::
        `all_gather_base_pooled` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    agi = AllGatherBaseInfo(input_size=input.size(), codecs=codecs)
    if dist.get_world_size(group) <= 1:
        return NoWait(input)

    if is_torchdynamo_compiling():
        return NoWait(all_gather_base_sync(group, agi, input))

    myreq = Request(group, device=input.device)
    AllGatherBase_Req.apply(group, myreq, agi, input)
    return myreq


def all_gather_base_sync(
    pg: dist.ProcessGroup,
    agi: AllGatherBaseInfo,
    input: Tensor,
) -> Tensor:
    if agi.codecs is not None:
        input = agi.codecs.forward.encode(input)

    with record_function("## all_gather_base ##"):
        outputs = dist._functional_collectives.all_gather_tensor(
            input,
            gather_dim=0,
            group=pg,
        )
    if agi.codecs is not None:
        outputs = agi.codecs.forward.decode(outputs)
    return outputs


def reduce_scatter_v_pooled(
    input: Tensor,
    input_splits: List[int],
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:
    """
    Performs reduce-scatter-v operation for a pooled embeddings tensor split unevenly
    into world size number of chunks. The result of the reduce operation gets scattered
    to all processes in the group according to `input_splits`.

    Args:
        input (Tensor): tensor to scatter.
        input_splits (List[int]): input splits.
        group (Optional[dist.ProcessGroup]): the process group to work on. If None, the
            default process group will be used.

    Returns:
        Awaitable[Tensor]: async work handle (Awaitable), which can be `wait()` later to get the resulting tensor.

    .. warning::
        `reduce_scatter_v_pooled` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    if dist.get_world_size(group) <= 1:
        return NoWait(input)

    input_size = list(input.size())
    input_sizes = [
        [ip_split if d == 0 else input_size[d] for d in range(len(input_size))]
        for ip_split in input_splits
    ]
    equal_splits = all(ip_split == input_splits[0] for ip_split in input_splits)

    rsvi = ReduceScatterVInfo(
        input_sizes=input_sizes,
        input_splits=input_splits,
        equal_splits=equal_splits,
        total_input_size=input_size,
        codecs=codecs,
    )

    if is_torchdynamo_compiling():
        return NoWait(reduce_scatter_v_sync(group, rsvi, input))

    myreq = Request(group, device=input.device)
    ReduceScatterV_Req.apply(group, myreq, rsvi, input)
    return myreq


def reduce_scatter_v_sync(
    pg: dist.ProcessGroup,
    rsi: ReduceScatterVInfo,
    input: Tensor,
) -> Tensor:
    world_size = pg.size()
    rank = pg.rank()

    if rsi.codecs is not None:
        input = rsi.codecs.forward.encode(input)

    if rsi.equal_splits:
        with record_function("## reduce_scatter_base ##"):
            output = dist._functional_collectives.reduce_scatter_tensor(
                input,
                reduceOp="sum",
                scatter_dim=0,
                group=pg,
            )
    else:
        with record_function("## reduce_scatter_v_via_all_to_all_single ##"):
            input_splits = rsi.input_splits
            output_splits = [rsi.input_splits[rank]] * world_size
            # TODO(ivankobzarev): Replace with _functional_collectives.reduce_scatter_v when it is added
            a2a_output = dist._functional_collectives.all_to_all_single(
                input,
                output_splits,
                input_splits,
                pg,
            )
            output = torch.sum(
                torch.stack(torch.split(a2a_output, output_splits)), dim=0
            )

    if rsi.codecs is not None:
        output = rsi.codecs.forward.decode(output)

    return output


def reduce_scatter_v_per_feature_pooled(
    input: Tensor,
    batch_size_per_rank_per_feature: List[List[int]],
    embedding_dims: List[int],
    group: Optional[dist.ProcessGroup] = None,
    codecs: Optional[QuantizedCommCodecs] = None,
) -> Awaitable[Tensor]:
    """
    Performs reduce-scatter-v operation for a 1-d pooled embeddings tensor of variable
    batch size per feature split unevenly into world size number of chunks. The result
    of the reduce operation gets scattered to all processes in the group.

    Args:
        input (Tensor): tensors to scatter, one per rank.
        batch_size_per_rank_per_feature (List[List[int]]): batch size per rank per
            feature used to determine input splits.
        embedding_dims (List[int]): embedding dimensions per feature used to determine
            input splits.
        group (Optional[dist.ProcessGroup]): The process group to work on. If None, the
            default process group will be used.
        codecs (Optional[QuantizedCommCodecs]): quantized communication codecs.

    Returns:
        Awaitable[Tensor]: async work handle (Awaitable), which can be `wait()` later to get the resulting tensor.

    .. warning::
        `reduce_scatter_v_per_feature_pooled` is experimental and subject to change.
    """

    if group is None:
        group = dist.distributed_c10d._get_default_group()

    world_size = group.size()
    if world_size <= 1:
        return NoWait(input)

    input_splits = [0 for _ in range(world_size)]
    if batch_size_per_rank_per_feature:
        for rank in range(world_size):
            rank_splits = 0
            for batch_size, emb_dim in zip(
                batch_size_per_rank_per_feature[rank], embedding_dims
            ):
                rank_splits += batch_size * emb_dim
            input_splits[rank] = rank_splits
    input_sizes = [[s] for s in input_splits]

    rsvi = ReduceScatterVInfo(
        input_sizes=input_sizes,
        input_splits=input_splits,
        equal_splits=False,
        total_input_size=list(input.size()),
        codecs=codecs,
    )

    if is_torchdynamo_compiling():
        return NoWait(reduce_scatter_v_sync(group, rsvi, input))

    myreq = Request(group, device=input.device)
    ReduceScatterV_Req.apply(group, myreq, rsvi, input)
    return myreq


# TODO: improve performance of _recat_pooled_embedding_grad_out, see T87591139
def _recat_pooled_embedding_grad_out(
    grad_output: Tensor, num_features_per_rank: List[int]
) -> Tensor:
    grad_outputs_by_rank = grad_output.split(num_features_per_rank, dim=1)
    return torch.cat(
        [
            grad_output_by_rank.contiguous().view(-1)
            for grad_output_by_rank in grad_outputs_by_rank
        ],
        dim=0,
    )


def _recat_seq_embedding(
    input_embeddings: Tensor,
    split_sizes: List[int],
    T_local: int,
    my_size: int,
    forward: bool,
) -> Tensor:
    seq_embeddings_by_rank = input_embeddings.split(split_sizes)
    if forward:
        return torch.cat(
            [
                seq_embeddings_by_rank[t * my_size + i]
                # .contiguous().view(-1)
                for i in range(my_size)
                for t in range(T_local)
            ],
            dim=0,
        )
    else:
        return torch.cat(
            [
                seq_embeddings_by_rank[i * T_local + t]
                # .contiguous()
                # .view(-1)
                for t in range(T_local)
                for i in range(my_size)
            ],
            dim=0,
        )


class All2All_Pooled_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        a2ai: All2AllPooledInfo,
        input_embeddings: Tensor,
    ) -> Tensor:
        my_rank = dist.get_rank(pg)
        (B_global, D_local_sum) = input_embeddings.shape

        dim_sum_per_rank = a2ai.dim_sum_per_rank
        batch_size_per_rank = a2ai.batch_size_per_rank
        B_local = batch_size_per_rank[my_rank]

        assert B_global == sum(batch_size_per_rank)

        sharded_input_embeddings = input_embeddings.view(-1)

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            qcomm_ctx = codecs.forward.create_context()
            sharded_input_embeddings = codecs.forward.encode(
                sharded_input_embeddings,
                qcomm_ctx,
            )
            output_split_sizes = [
                codecs.forward.calc_quantized_size(
                    B_local * D_rank_sum,
                    qcomm_ctx,
                )
                for D_rank_sum in dim_sum_per_rank
            ]
            input_split_sizes = [
                codecs.forward.calc_quantized_size(
                    D_local_sum * B_rank,
                    qcomm_ctx,
                )
                for B_rank in batch_size_per_rank
            ]
        else:
            output_split_sizes = [
                B_local * D_rank_sum for D_rank_sum in dim_sum_per_rank
            ]
            input_split_sizes = [D_local_sum * B_rank for B_rank in batch_size_per_rank]
            qcomm_ctx = None

        sharded_output_embeddings = torch.empty(
            sum(output_split_sizes),
            dtype=sharded_input_embeddings.dtype,
            device=sharded_input_embeddings.device,
        )

        with record_function("## alltoall_fwd_single ##"):
            req = dist.all_to_all_single(
                output=sharded_output_embeddings,
                input=sharded_input_embeddings,
                output_split_sizes=output_split_sizes,
                input_split_sizes=input_split_sizes,
                group=pg,
                async_op=True,
            )

        myreq.req = req
        myreq.tensor = sharded_output_embeddings
        myreq.qcomm_ctx = qcomm_ctx
        myreq.a2ai = a2ai
        myreq.wait_function = All2All_Pooled_Wait
        ctx.myreq = myreq
        ctx.pg = pg
        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *unused) -> Tuple[None, None, None, Tensor]:
        pg = ctx.pg
        my_rank = dist.get_rank(pg)
        myreq = ctx.myreq
        a2ai = myreq.a2ai
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        grad_output = myreq.tensor
        dim_sum_per_rank = a2ai.dim_sum_per_rank
        batch_size_per_rank = a2ai.batch_size_per_rank
        D_local_sum = dim_sum_per_rank[my_rank]
        B_global = sum(batch_size_per_rank)
        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            grad_input = codecs.backward.decode(grad_output, myreq.qcomm_ctx)
            grad_input = grad_input.view(B_global, D_local_sum)
        else:
            grad_input = grad_output.view(B_global, D_local_sum)
        if GRADIENT_DIVISION:
            grad_input.div_(dist.get_world_size(ctx.pg))
        myreq.tensor = None
        myreq.dummy_tensor = None
        return (None, None, None, grad_input)


class All2All_Pooled_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_tensor: Tensor,
    ) -> Tensor:
        my_rank = dist.get_rank(pg)
        a2ai = myreq.a2ai
        ctx.a2ai = a2ai
        assert myreq.req is not None
        myreq.req.wait()
        sharded_output_embeddings = myreq.tensor
        myreq.req = None
        myreq.tensor = None
        ctx.pg = pg
        ctx.myreq = myreq
        dim_sum_per_rank = a2ai.dim_sum_per_rank
        batch_size_per_rank = a2ai.batch_size_per_rank
        B_local = batch_size_per_rank[my_rank]

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            sharded_output_embeddings = codecs.forward.decode(
                sharded_output_embeddings,
                myreq.qcomm_ctx,
            )

        outputs_by_rank = sharded_output_embeddings.split(
            [B_local * D_rank_sum for D_rank_sum in dim_sum_per_rank]
        )
        result = torch.cat(
            [output.view(B_local, -1) for output in outputs_by_rank], dim=1
        )
        return result

    @staticmethod
    # pyre-fixme[14]: `backward` overrides method defined in `Function` inconsistently.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, grad_output: Tensor) -> Tuple[None, None, Tensor]:
        myreq = ctx.myreq
        a2ai = ctx.a2ai
        pg = ctx.pg
        my_rank = dist.get_rank(pg)
        dim_sum_per_rank = a2ai.dim_sum_per_rank
        batch_size_per_rank = a2ai.batch_size_per_rank

        D_local_sum = dim_sum_per_rank[my_rank]
        (B_local, D_global_sum) = grad_output.shape
        assert sum(dim_sum_per_rank) == D_global_sum

        sharded_grad_output = _recat_pooled_embedding_grad_out(
            grad_output.contiguous(),
            dim_sum_per_rank,
        )

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            qcomm_ctx = codecs.backward.create_context()
            sharded_grad_output = codecs.backward.encode(
                sharded_grad_output,
                qcomm_ctx,
            )
            input_split_sizes = [
                codecs.backward.calc_quantized_size(
                    B_local * D_rank_sum,
                    qcomm_ctx,
                )
                for D_rank_sum in dim_sum_per_rank
            ]
            output_split_sizes = [
                codecs.backward.calc_quantized_size(
                    D_local_sum * B_rank,
                    qcomm_ctx,
                )
                for B_rank in batch_size_per_rank
            ]
        else:
            qcomm_ctx = None
            input_split_sizes = [
                B_local * D_rank_sum for D_rank_sum in dim_sum_per_rank
            ]
            output_split_sizes = [
                D_local_sum * B_rank for B_rank in batch_size_per_rank
            ]

        sharded_grad_input = torch.empty(
            sum(output_split_sizes),
            device=sharded_grad_output.device,
            dtype=sharded_grad_output.dtype,
        )
        with record_function("## alltoall_bwd_single ##"):
            req = dist.all_to_all_single(
                output=sharded_grad_input,
                input=sharded_grad_output,
                output_split_sizes=output_split_sizes,
                input_split_sizes=input_split_sizes,
                group=pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = sharded_grad_input
        myreq.qcomm_ctx = qcomm_ctx

        return (None, None, myreq.dummy_tensor)


class Variable_Batch_All2All_Pooled_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        a2ai: VariableBatchAll2AllPooledInfo,
        input_embeddings: Tensor,
    ) -> Tensor:
        my_rank = dist.get_rank(pg)

        # get input splits
        world_size = dist.get_world_size(pg)
        input_split_sizes = [0 for _ in range(world_size)]
        if a2ai.batch_size_per_rank_per_feature:
            for i in range(world_size):
                curr_size = 0
                for batch_size, emb_dim in zip(
                    a2ai.batch_size_per_rank_per_feature[i],
                    a2ai.emb_dim_per_rank_per_feature[my_rank],
                ):
                    curr_size += batch_size * emb_dim
                input_split_sizes[i] = curr_size
        a2ai.input_splits = input_split_sizes

        # get output splits
        output_split_sizes = [0 for _ in range(world_size)]
        ind = 0
        for i in range(world_size):
            curr_size = 0
            for emb_dim in a2ai.emb_dim_per_rank_per_feature[i]:
                curr_size += a2ai.batch_size_per_feature_pre_a2a[ind] * emb_dim
                ind += 1
            output_split_sizes[i] = curr_size
        a2ai.output_splits = output_split_sizes

        sharded_input_embeddings = input_embeddings.view(-1)
        qcomm_ctx = None

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            qcomm_ctx = codecs.forward.create_context()
            sharded_input_embeddings = codecs.forward.encode(
                sharded_input_embeddings,
                qcomm_ctx,
            )
            output_split_sizes = [
                codecs.forward.calc_quantized_size(
                    split,
                    qcomm_ctx,
                )
                for split in output_split_sizes
            ]
            input_split_sizes = [
                codecs.forward.calc_quantized_size(
                    split,
                    qcomm_ctx,
                )
                for split in input_split_sizes
            ]

        sharded_output_embeddings = torch.empty(
            sum(output_split_sizes),
            dtype=sharded_input_embeddings.dtype,
            device=sharded_input_embeddings.device,
        )

        with record_function("## alltoall_fwd_single ##"):
            req = dist.all_to_all_single(
                output=sharded_output_embeddings,
                input=sharded_input_embeddings,
                output_split_sizes=output_split_sizes,
                input_split_sizes=input_split_sizes,
                group=pg,
                async_op=True,
            )

        myreq.req = req
        myreq.tensor = sharded_output_embeddings
        myreq.qcomm_ctx = qcomm_ctx
        myreq.a2ai = a2ai
        myreq.wait_function = Variable_Batch_All2All_Pooled_Wait
        ctx.myreq = myreq
        ctx.pg = pg
        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *unused) -> Tuple[None, None, None, Tensor]:
        myreq = ctx.myreq
        a2ai = myreq.a2ai
        assert myreq.req is not None
        myreq.req.wait()
        if isinstance(myreq.req, dist.Work):
            myreq.req.wait()

        myreq.req = None
        grad_output = myreq.tensor

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            grad_input = codecs.backward.decode(grad_output, myreq.qcomm_ctx)
        else:
            grad_input = grad_output
        if GRADIENT_DIVISION:
            grad_input.div_(dist.get_world_size(ctx.pg))
        myreq.tensor = None
        myreq.dummy_tensor = None
        return (None, None, None, grad_input)


class Variable_Batch_All2All_Pooled_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_tensor: Tensor,
    ) -> Tensor:
        a2ai = myreq.a2ai
        ctx.a2ai = a2ai
        assert myreq.req is not None
        if isinstance(myreq.req, dist.Work):
            myreq.req.wait()
        sharded_output_embeddings = myreq.tensor
        myreq.req = None
        myreq.tensor = None
        ctx.pg = pg
        ctx.myreq = myreq

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            sharded_output_embeddings = codecs.forward.decode(
                sharded_output_embeddings,
                myreq.qcomm_ctx,
            )
        # the return result is a 1-d tensor, like: f_0_s_0, f_0_s1, ..., f_n_s_0, f_n_s_k
        # f_0, f_1, ... , f_n are ordered by features on each rank
        return sharded_output_embeddings

    @staticmethod
    # pyre-fixme[14]: `backward` overrides method defined in `Function` inconsistently.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, grad_output: Tensor) -> Tuple[None, None, Tensor]:
        myreq = ctx.myreq
        a2ai = ctx.a2ai
        pg = ctx.pg

        assert a2ai.input_splits is not None
        assert a2ai.output_splits is not None
        input_split_sizes = a2ai.output_splits
        output_split_sizes = a2ai.input_splits

        sharded_grad_output = grad_output.contiguous()
        qcomm_ctx = None

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            qcomm_ctx = codecs.backward.create_context()
            sharded_grad_output = codecs.backward.encode(
                sharded_grad_output,
                qcomm_ctx,
            )
            input_split_sizes = [
                codecs.backward.calc_quantized_size(
                    split,
                    qcomm_ctx,
                )
                for split in input_split_sizes
            ]
            output_split_sizes = [
                codecs.backward.calc_quantized_size(
                    split,
                    qcomm_ctx,
                )
                for split in output_split_sizes
            ]

        sharded_grad_input = torch.empty(
            sum(output_split_sizes),
            device=sharded_grad_output.device,
            dtype=sharded_grad_output.dtype,
        )
        with record_function("## alltoall_bwd_single ##"):
            req = dist.all_to_all_single(
                output=sharded_grad_input,
                input=sharded_grad_output,
                output_split_sizes=output_split_sizes,
                input_split_sizes=input_split_sizes,
                group=pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = sharded_grad_input
        myreq.qcomm_ctx = qcomm_ctx

        return (None, None, myreq.dummy_tensor)


class All2All_Seq_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        a2ai: All2AllSequenceInfo,
        sharded_input_embeddings: Tensor,
    ) -> Tensor:
        world_size = dist.get_world_size(pg)
        my_rank = dist.get_rank(pg)
        D = a2ai.embedding_dim
        forward_recat_tensor = a2ai.forward_recat_tensor
        variable_batch_size = a2ai.variable_batch_size
        lengths_after_sparse_data_all2all = a2ai.lengths_after_sparse_data_all2all * D
        input_splits = [i * D for i in a2ai.output_splits]
        output_splits = [i * D for i in a2ai.input_splits]

        a2ai.input_splits = input_splits
        a2ai.output_splits = output_splits

        local_T = lengths_after_sparse_data_all2all.shape[0]
        if local_T > 0:
            with record_function("## alltoall_seq_embedding_fwd_permute ##"):
                if not variable_batch_size:
                    (
                        permuted_lengths_after_sparse_data_all2all,
                        sharded_input_embeddings,
                        _,
                    ) = torch.ops.fbgemm.permute_2D_sparse_data(
                        forward_recat_tensor,
                        lengths_after_sparse_data_all2all.view(
                            local_T * world_size, -1
                        ),
                        sharded_input_embeddings.view(-1),
                        None,
                        sharded_input_embeddings.numel(),
                    )
                else:
                    (
                        permuted_lengths_after_sparse_data_all2all,
                        sharded_input_embeddings,
                        _,
                    ) = torch.ops.fbgemm.permute_1D_sparse_data(
                        forward_recat_tensor,
                        lengths_after_sparse_data_all2all.view(-1),
                        sharded_input_embeddings.view(-1),
                        None,
                        sharded_input_embeddings.numel(),
                    )
        else:
            permuted_lengths_after_sparse_data_all2all = None

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            qcomm_ctx = codecs.forward.create_context()
            # pyre-ignore [16]
            sharded_input_embeddings = a2ai.codecs.forward.encode(
                sharded_input_embeddings, qcomm_ctx
            )
            output_splits = [
                a2ai.codecs.forward.calc_quantized_size(x, qcomm_ctx)
                for x in output_splits
            ]
            input_splits = [
                a2ai.codecs.forward.calc_quantized_size(x, qcomm_ctx)
                for x in input_splits
            ]
        else:
            qcomm_ctx = None

        sharded_output_embeddings = torch.empty(
            sum(output_splits),
            dtype=sharded_input_embeddings.dtype,
            device=sharded_input_embeddings.device,
        )

        with record_function("## alltoall_seq_embedding_fwd_single ##"):
            req = dist.all_to_all_single(
                output=sharded_output_embeddings,
                input=sharded_input_embeddings,
                output_split_sizes=output_splits,
                input_split_sizes=input_splits,
                group=pg,
                async_op=True,
            )
        a2ai.permuted_lengths_after_sparse_data_all2all = (
            permuted_lengths_after_sparse_data_all2all
        )
        myreq.req = req
        myreq.tensor = sharded_output_embeddings
        myreq.a2ai = a2ai
        myreq.wait_function = All2All_Seq_Req_Wait
        ctx.myreq = myreq
        myreq.qcomm_ctx = qcomm_ctx
        ctx.pg = pg
        ctx.my_rank = my_rank
        ctx.world_size = world_size
        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *unused) -> Tuple[None, None, None, Tensor]:
        myreq = ctx.myreq
        a2ai = myreq.a2ai
        D = a2ai.embedding_dim
        variable_batch_size = a2ai.variable_batch_size
        backward_recat_tensor = a2ai.backward_recat_tensor
        permuted_lengths_after_sparse_data_all2all = (
            a2ai.permuted_lengths_after_sparse_data_all2all
        )
        assert myreq.req is not None
        myreq.req.wait()
        sharded_grad_input = myreq.tensor
        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            sharded_grad_input = codecs.backward.decode(
                sharded_grad_input, myreq.qcomm_ctx
            )
        myreq.req = None
        myreq.tensor = None
        myreq.dummy_tensor = None

        if permuted_lengths_after_sparse_data_all2all is not None:
            with record_function("## alltoall_seq_embedding_bwd_permute ##"):
                if not variable_batch_size:
                    _, sharded_grad_input, _ = torch.ops.fbgemm.permute_2D_sparse_data(
                        backward_recat_tensor,
                        permuted_lengths_after_sparse_data_all2all,
                        sharded_grad_input,
                        None,
                        sharded_grad_input.numel(),
                    )
                else:
                    _, sharded_grad_input, _ = torch.ops.fbgemm.permute_1D_sparse_data(
                        backward_recat_tensor,
                        permuted_lengths_after_sparse_data_all2all,
                        sharded_grad_input,
                        None,
                        sharded_grad_input.numel(),
                    )
        if GRADIENT_DIVISION:
            sharded_grad_input.div_(dist.get_world_size(ctx.pg))
        return (None, None, None, sharded_grad_input.view(-1, D))


class All2All_Seq_Req_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_tensor: torch.Tensor,
    ) -> Tensor:
        a2ai = myreq.a2ai
        D = a2ai.embedding_dim
        ctx.a2ai = a2ai
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        sharded_output_embeddings = myreq.tensor
        myreq.tensor = None
        ctx.pg = pg
        ctx.myreq = myreq
        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            sharded_output_embeddings = codecs.forward.decode(
                sharded_output_embeddings, myreq.qcomm_ctx
            )
        return sharded_output_embeddings.view(-1, D)

    @staticmethod
    # pyre-fixme[14]: `backward` overrides method defined in `Function` inconsistently.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, sharded_grad_output: Tensor) -> Tuple[None, None, Tensor]:
        myreq = ctx.myreq
        a2ai = ctx.a2ai
        pg = ctx.pg
        input_splits = a2ai.output_splits
        output_splits = a2ai.input_splits

        if a2ai.codecs is not None:
            codecs = none_throws(a2ai.codecs)
            qcomm_ctx = codecs.backward.create_context()
            sharded_grad_output = a2ai.codecs.backward.encode(
                sharded_grad_output, qcomm_ctx
            )
            output_splits = [
                a2ai.codecs.backward.calc_quantized_size(x, qcomm_ctx)
                for x in output_splits
            ]
            input_splits = [
                a2ai.codecs.backward.calc_quantized_size(x, qcomm_ctx)
                for x in input_splits
            ]
        else:
            qcomm_ctx = None

        sharded_grad_input = torch.empty(
            sum(output_splits),
            device=sharded_grad_output.device,
            dtype=sharded_grad_output.dtype,
        )
        with record_function("## alltoall_seq_embedding_bwd_single ##"):
            req = dist.all_to_all_single(
                output=sharded_grad_input,
                input=sharded_grad_output.view(-1),
                output_split_sizes=output_splits,
                input_split_sizes=input_splits,
                group=pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = sharded_grad_input
        myreq.qcomm_ctx = qcomm_ctx

        return (None, None, myreq.dummy_tensor)


class All2Allv_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        a2ai: All2AllVInfo,
        inputs: List[Tensor],
    ) -> Tensor:
        input_split_sizes = [m * sum(a2ai.D_local_list) for m in a2ai.B_local_list]
        output_split_sizes = [a2ai.B_local * e for e in a2ai.dims_sum_per_rank]
        input = torch.cat(inputs, dim=1).view([-1])
        if a2ai.codecs is not None:
            input = a2ai.codecs.forward.encode(input)

        output = input.new_empty(sum(output_split_sizes))
        with record_function("## alltoallv_bwd_single ##"):
            req = dist.all_to_all_single(
                output,
                input,
                output_split_sizes,
                input_split_sizes,
                group=pg,
                async_op=True,
            )

        myreq.req = req
        myreq.tensor = output
        myreq.wait_function = All2Allv_Wait
        a2ai.input_split_sizes = input_split_sizes
        a2ai.output_split_sizes = output_split_sizes
        myreq.a2ai = a2ai
        ctx.a2ai = a2ai
        ctx.myreq = myreq
        ctx.tensor = output
        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[3]: Return type must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *grad_output):
        a2ai = ctx.a2ai
        myreq = ctx.myreq
        myreq.req.wait()
        myreq.req = None
        grad_input = myreq.tensor
        if a2ai.codecs is not None:
            grad_input = a2ai.codecs.backward.decode(grad_input)

        grad_inputs = grad_input.view([a2ai.B_global, -1]).split(
            a2ai.D_local_list, dim=1
        )
        grad_inputs = [gin.contiguous() for gin in grad_inputs]
        myreq.tensor = None
        myreq.dummy_tensor = None
        return (None, None, None, *grad_inputs)


class All2Allv_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_tensor: torch.Tensor,
    ) -> Tuple[Tensor]:
        a2ai = myreq.a2ai
        ctx.a2ai = a2ai
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        output = myreq.tensor
        myreq.tensor = None
        ctx.pg = pg
        ctx.myreq = myreq

        if a2ai.codecs is not None:
            output = a2ai.codecs.forward.decode(output)
        outputs = tuple(
            [
                out.view([a2ai.B_local, -1])
                for out in output.split(a2ai.output_split_sizes)
            ]
        )
        return outputs

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *grad_outputs) -> Tuple[None, None, Tensor]:
        pg = ctx.pg
        myreq = ctx.myreq
        a2ai = ctx.a2ai

        if a2ai.codecs is not None:
            grad_outputs = a2ai.codecs.backward.encode(grad_outputs)

        grad_outputs = [gout.contiguous().view([-1]) for gout in grad_outputs]
        grad_output = torch.cat(grad_outputs)
        grad_input = grad_output.new_empty([a2ai.B_global * sum(a2ai.D_local_list)])
        with record_function("## alltoall_bwd_single ##"):
            req = dist.all_to_all_single(
                grad_input,
                grad_output,
                a2ai.input_split_sizes,
                a2ai.output_split_sizes,
                group=pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = grad_input
        return (None, None, myreq.dummy_tensor)


class ReduceScatter_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        rsi: ReduceScatterInfo,
        *inputs: Any,
    ) -> Tensor:
        my_rank = dist.get_rank(pg)

        if rsi.codecs is not None:
            # pyre-ignore
            inputs = [rsi.codecs.forward.encode(input) for input in inputs]

        output = inputs[my_rank].new_empty(
            inputs[my_rank].size(),
            dtype=inputs[my_rank].dtype,
            device=inputs[my_rank].device,
        )
        with record_function("## reduce_scatter ##"):
            req = dist.reduce_scatter(
                output,
                list(inputs),
                group=pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = output
        myreq.wait_function = ReduceScatter_Wait
        myreq.rsi = rsi
        ctx.myreq = myreq
        ctx.pg = pg
        ctx.tensor = output
        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *unused: Tensor) -> Tuple[Optional[Tensor], ...]:
        myreq = ctx.myreq
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        grad_inputs = list(myreq.tensor)
        rsi = myreq.rsi
        if rsi.codecs is not None:
            grad_inputs = [
                rsi.codecs.backward.decode(grad_input) for grad_input in grad_inputs
            ]
        # Make it equivalent to running on a single rank.
        if GRADIENT_DIVISION:
            for grad_input in grad_inputs:
                grad_input.div_(dist.get_world_size(ctx.pg))
        myreq.tensor = None
        myreq.dummy_tensor
        return (None, None, None, *grad_inputs)


class ReduceScatter_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_tensor: Tensor,
    ) -> Tensor:
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        output = myreq.tensor
        myreq.tensor = None
        ctx.myreq = myreq
        ctx.pg = pg

        rsi = myreq.rsi
        if rsi.codecs is not None:
            output = rsi.codecs.forward.decode(output)
        return output

    @staticmethod
    # pyre-fixme[14]: `backward` overrides method defined in `Function` inconsistently.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, grad_output: Tensor) -> Tuple[None, None, Tensor]:
        myreq = ctx.myreq
        rsi = myreq.rsi
        if rsi.codecs is not None:
            grad_output = rsi.codecs.backward.encode(grad_output)

        grad_inputs = [
            grad_output.new_empty(
                in_size,
                dtype=grad_output.dtype,
                device=grad_output.device,
            )
            for in_size in rsi.input_sizes
        ]

        with record_function("## reduce_scatter_bw (all_gather) ##"):
            req = dist.all_gather(
                grad_inputs,
                grad_output.contiguous(),
                group=ctx.pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = grad_inputs
        return (None, None, myreq.dummy_tensor)


class ReduceScatterBase_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        rsi: ReduceScatterBaseInfo,
        inputs: Tensor,
    ) -> Tensor:
        my_size = dist.get_world_size(pg)
        assert inputs.size(0) % my_size == 0
        if rsi.codecs is not None:
            inputs = rsi.codecs.forward.encode(inputs)
        output = inputs.new_empty((inputs.size(0) // my_size, inputs.size(1)))
        with record_function("## reduce_scatter_base ##"):
            req = dist._reduce_scatter_base(
                output,
                inputs,
                group=pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = output
        myreq.wait_function = ReduceScatterBase_Wait
        myreq.rsi = rsi
        myreq.tensor = output
        ctx.myreq = myreq
        ctx.pg = pg

        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *unused: Tensor) -> Tuple[Optional[Tensor], ...]:
        myreq = ctx.myreq
        myreq.req.wait()
        myreq.req = None
        grad_inputs = myreq.tensor
        rsi = myreq.rsi
        if rsi.codecs is not None:
            grad_inputs = rsi.codecs.backward.decode(grad_inputs)
        # Make it equivalent to running on a single rank.
        if GRADIENT_DIVISION:
            grad_inputs.div_(dist.get_world_size(ctx.pg))
        myreq.tensor = None
        myreq.dummy_tensor = None
        return (None, None, None, grad_inputs)


class ReduceScatterBase_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_Tensor: Tensor,
    ) -> Tensor:
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        output = myreq.tensor
        myreq.tensor = None
        ctx.myreq = myreq
        ctx.pg = pg
        rsi = myreq.rsi

        if rsi.codecs is not None:
            output = rsi.codecs.forward.decode(output)
        return output

    @staticmethod
    # pyre-fixme[14]: `backward` overrides method defined in `Function` inconsistently.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, grad_output: Tensor) -> Tuple[None, None, Tensor]:
        myreq = ctx.myreq
        rsi = myreq.rsi

        if rsi.codecs is not None:
            grad_output = rsi.codecs.backward.encode(grad_output)
        grad_inputs = grad_output.new_empty(rsi.input_sizes)
        with record_function("## reduce_scatter_base_bw (all_gather) ##"):
            req = dist._all_gather_base(
                grad_inputs,
                grad_output.contiguous(),
                group=ctx.pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = grad_inputs
        return (None, None, myreq.dummy_tensor)


class AllGatherBase_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        agi: AllGatherBaseInfo,
        input: Tensor,
    ) -> Tensor:
        my_size = dist.get_world_size(pg)

        if agi.codecs is not None:
            input = agi.codecs.forward.encode(input)

        outputs = input.new_empty((input.size(0) * my_size, input.size(1)))
        with record_function("## all_gather_base ##"):
            req = dist._all_gather_base(
                outputs,
                input,
                group=pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = outputs
        myreq.wait_function = AllGatherBase_Wait
        myreq.agi = agi
        ctx.myreq = myreq
        ctx.pg = pg
        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *unused: Tensor) -> Tuple[Optional[Tensor], ...]:
        myreq = ctx.myreq
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        agi = myreq.agi
        grad_input = myreq.tensor
        if agi.codecs is not None:
            grad_input = agi.codecs.backward.decode(grad_input)

        # Make it equivalent to running on a single rank.
        if GRADIENT_DIVISION:
            grad_input.div_(dist.get_world_size(ctx.pg))
        myreq.tensor = None
        myreq.dummy_tensor = None
        return (None, None, None, grad_input)


class AllGatherBase_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_tensor: Tensor,
    ) -> Tensor:
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        outputs = myreq.tensor
        myreq.tensor = None
        ctx.myreq = myreq
        ctx.pg = pg

        agi = myreq.agi
        if agi.codecs is not None:
            outputs = agi.codecs.forward.decode(outputs)
        return outputs

    @staticmethod
    # pyre-fixme[14]: `backward` overrides method defined in `Function` inconsistently.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, grad_outputs: Tensor) -> Tuple[None, None, Tensor]:
        myreq = ctx.myreq
        agi = myreq.agi

        if agi.codecs is not None:
            grad_outputs = agi.codecs.backward.encode(grad_outputs)
        grad_input = grad_outputs.new_empty(agi.input_size)
        with record_function("## all_gather_base_bw (reduce_scatter) ##"):
            req = dist._reduce_scatter_base(
                grad_input,
                grad_outputs.contiguous(),
                group=ctx.pg,
                async_op=True,
            )
        myreq.req = req
        myreq.tensor = grad_input

        return (None, None, myreq.dummy_tensor)


class ReduceScatterV_Req(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        rsi: ReduceScatterVInfo,
        input: Tensor,
    ) -> Tensor:
        my_rank = dist.get_rank(pg)

        if rsi.codecs is not None:
            input = rsi.codecs.forward.encode(input)

        output = input.new_empty(rsi.input_sizes[my_rank])

        # Use dist._reduce_scatter_base when a vector reduce-scatter is not needed
        # else use dist.reduce_scatter which internally supports vector reduce-scatter
        if rsi.equal_splits:
            with record_function("## reduce_scatter_base ##"):
                req = dist._reduce_scatter_base(
                    output,
                    input,
                    group=pg,
                    async_op=True,
                )
        else:
            with record_function("## reduce_scatter_v ##"):
                req = dist.reduce_scatter(
                    output,
                    list(torch.split(input, rsi.input_splits)),
                    group=pg,
                    async_op=True,
                )

        myreq.req = req
        myreq.tensor = output
        myreq.wait_function = ReduceScatterV_Wait
        myreq.rsi = rsi
        ctx.myreq = myreq
        ctx.pg = pg

        return myreq.dummy_tensor

    @staticmethod
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, *unused: Tensor) -> Tuple[Optional[Tensor], ...]:
        myreq = ctx.myreq
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        grad_input = myreq.tensor
        rsi = myreq.rsi
        if rsi.codecs is not None:
            grad_input = rsi.codecs.backward.decode(grad_input)
        # Make it equivalent to running on a single rank.
        if GRADIENT_DIVISION:
            grad_input.div_(dist.get_world_size(ctx.pg))
        myreq.tensor = None
        myreq.dummy_tensor = None
        return (None, None, None, grad_input)


class ReduceScatterV_Wait(Function):
    @staticmethod
    # pyre-fixme[14]: `forward` overrides method defined in `Function` inconsistently.
    def forward(
        # pyre-fixme[2]: Parameter must be annotated.
        ctx,
        pg: dist.ProcessGroup,
        myreq: Request[Tensor],
        *dummy_tensor: Tensor,
    ) -> Tensor:
        assert myreq.req is not None
        myreq.req.wait()
        myreq.req = None
        # pyre-ignore
        output: torch.Tensor = myreq.tensor
        myreq.tensor = None

        ctx.myreq = myreq
        ctx.pg = pg

        rsi = myreq.rsi
        if rsi.codecs is not None:
            output = rsi.codecs.forward.decode(output)

        return output

    @staticmethod
    # pyre-fixme[14]: `backward` overrides method defined in `Function` inconsistently.
    # pyre-fixme[2]: Parameter must be annotated.
    def backward(ctx, grad_output: Tensor) -> Tuple[None, None, Tensor]:
        myreq = ctx.myreq
        rsi = myreq.rsi
        if rsi.codecs is not None:
            grad_output = rsi.codecs.backward.encode(grad_output)
        grad_input = grad_output.new_empty(rsi.total_input_size)

        if rsi.equal_splits:
            with record_function("## reduce_scatter_base_bw (all_gather) ##"):
                req = dist._all_gather_base(
                    grad_input,
                    grad_output.contiguous(),
                    group=ctx.pg,
                    async_op=True,
                )
        else:
            with record_function("## reduce_scatter_v_bw (all_gather_v) ##"):
                req = dist.all_gather(
                    list(torch.split(grad_input, rsi.input_splits)),
                    grad_output.contiguous(),
                    group=ctx.pg,
                    async_op=True,
                )
        myreq.req = req
        myreq.tensor = grad_input
        return (None, None, myreq.dummy_tensor)


"""


Torch does not provide Autograd formulas for functional_collectives.
Registering them here in dynamo compatible way - when functional collective is called below Autograd.
This will be a leaf in dynamo trace.
"""


class _All2AllSingle(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx,  # pyre-ignore
        input: torch.Tensor,
        output_split_sizes: List[int],
        input_split_sizes: List[int],
        tag: str,
        ranks: List[int],
        group_size: int,
    ) -> torch.Tensor:
        ctx.input_size = input.size()
        ctx.output_split_sizes = input_split_sizes
        ctx.input_split_sizes = output_split_sizes
        ctx.tag = tag
        ctx.ranks = ranks
        ctx.group_size = group_size

        with torch._C._AutoDispatchBelowAutograd():
            ret = torch.ops.c10d_functional.all_to_all_single(
                input, output_split_sizes, input_split_sizes, tag, ranks, group_size
            )
        return ret

    @staticmethod
    def backward(ctx, grad_output):  # pyre-ignore
        return (
            torch.ops.c10d_functional.all_to_all_single(
                grad_output,
                ctx.output_split_sizes,
                ctx.input_split_sizes,
                ctx.tag,
                ctx.ranks,
                ctx.group_size,
            ),
            None,
            None,
            None,
            None,
            None,
        )


def _all_to_all_single_autograd(
    input: torch.Tensor,
    output_split_sizes: List[int],
    input_split_sizes: List[int],
    tag: str,
    ranks: List[int],
    group_size: int,
) -> torch.Tensor:
    return _All2AllSingle.apply(
        input, output_split_sizes, input_split_sizes, tag, ranks, group_size
    )


class _All2AllSingle_native_funcol(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx,  # pyre-ignore
        input: torch.Tensor,
        output_split_sizes: List[int],
        input_split_sizes: List[int],
        group_name: str,
    ) -> torch.Tensor:
        ctx.input_size = input.size()
        ctx.output_split_sizes = input_split_sizes
        ctx.input_split_sizes = output_split_sizes
        ctx.group_name = group_name

        with torch._C._AutoDispatchBelowAutograd():
            ret = torch.ops._c10d_functional.all_to_all_single(
                input, output_split_sizes, input_split_sizes, group_name
            )
        return ret

    @staticmethod
    def backward(ctx, grad_output):  # pyre-ignore
        return (
            torch.ops._c10d_functional.all_to_all_single(
                grad_output,
                ctx.output_split_sizes,
                ctx.input_split_sizes,
                ctx.group_name,
            ),
            None,
            None,
            None,
        )


def _all_to_all_single_autograd_native_funcol(
    input: torch.Tensor,
    output_split_sizes: List[int],
    input_split_sizes: List[int],
    group_name: str,
) -> torch.Tensor:
    return _All2AllSingle_native_funcol.apply(
        input, output_split_sizes, input_split_sizes, group_name
    )


class _ReduceScatterTensor(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx,  # pyre-ignore
        input: torch.Tensor,
        reduceOp: str,
        tag: str,
        ranks: List[int],
        group_size: int,
    ) -> torch.Tensor:
        input = input.contiguous()
        ctx.tag = tag
        ctx.ranks = ranks
        ctx.group_size = group_size
        with torch._C._AutoDispatchBelowAutograd():
            ret = torch.ops.c10d_functional.reduce_scatter_tensor(
                input,
                reduceOp,
                tag,
                ranks,
                group_size,
            )
        return ret

    @staticmethod
    def backward(ctx, grad_output):  # pyre-ignore
        grad_output = grad_output.contiguous()
        return (
            torch.ops.c10d_functional.all_gather_into_tensor(
                grad_output, ctx.tag, ctx.ranks, ctx.group_size
            ),
            None,
            None,
            None,
            None,
        )


# pyre-ignore
def _reduce_scatter_tensor_autograd(input, reduceOp, tag, ranks, group_size):
    return _ReduceScatterTensor.apply(input, reduceOp, tag, ranks, group_size)


class _ReduceScatterTensor_native_funcol(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx,  # pyre-ignore
        input: torch.Tensor,
        reduceOp: str,
        group_size: int,
        group_name: str,
    ) -> torch.Tensor:
        input = input.contiguous()
        ctx.group_size = group_size
        ctx.group_name = group_name
        with torch._C._AutoDispatchBelowAutograd():
            ret = torch.ops._c10d_functional.reduce_scatter_tensor(
                input,
                reduceOp,
                group_size,
                group_name,
            )
        return ret

    @staticmethod
    def backward(ctx, grad_output):  # pyre-ignore
        grad_output = grad_output.contiguous()
        return (
            torch.ops._c10d_functional.all_gather_into_tensor(
                grad_output, ctx.group_size, ctx.group_name
            ),
            None,
            None,
            None,
        )


# pyre-ignore
def _reduce_scatter_tensor_autograd_native_funcol(
    # pyre-ignore
    input,
    # pyre-ignore
    reduceOp,
    # pyre-ignore
    group_size,
    # pyre-ignore
    group_name,
):
    return _ReduceScatterTensor_native_funcol.apply(
        input, reduceOp, group_size, group_name
    )


class _AllGatherIntoTensor(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx,  # pyre-ignore
        shard: torch.Tensor,
        tag: str,
        ranks: List[int],
        group_size: int,
    ) -> torch.Tensor:
        shard = shard.contiguous()
        ctx.tag = tag
        ctx.ranks = ranks
        ctx.group_size = group_size
        with torch._C._AutoDispatchBelowAutograd():
            ret = torch.ops.c10d_functional.all_gather_into_tensor(
                shard, tag, ranks, group_size
            )
        return ret

    @staticmethod
    def backward(ctx, grad_output):  # pyre-ignore
        return (
            torch.ops.c10d_functional.reduce_scatter_tensor(
                grad_output, "sum", ctx.tag, ctx.ranks, ctx.group_size
            ),
            None,
            None,
            None,
        )


# pyre-ignore
def _all_gather_into_tensor_autograd(shard, tag, ranks, group_size):
    return _AllGatherIntoTensor.apply(shard, tag, ranks, group_size)


class _AllGatherIntoTensor_native_funcols(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx,  # pyre-ignore
        shard: torch.Tensor,
        group_size: int,
        group_name: str,
    ) -> torch.Tensor:
        shard = shard.contiguous()
        ctx.group_size = group_size
        ctx.group_name = group_name
        with torch._C._AutoDispatchBelowAutograd():
            ret = torch.ops._c10d_functional.all_gather_into_tensor(
                shard, group_size, group_name
            )
        return ret

    @staticmethod
    def backward(ctx, grad_output):  # pyre-ignore
        return (
            torch.ops._c10d_functional.reduce_scatter_tensor(
                grad_output, "sum", ctx.group_size, ctx.group_name
            ),
            None,
            None,
        )


# pyre-ignore
def _all_gather_into_tensor_autograd_native_funcol(shard, group_size, group_name):
    return _AllGatherIntoTensor_native_funcols.apply(shard, group_size, group_name)


class _Wait(torch.autograd.Function):
    @staticmethod
    # pyre-ignore
    def forward(
        ctx,  # pyre-ignore
        input: torch.Tensor,
    ) -> torch.Tensor:
        with torch._C._AutoDispatchBelowAutograd():
            ret = torch.ops.c10d_functional.wait_tensor(input)
        return ret

    @staticmethod
    def backward(ctx, grad_output):  # pyre-ignore
        return (grad_output,)


def _wait_autograd(input: torch.Tensor) -> torch.Tensor:
    return _Wait.apply(input)


# pyre-ignore
c10d_functional_autograd_ops = [
    ("all_to_all_single", _all_to_all_single_autograd),
    ("reduce_scatter_tensor", _reduce_scatter_tensor_autograd),
    ("all_gather_into_tensor", _all_gather_into_tensor_autograd),
    ("wait_tensor", _wait_autograd),
]

# pyre-ignore
c10d_functional_autograd_ops_native_funcols = [
    ("all_to_all_single", _all_to_all_single_autograd_native_funcol),
    ("reduce_scatter_tensor", _reduce_scatter_tensor_autograd_native_funcol),
    ("all_gather_into_tensor", _all_gather_into_tensor_autograd_native_funcol),
    ("wait_tensor", _wait_autograd),
]


if not torch._running_with_deploy():
    for ns, op_to_autograd_fn in zip(
        ["c10d_functional", "_c10d_functional"],
        [c10d_functional_autograd_ops, c10d_functional_autograd_ops_native_funcols],
    ):
        c10_lib_impl = torch.library.Library(ns, "IMPL")
        backend = "Autograd"
        for op_name, fn in op_to_autograd_fn:
            if not torch._C._dispatch_has_kernel_for_dispatch_key(
                f"{ns}::{op_name}", backend
            ):
                c10_lib_impl.impl(op_name, fn, backend)
