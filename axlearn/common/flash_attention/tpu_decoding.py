# Copyright © 2025 Apple Inc.
"""Implements TPU decoding.

Unlike GPU, TPU blocks are sequential (except when there're two cores). Therefore, unlike GPU
decoding, there's no need to parallelize over the KV sequence length. As the result, it works
very similar to full attention. The grid dimensions are
(batch_size, num_kv_heads, num_kv_blocks).

The main reason to use the kernel is that it can take advantage of the fact that most KV blocks
are padding in practical decoding scenarios. Also, it can take advantage of sparsity in
`mask_fn`.

Performance note:
1. When kv_seq_len == padded_kv_seq_len:
    This kernels performs similarly to non-fused (i.e. XLA) attention, or within 10% slower.
2. When kv_seq_len < padded_kv_seq_len or `mask_fn` has sparsity:
    This kernel provides speed up roughly equal to padded_kv_seq_len / kv_seq_len or number
    of masked kv blocks / total kv blocks.

The main reason why non-fused attention is faster when kv are not padded is that the non-fused
matmuls can flatten the non-head dimensions, thus having larger non-contracting dimensions.
This leads to have better utilization of the matrix and memory units.
"""
from functools import partial
from typing import Optional

import jax
import jax.numpy as jnp
from absl import logging
from jax import lax
from jax.experimental import pallas as pl
from jax.experimental.pallas import tpu as pltpu

from axlearn.common.attention_bias import (
    NEG_INF,
    BaseAttentionBias,
    MaskFn,
    MaskFnAttentionBias,
    SlidingWindowAttentionBias,
    split,
)
from axlearn.common.flash_attention.common import (
    BaseSingleStepDecoding,
    build_mask,
    build_sliding_window_mask,
    get_tpu_dot_precision,
    query_iterator_indices,
)
from axlearn.common.kv_cache.base_kv_cache import BaseKVCache
from axlearn.common.kv_cache.kv_cache import KVCache
from axlearn.common.utils import Nested, Tensor


def _tpu_decoding_kernel(
    # Scalars.
    kv_seq_len_ref,
    kv_block_offset,
    kv_block_offset_size,
    # Inputs.
    q_ref,
    k_ref,
    v_ref,
    b_ref,
    # Outputs.
    o_ref,
    # Scatch.
    m_i,
    l_i,
    o_scratch,
    # Compile time args.
    softmax_scale: float,
    mask_fn: Optional[MaskFn],
):
    batch_index = pl.program_id(0)
    non_empty_kv_block_index = pl.program_id(2)
    _, block_k = k_ref.shape
    precision = get_tpu_dot_precision(q_ref.dtype)

    # o is the buffer where we accumulate the output on sram.
    # m_i and l_i (see FlashAttention paper) are updated during the k,v loop.
    @pl.when(non_empty_kv_block_index == 0)
    def init():
        m_i[...] = jnp.full_like(m_i, NEG_INF)
        l_i[...] = jnp.zeros_like(l_i)
        o_scratch[...] = jnp.zeros_like(o_scratch)

    # Note: on CPU interpret mode, pl.program_id() cannot appear in functions decorated by
    # pl.when.
    kv_offset = kv_block_offset[batch_index, non_empty_kv_block_index] * block_k
    kv_seq_len = kv_seq_len_ref[batch_index]
    num_non_empty_kv_blocks = kv_block_offset_size[batch_index]

    # Different batch may have different number of-non empty kv blocks.
    @pl.when(non_empty_kv_block_index < num_non_empty_kv_blocks)
    def compute():
        q = q_ref[...]
        k = k_ref[...].astype(q.dtype)
        qk = pl.dot(q, k, precision=precision)
        if softmax_scale != 1.0:
            qk *= softmax_scale
        if b_ref is not None:
            qk += b_ref[...]
            qk = jnp.maximum(qk, NEG_INF)
        # Note: Pallas TPU requires the use of lax.broadcasted_iota instead of jnp.arange as only
        # 2D range is supported.
        block_kv_indices = kv_offset + lax.broadcasted_iota(jnp.int32, qk.shape, 1)
        kv_mask = block_kv_indices < kv_seq_len
        if mask_fn is not None:
            kv_mask = kv_mask & mask_fn(kv_seq_len - 1, block_kv_indices)
        qk = jnp.where(kv_mask, qk, NEG_INF)

        m_prev = m_i[...]
        l_prev = l_i[...]
        o_prev = o_scratch[...]

        # We need to make sure each array has two dims, or we get TPU Mosaic lowering errors.
        m_curr = qk.max(axis=-1, keepdims=True)
        m_next = jnp.maximum(m_prev, m_curr)
        correction = jnp.exp(m_prev - m_next)
        l_prev_corr = correction * l_prev
        # Use m_next instead of m_curr to avoid a correction on l_curr.
        s_curr = jnp.exp(qk - m_next)
        l_curr = s_curr.sum(axis=-1, keepdims=True)
        l_next = l_prev_corr + l_curr
        o_prev_corr = correction * o_prev
        v = v_ref[...].astype(q.dtype)
        o_curr = pl.dot(s_curr.astype(v.dtype), v.T, precision=precision)

        o_next = o_prev_corr + o_curr

        m_i[...] = m_next
        l_i[...] = l_next
        o_scratch[...] = o_next

    @pl.when(non_empty_kv_block_index == num_non_empty_kv_blocks - 1)
    def final():
        # We keep an unscaled version of o during the scan over kv_seq_len. Scaling it
        # by the last l_i gives us the correct final output. See section 3.1.1 in the
        # FlashAttention-2 paper: https://arxiv.org/pdf/2307.08691.
        o_ref[...] = (o_scratch[...] / l_i[...]).astype(o_ref.dtype)


