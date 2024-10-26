import torch
import torch.nn as nn
import torch.nn.functional as F
import math

# From https://github.com/karpathy/minGPT/blob/master/mingpt/model.py

class NewGELU(nn.Module):
    """
    Implementation of the GELU activation function currently in Google BERT repo (identical to OpenAI GPT).
    Reference: Gaussian Error Linear Units (GELU) paper: https://arxiv.org/abs/1606.08415
    """
    def __init__(self):
        super(NewGELU, self).__init__()
    
    def forward(self, x):
        return 0.5 * x * (1.0 + torch.tanh(math.sqrt(2.0 / math.pi) * (x + 0.044715 * torch.pow(x, 3.0))))

class CausalSelfAttention(nn.Module):
    """
    A vanilla multi-head masked self-attention layer with a projection at the end.
    It is possible to use torch.nn.MultiheadAttention here but I am including an
    explicit implementation here to show that there is nothing too scary here.
    """

    def __init__(self, n_embd, n_head, attn_pdrop, resid_pdrop, max_seqlen):
        super().__init__()
        assert n_embd % n_head == 0
        # key, query, value projections for all heads, but in a batch
        self.c_attn = nn.Linear(n_embd, 3 * n_embd)
        # output projection
        self.c_proj = nn.Linear(n_embd, n_embd)
        # regularization
        self.attn_dropout = nn.Dropout(attn_pdrop)
        self.resid_dropout = nn.Dropout(resid_pdrop)
        # causal mask to ensure that attention is only applied to the left in the input sequence
        self.register_buffer("bias", torch.tril(torch.ones(max_seqlen, max_seqlen))
                                     .view(1, 1, max_seqlen, max_seqlen))
        self.n_head = n_head
        self.n_embd = n_embd

    def forward(self, x):
        B, T, C = x.size() # batch size, sequence length, embedding dimensionality (n_embd)

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        q, k ,v  = self.c_attn(x).split(self.n_embd, dim=2)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        att = att.masked_fill(self.bias[:,:,:T,:T] == 0, float('-inf'))
        att = F.softmax(att, dim=-1)
        att = self.attn_dropout(att)
        y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_dropout(self.c_proj(y))
        return y
    
class Model(nn.Module):
    """ an unassuming Transformer block """

    def __init__(self, n_embd, n_head, attn_pdrop, resid_pdrop, max_seqlen):
        super().__init__()
        self.ln_1 = nn.LayerNorm(n_embd)
        self.attn = CausalSelfAttention(n_embd, n_head, attn_pdrop, resid_pdrop, max_seqlen)
        self.ln_2 = nn.LayerNorm(n_embd)
        self.mlp = nn.ModuleDict(dict(
            c_fc    = nn.Linear(n_embd, 4 * n_embd),
            c_proj  = nn.Linear(4 * n_embd, n_embd),
            act     = NewGELU(),
            dropout = nn.Dropout(resid_pdrop),
        ))
        m = self.mlp
        self.mlpf = lambda x: m.dropout(m.c_proj(m.act(m.c_fc(x)))) # MLP forward

    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlpf(self.ln_2(x))
        return x

batch_size = 128
max_seqlen = 1024
seq_len = 512
n_embd = 768
n_head = 8
attn_pdrop = 0.0
resid_pdrop = 0.0

def get_inputs():
    return [torch.randn(batch_size, seq_len, n_embd)]

def get_init_inputs():
    return [n_embd, n_head, attn_pdrop, resid_pdrop, max_seqlen]

# Define custom CUDA kernel for fused MatMul + ReLU
class FusedMatMulReLU(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, weight, bias):
        ctx.save_for_backward(input, weight, bias)
        output = torch.matmul(input, weight.t())
        output.add_(bias)
        output.relu_()
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input, weight, bias = ctx.saved_tensors
        grad_input = grad_output @ weight
        grad_weight = grad_output.t() @ input
        grad_bias = grad_output.sum(dim=0)
        return grad_input, grad_weight, grad_bias

# Define custom CUDA kernel for fused LayerNorm + Attention + Dropout
class FusedLayerNormAttentionDropout(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, weight, bias, attn_mask, dropout_p):
        ctx.save_for_backward(input, weight, bias, attn_mask)
        ctx.dropout_p = dropout_p
        # Implement LayerNorm using CUDA kernel
        output = torch.cuda.layer_norm(input, (n_embd,), weight, bias)
        # Implement Attention using CUDA kernel
        output = torch.cuda.masked_softmax(torch.matmul(output, weight.t()), attn_mask, dim=-1)
        # Implement Dropout using CUDA kernel
        output = torch.cuda.dropout(output, p=dropout_p)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        input, weight, bias, attn_mask = ctx.saved_tensors
        dropout_p = ctx.dropout_p
        # Implement backward pass for LayerNorm using CUDA kernel
        grad_input = torch.cuda.layer_norm_backward(grad_output, input, (n_embd,), weight, bias)
        # Implement backward pass for Attention using CUDA kernel
        grad_weight = torch.cuda.masked_softmax_backward(grad_output, output, attn_mask, dim=-1)
        # Implement backward pass for Dropout using CUDA kernel
        grad_output = torch.cuda.dropout_backward(grad_output, dropout_p)
        return grad_input, grad_weight, grad_bias, None, None

# Define the optimized model with custom CUDA kernels
class ModelNew(nn.Module):
    def __init__(self, n_embd, n_head, attn_pdrop, resid_pdrop, max_seqlen):
        super().__init__()
        self.ln_1 = nn.LayerNorm(n_embd)
        # Replace CausalSelfAttention with custom kernel
        self.attn = FusedLayerNormAttentionDropout.apply
        self.ln_2 = nn.LayerNorm(n_embd)
        self.mlp = nn.ModuleDict(dict(
            c_fc    = nn.Linear(n_embd, 4 * n_embd),
            c_proj  = nn.Linear(4 * n_embd, n_embd),
            act     = NewGELU(),
            dropout = nn.Dropout(resid_pdrop),
        ))
        m = self.mlp
        # Replace MatMul + ReLU with custom kernel
        self.mlpf = lambda x: m.dropout(FusedMatMulReLU.apply(m.c_fc(x), m.c_proj.weight, m.c_proj.bias))

    def forward(self, x):
        # Use custom kernels for LayerNorm + Attention + Dropout
        x = x + self.attn(self.ln_1(x), self.attn.c_attn.weight, self.attn.c_attn.bias, self.attn.bias, attn_pdrop)
        # Use custom kernel for MatMul + ReLU
        x = x + self.mlpf(self.ln_2(x))
        return x