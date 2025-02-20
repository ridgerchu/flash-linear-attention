# -*- coding: utf-8 -*-

from typing import Optional

import torch
from einops import rearrange, repeat


@torch.compile
def naive_nsa(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    indices: torch.LongTensor,
    block_size: int = 64,
    scale: Optional[float] = None,
    head_first: bool = False,
    cu_seqlens: Optional[torch.LongTensor] = None
) -> torch.Tensor:
    r"""
    Args:
        q (torch.Tensor):
            queries of shape `[B, HQ, T, K]` if `head_first=True` else `[B, T, HQ, K]`.
        k (torch.Tensor):
            keys of shape `[B, H, T, K]` if `head_first=True` else `[B, T, H, K]`.
            GQA is enforced here. The ratio of query heads (HQ) to key/value heads (H) must be a power of 2 and >=8.
        v (torch.Tensor):
            values of shape `[B, H, T, V]` if `head_first=True` else `[B, T, H, V]`.
        indices (torch.LongTensor):
            Block indices of shape `[B, T, H, S]` if `head_first=True` else `[B, T, H, S]`.
            `S` is the number of selected blocks for each query token, which is set to 16 in the paper.
        block_size (int):
            Selected block size. Default: 64.
        scale (Optional[int]):
            Scale factor for attention scores.
            If not provided, it will default to `1 / sqrt(K)`. Default: `None`.
        head_first (Optional[bool]):
            Whether the inputs are in the head-first format. Default: `False`.
        cu_seqlens (torch.LongTensor):
            Cumulative sequence lengths of shape `[N+1]` used for variable-length training,
            consistent with the FlashAttention API.

    Returns:
        o (torch.Tensor):
            Outputs of shape `[B, HQ, T, V]` if `head_first=True` else `[B, T, HQ, V]`.
    """
    if scale is None:
        scale = k.shape[-1] ** -0.5
    if cu_seqlens is not None:
        if head_first:
            raise RuntimeError("Sequences with variable lengths are not supported for head-first mode")
    if head_first:
        q, k, v, indices = map(lambda x: rearrange(x, 'b h t d -> b t h d'), (q, k, v, indices))

    dtype = q.dtype
    num_kv_groups = q.shape[2] // k.shape[2]
    k, v, indices = (repeat(x, 'b t h d -> b t (h g) d', g=num_kv_groups) for x in (k, v, indices))
    q, k, v = map(lambda x: x.float(), (q, k, v))

    o = torch.zeros_like(v)
    varlen = True
    if cu_seqlens is None:
        varlen = False
        B, T = q.shape[:2]
        cu_seqlens = torch.cat([indices.new_tensor(range(0, B*T, T)), indices.new_tensor([B*T])])

    for i in range(len(cu_seqlens) - 1):
        if not varlen:
            q_b, k_b, v_b, i_b = q[i], k[i], v[i], indices[i]
        else:
            T = cu_seqlens[i+1] - cu_seqlens[i]
            q_b, k_b, v_b, i_b = map(lambda x: x[cu_seqlens[i]:cu_seqlens[i+1]], (q, k, v, indices))

        i_b = i_b.unsqueeze(-1) * block_size + i_b.new_tensor(range(block_size))
        # [T, S*block_size, HQ]
        i_b = i_b.view(T, indices.shape[2], -1).transpose(1, 2)
        for i_q in range(T):
            # [HQ, D]
            q_i = q_b[i_q] * scale
            # [S*block_size, HQ]
            i_i = i_b[i_q]
            # [S*block_size, HQ, -1]
            k_i, v_i = map(lambda x: x.gather(0, i_i.unsqueeze(-1)), (k_b, v_b))
            # [S*block_size, HQ]
            attn = torch.einsum('h d, n h d -> n h', q_i, k_i).masked_fill(i_i > i_q, float('-inf')).softmax(0)
            if not varlen:
                o[i, i_q] = torch.einsum('n h, n h v -> h v', attn, v_i)
            else:
                o[cu_seqlens[i]+i_q] = torch.einsum('n h, n h v -> h v', attn, v_i)

    if head_first:
        o = rearrange(o, 'b t h d -> b h t d')
    o = o.to(dtype)
    return o