class TPUDecoding(BaseSingleStepDecoding):
    "Wraps the TPU decoding kernel."

    def is_supported(
        self,
        input_batch: Nested[Tensor | BaseAttentionBias],
        kv_cache_type: Optional[type[BaseKVCache]],
    ) -> bool:
        """See `BaseFlashAttention.is_supported`."""
        if not super().is_supported(input_batch=input_batch, kv_cache_type=kv_cache_type):
            return False

        if kv_cache_type != KVCache:
            return self._log_unsupported(f"{kv_cache_type=}")

        block_size = self.cfg.tpu_block_size
        key: Tensor = input_batch["key"]
        k_seq_len = key.shape[1]
        if k_seq_len % block_size != 0 and k_seq_len > block_size:
            return self._log_unsupported(f"{k_seq_len=} is not divisible by {block_size=}")
        return True

    @partial(jax.jit, static_argnames=["self"])
    def __call__(
        self,
        input_batch: Nested[Tensor | BaseAttentionBias],
    ) -> Tensor:
        """See `BaseFlashAttention.__call__`."""
        bias: BaseAttentionBias = input_batch["bias"]
        mask, explicit_bias = split(bias, MaskFnAttentionBias)
        if mask is None or mask.target_positions is None:
            raise ValueError("Cannot retrieve MaskFnAttentionBias or target_positions.")
        mask_fn = mask.mask
        kv_seq_len = mask.target_positions[:, -1] + 1
        logging.info("Using mask_fn=%s for Decoding.", mask_fn)

        bias = explicit_bias.value()
        if bias is not None:
            logging.info(
                "Using explicit_bias=%s for Decoding. "
                "This is not expected unless an explicit Tensor bias is used.",
                bias,
            )

        # Pallas TPU doesn't support pl.load(..., mask=xxx), so we kv len must divide block size.
        # However, we can reduce the block size to support the case where
        # padded_kv_seq_len < block_size.
        query: Tensor = input_batch["query"]
        key: Tensor = input_batch["key"]
        value: Tensor = input_batch["value"]
        block_size = min(self.cfg.tpu_block_size, key.shape[1])
        orig_q_shape = query.shape
        q_seq_len = query.shape[1]
        block_kv = block_size

        q = query.squeeze(1)
        # Convert to bnhs which is the native shape of KV in the kv cache. These two transposes
        # should be elided by the compiler. See `BaseQKVLinear.init_states` from attention.py.
        k = jnp.einsum("bsnh->bnhs", key)
        v = jnp.einsum("bsnh->bnhs", value)
        bs, kv_heads, head_dim, padded_kv_seq_len = k.shape
        kv_seq_len = jnp.broadcast_to(jnp.asarray(kv_seq_len), (bs,))
        # Computes a full block map num_kv_blocks * num_kv_blocks.
        # Use a padding to ensure padding blocks aren't counted towards `kv_block_offset_size`.
        padding = -1
        with jax.ensure_compile_time_eval():
            if mask_fn is not None:
                mask_args = dict(
                    q_seq_len=padded_kv_seq_len,
                    kv_seq_len=padded_kv_seq_len,
                    block_q=block_size,
                    block_k=block_size,
                )
                if isinstance(mask, SlidingWindowAttentionBias):
                    bool_mask = build_sliding_window_mask(
                        **mask_args, sliding_window_size=mask.sliding_window_size
                    )
                else:
                    bool_mask = build_mask(mask_fn, **mask_args)
                offset, _ = query_iterator_indices(bool_mask, padding=padding)
            else:
                padded_num_kv_blocks = pl.cdiv(padded_kv_seq_len, block_size)
                offset = lax.broadcasted_iota(
                    jnp.int32, (padded_num_kv_blocks, padded_num_kv_blocks), 1
                )

        # Dynamically slice the rows according to the query position (which is kv_seq_len - 1).
        kv_block_offset = offset[(kv_seq_len - 1) // block_size]
        # Count the number of blocks with position < kv_seq_len.
        kv_block_offset_size = jnp.count_nonzero(
            (kv_block_offset != padding) & (kv_block_offset * block_size < kv_seq_len[:, None]),
            axis=1,
        )
        # Replace padding with the last valid kv block's index. See
        # https://docs.jax.dev/en/latest/pallas/tpu/sparse.html#sparse-access-patterns-on-dense-data
        kv_block_offset = jnp.where(
            kv_block_offset == padding, kv_block_offset.max(axis=1, keepdims=True), kv_block_offset
        )

        q = q.reshape(bs, kv_heads, -1, head_dim)
        q_seq_head = q.shape[-2]  # = q_seq_len * num_q_heads_per_kv_head
        assert q_seq_head <= 512

        def kv_index_map(
            batch_idx, head_idx, kv_block_idx, kv_seq_len, kv_block_offset, kv_block_offset_size
        ):
            del kv_seq_len, kv_block_offset_size
            return (batch_idx, head_idx, 0, kv_block_offset[batch_idx, kv_block_idx])

        q_spec = pl.BlockSpec(
            (None, None, q_seq_head, head_dim), lambda b, h, j, *args: (b, h, 0, 0)
        )
        kv_spec = pl.BlockSpec((None, None, head_dim, block_kv), kv_index_map)
        bias_spec = None
        if bias is not None:
            if bias.shape[0] == 1 and bias.shape[1] == 1:

                def bias_index_map(
                    batch_idx,
                    head_idx,
                    kv_block_idx,
                    kv_seq_len,
                    kv_block_offset,
                    kv_block_offset_size,
                ):
                    del head_idx, kv_seq_len, kv_block_offset_size
                    return (0, 0, 0, kv_block_offset[batch_idx, kv_block_idx])

                bias_spec = pl.BlockSpec((None, None, q_seq_len, block_kv), bias_index_map)
            else:
                bias = bias.reshape(bs, kv_heads, q_seq_head, padded_kv_seq_len)
                bias_spec = pl.BlockSpec((None, None, q_seq_head, block_kv), kv_index_map)

        out: Tensor = pl.pallas_call(
            partial(_tpu_decoding_kernel, softmax_scale=self.cfg.softmax_scale, mask_fn=mask_fn),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=3,
                in_specs=[
                    q_spec,
                    kv_spec,
                    kv_spec,
                    bias_spec,
                ],
                out_specs=q_spec,
                scratch_shapes=[
                    # VMEM requires 2D arrays.
                    pltpu.VMEM((q_seq_head, 1), jnp.float32),
                    pltpu.VMEM((q_seq_head, 1), jnp.float32),
                    pltpu.VMEM((q_seq_head, head_dim), jnp.float32),
                ],
                grid=(bs, kv_heads, kv_block_offset_size.max()),
            ),
            out_shape=jax.ShapeDtypeStruct(q.shape, q.dtype),
            compiler_params=pltpu.TPUCompilerParams(
                dimension_semantics=("parallel", "parallel", "arbitrary")
            ),
            interpret=self.cfg.interpret,
        )(kv_seq_len, kv_block_offset, kv_block_offset_size, q, k, v, bias)
        return out.reshape(orig_q_shape)
