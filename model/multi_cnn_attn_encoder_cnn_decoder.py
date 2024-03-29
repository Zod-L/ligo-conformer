import torch
import torch.nn as nn
import torch.nn.functional as F
from functools import partial
import random
from .cnn import ConvTransBlock as CNNBlock
from .cnn import ConvBlock as CNN
from timm.models.layers import DropPath, trunc_normal_

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # make torchscript happy (cannot use tensor as tuple)
        

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x
    

# class Attention_Sep(nn.Module):
#     def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., num_branch=4):
#         super().__init__()
#         self.num_heads = num_heads
#         head_dim = dim // num_heads
#         # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
#         self.scale = qk_scale or head_dim ** -0.5
#         self.num_branch = num_branch
#         self.attn_drop = attn_drop

#         self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
#         self.proj = nn.Linear(dim, dim)
#         self.proj_drop = nn.Dropout(proj_drop)

#     def forward(self, x):
#         B, N, C = x.shape
#         spb = N // self.num_branch
#         qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
#         q, k, v = qkv[0], qkv[1], qkv[2]  # [B, num_head, seq_len, dim]



#         # CLS collect information 
#         cls = x[:, 0:1, :]
#         cls = F.scaled_dot_product_attention(q[:, :, 0:1, :], k, v, scale=self.scale, dropout_p=self.attn_drop).reshape(B, 1, C) + cls
#         qkv_cls = self.qkv(cls).reshape(B, 1, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
#         q_cls, k_cls, v_cls = qkv_cls[0], qkv_cls[1], qkv_cls[2]  # [B, num_head, 1, dim]

        

#         xs = []
#         for i in range(self.num_branch):
#             xs.append(F.scaled_dot_product_attention(torch.cat((q_cls, q[:, :, 1+i*spb:1+i*spb+spb, :]), 2), 
#                                                      torch.cat((k_cls, k[:, :, 1+i*spb:1+i*spb+spb, :]), 2), 
#                                                      torch.cat((v_cls, v[:, :, 1+i*spb:1+i*spb+spb, :]), 2), 
#                                                      scale=self.scale, dropout_p=self.attn_drop)[:, :, 1:, :].reshape(B, (N-1) // self.num_branch, C))




#         x = torch.cat([cls] + xs, 1)
#         x = self.proj(x)
#         x = self.proj_drop(x)
#         return x




class Attention_Sep(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., num_branch=4):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5
        self.num_branch = num_branch
        self.attn_drop = attn_drop

        for i in range(num_branch):
            setattr(self, f"qkv_{i}", nn.Linear(dim, dim * 3, bias=qkv_bias))
            setattr(self, f"proj_{i}", nn.Linear(dim, dim))
        
        self.fuse_q = nn.Linear(dim, dim, bias=qkv_bias)
        self.fuse_kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.cls_proj = nn.Linear(dim, dim)

        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        spb = (N-1) // self.num_branch


        cls = x[:, 0:1, :]
        q = self.fuse_q(cls).reshape(B, 1, 1, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)[0]
        kv = self.fuse_kv(x).reshape(B, N, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        k, v = kv[0], kv[1]  # [B, num_head, seq_len, dim]
        # CLS collect information 
        cls = F.scaled_dot_product_attention(q, k, v, scale=self.scale, dropout_p=self.attn_drop).reshape(B, 1, C) + cls


        

        xs = []
        for i in range(self.num_branch):
            qkv = getattr(self, f"qkv_{i}")(torch.cat((cls, x[:, 1+i*spb:1+i*spb+spb, :]), 1)).reshape(B, spb+1, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            q, k, v = qkv[0], qkv[1], qkv[2]  # [B, num_head, seq_len, dim]

            _x = F.scaled_dot_product_attention(q, k, v, 
                                                     scale=self.scale, dropout_p=self.attn_drop)[:, :, 1:, :].reshape(B, (N-1) // self.num_branch, C)
            
            xs.append(getattr(self, f"proj_{i}")(_x))

        cls = self.cls_proj(cls)


        x = torch.cat([cls] + xs, 1)
        x = self.proj_drop(x)
        return x


    

class split_layer_norm(nn.Module):
    def __init__(self, dim, num_branch=4):
        self.num_branch = num_branch
        for i in range(num_branch):
            setattr(self, f"norm_{i}", nn.LayerNorm(dim, eps=1e-6))
        self.norm_cls = nn.LayerNorm(dim, eps=1e-6)
        
    def forward(self, x):
        B, N, C = x.shape
        spb = (N-1) // self.num_branch
        xs = []

        for i in range(self.num_branch):
            xs.append(getattr(self, f"norm_{i}")(x[:, 1+spb*i:1+spb*i+spb, :]))

        return torch.cat((self.norm_cls(x[:, 0:1, :]), xs), 1)



class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=partial(nn.LayerNorm, eps=1e-6), num_branch=4):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention_Sep(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, num_branch=num_branch)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x):
        x = x + self.drop_path(self.attn(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x



class ConvBlockSingle(nn.Module):

    def __init__(self, inplanes, outplanes, stride=1, res_conv=False, act_layer=nn.LeakyReLU, groups=1,
                 norm_layer=partial(nn.BatchNorm2d, eps=1e-6), drop_block=None, drop_path=None):
        super(ConvBlockSingle, self).__init__()

        expansion = 4
        med_planes = outplanes // expansion if outplanes > expansion else outplanes

        self.conv1 = nn.Conv2d(inplanes, med_planes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn1 = norm_layer(med_planes)
        self.act1 = act_layer()

        self.conv2 = nn.Conv2d(med_planes, med_planes, kernel_size=3, stride=stride, groups=groups, padding=1, bias=False)
        self.bn2 = norm_layer(med_planes)
        self.act2 = act_layer()

        self.conv3 = nn.Conv2d(med_planes, outplanes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn3 = norm_layer(outplanes)
        self.act3 = act_layer()

        if res_conv:
            self.residual_conv = nn.Conv2d(inplanes, outplanes, kernel_size=1, stride=stride, padding=0, bias=False)
            self.residual_bn = norm_layer(outplanes)

        self.res_conv = res_conv
        self.drop_block = drop_block
        self.drop_path = drop_path

    def zero_init_last_bn(self):
        nn.init.zeros_(self.bn3.weight)

    def forward(self, x, x_t=None, return_x_2=True):
        residual = x

        x = self.conv1(x)
        x = self.bn1(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x = self.act1(x)

        x = self.conv2(x) if x_t is None else self.conv2(x + x_t)
        x = self.bn2(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x2 = self.act2(x)

        x = self.conv3(x2)
        x = self.bn3(x)
        if self.drop_block is not None:
            x = self.drop_block(x)

        if self.drop_path is not None:
            x = self.drop_path(x)

        if self.res_conv:
            residual = self.residual_conv(residual)
            residual = self.residual_bn(residual)

        x += residual
        x = self.act3(x)

        if return_x_2:
            return x, x2
        else:
            return x


class ConvBlock(nn.Module):

    def __init__(self, inplanes, outplanes, stride=1, res_conv=False, act_layer=nn.LeakyReLU, groups=1,
                 norm_layer=partial(nn.BatchNorm2d, eps=1e-6), drop_block=None, drop_path=None, num_branch=4):
        super(ConvBlock, self).__init__()
        self.num_branch = num_branch
        for i in range(num_branch):
            setattr(self, f"conv_{i}", ConvBlockSingle(inplanes, outplanes, stride, res_conv, act_layer, groups,
                 norm_layer, drop_block, drop_path))

    def zero_init_last_bn(self):
        for i in range(self.num_branch):
            nn.init.zeros_(getattr(getattr(self, f"conv_{i}"), "bn3")).weight

    def forward(self, x, x_t=None, return_x_2=True):
        xs = []
        x2s = []
        cpb = x.shape[1] // self.num_branch
        spb = x_t.shape[1] // self.num_branch if x_t is not None else 0
        for i in range(self.num_branch):
            _x, _x2 = getattr(self, f"conv_{i}")(x[:, cpb*i : cpb*i+cpb, :, :], x_t[:, spb*i : spb*i+spb, :] if x_t is not None else x_t, True)
            xs.append(_x)
            x2s.append(_x2)
        if return_x_2: 
            return torch.cat(xs, 1), torch.cat(x2s, 1)  
        else:
            return torch.cat(xs, 1)



class ConvBlockDecodeSingle(nn.Module):

    def __init__(self, inplanes, outplanes, stride=1, res_conv=False, act_layer=nn.LeakyReLU, groups=1,
                 norm_layer=partial(nn.BatchNorm2d, eps=1e-6), drop_block=None, drop_path=None):
        super(ConvBlockDecodeSingle, self).__init__()

        expansion = 4
        med_planes = outplanes // expansion if outplanes > expansion else outplanes

        self.conv1 = nn.ConvTranspose2d(inplanes, med_planes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn1 = norm_layer(med_planes)
        self.act1 = act_layer()

        self.conv2 = nn.ConvTranspose2d(med_planes, med_planes, kernel_size=3, stride=stride, output_padding=stride-1, groups=groups, padding=1, bias=False)
        self.bn2 = norm_layer(med_planes)
        self.act2 = act_layer()

        self.conv3 = nn.ConvTranspose2d(med_planes, outplanes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn3 = norm_layer(outplanes)
        self.act3 = act_layer()

        if res_conv:
            self.residual_conv = nn.ConvTranspose2d(inplanes, outplanes, kernel_size=1, stride=stride, output_padding=stride-1, padding=0, bias=False)
            self.residual_bn = norm_layer(outplanes)

        self.res_conv = res_conv
        self.drop_block = drop_block
        self.drop_path = drop_path

    def zero_init_last_bn(self):
        nn.init.zeros_(self.bn3.weight)

    def forward(self, x, x_t=None, return_x_2=True):
        residual = x

        x = self.conv1(x)
        x = self.bn1(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x = self.act1(x)

        x = self.conv2(x) if x_t is None else self.conv2(x + x_t)
        x = self.bn2(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x2 = self.act2(x)

        x = self.conv3(x2)
        x = self.bn3(x)
        if self.drop_block is not None:
            x = self.drop_block(x)

        if self.drop_path is not None:
            x = self.drop_path(x)

        if self.res_conv:
            residual = self.residual_conv(residual)
            residual = self.residual_bn(residual)

        x += residual
        x = self.act3(x)

        if return_x_2:
            return x, x2
        else:
            return x



class ConvBlockDecode(nn.Module):

    def __init__(self, inplanes, outplanes, stride=1, res_conv=False, act_layer=nn.LeakyReLU, groups=1,
                 norm_layer=partial(nn.BatchNorm2d, eps=1e-6), drop_block=None, drop_path=None, num_branch=4):
        super(ConvBlockDecode, self).__init__()
        self.num_branch = num_branch
        for i in range(num_branch):
            setattr(self, f"conv_{i}", ConvBlockDecodeSingle(inplanes, outplanes, stride, res_conv, act_layer, groups,
                 norm_layer, drop_block, drop_path))

    def zero_init_last_bn(self):
        for i in range(self.num_branch):
            nn.init.zeros_(getattr(getattr(self, f"conv_{i}"), "bn3")).weight

    def forward(self, x, x_t=None, return_x_2=True):    
        xs = []
        x2s = []
        cpb = x.shape[1] // self.num_branch
        spb = x_t.shape[1] // self.num_branch if x_t is not None else 0
        for i in range(self.num_branch):
            _x, _x2 = getattr(self, f"conv_{i}")(x[:, cpb*i : cpb*i+cpb, :, :], x_t[:, spb*i : spb*i+spb, :] if x_t is not None else x_t, True)
            xs.append(_x)
            x2s.append(_x2)
        if return_x_2: 
            return torch.cat(xs, 1), torch.cat(x2s, 1)  
        else:
            return torch.cat(xs, 1)


class FCUDown(nn.Module):
    """ CNN feature maps -> Transformer patch embeddings
    """

    def __init__(self, inplanes, outplanes, dw_stride, act_layer=nn.GELU,
                 norm_layer=partial(nn.LayerNorm, eps=1e-6), num_branch=4):
        super(FCUDown, self).__init__()
        self.dw_stride = dw_stride
        self.num_branch = num_branch
 
        for i in range(num_branch):
            setattr(self, f"conv_project_{i}", nn.Conv2d(inplanes, outplanes, kernel_size=1, stride=1, padding=0))
            setattr(self, f"ln_{i}", norm_layer(outplanes))


        self.sample_pooling = nn.AvgPool2d(kernel_size=dw_stride, stride=dw_stride)

        self.act = act_layer()

    def forward(self, x, x_t):
        xs = []
        cpb = x.shape[1] // self.num_branch
        for i in range(self.num_branch):
            _x = getattr(self, f"conv_project_{i}")(x[:, i*cpb : i*cpb + cpb, :, :])
            _x = self.sample_pooling(_x).flatten(2).transpose(1, 2)
            _x = getattr(self, f"ln_{i}")(_x)
            xs.append(_x)
        x = torch.cat(xs, 1)


        x = self.act(x)

        x = torch.cat([x_t[:, 0:1], x], dim=1)

        return x


class FCUUp(nn.Module):
    """ Transformer patch embeddings -> CNN feature maps
    """

    def __init__(self, inplanes, outplanes, up_stride, act_layer=nn.LeakyReLU,
                 norm_layer=partial(nn.BatchNorm2d, eps=1e-6), num_branch=4):
        super(FCUUp, self).__init__()

        self.up_stride = up_stride
        self.num_branch = num_branch
        for i in range(num_branch):
            setattr(self, f"conv_project_{i}", nn.Conv2d(inplanes, outplanes, kernel_size=1, stride=1, padding=0))
            setattr(self, f"bn_{i}", norm_layer(outplanes))


        self.act = act_layer()

    def forward(self, x, H, W):
        B, _, C = x.shape

        x_r = x[:, 1:]
        xrs = []
        cpb = x_r.shape[1] // self.num_branch
        for i in range(self.num_branch):
            _x_r = x_r[:, i*cpb : i*cpb + cpb, :].transpose(1, 2).reshape(B, C, H, W)
            _x_r = getattr(self, f"conv_project_{i}")(_x_r)
            _x_r = self.act(getattr(self, f"bn_{i}")(_x_r))
            _x_r = F.interpolate(_x_r, size=(H * self.up_stride, W * self.up_stride))
            xrs.append(_x_r)
        _x_r = torch.cat(xrs, 1)



        return _x_r


class Med_ConvBlockSingle(nn.Module):
    """ special case for Convblock with down sampling,
    """
    def __init__(self, inplanes, act_layer=nn.LeakyReLU, groups=1, norm_layer=partial(nn.BatchNorm2d, eps=1e-6),
                 drop_block=None, drop_path=None):

        super(Med_ConvBlockSingle, self).__init__()

        expansion = 4
        med_planes = inplanes // expansion

        self.conv1 = nn.Conv2d(inplanes, med_planes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn1 = norm_layer(med_planes)
        self.act1 = act_layer()

        self.conv2 = nn.Conv2d(med_planes, med_planes, kernel_size=3, stride=1, groups=groups, padding=1, bias=False)
        self.bn2 = norm_layer(med_planes)
        self.act2 = act_layer()

        self.conv3 = nn.Conv2d(med_planes, inplanes, kernel_size=1, stride=1, padding=0, bias=False)
        self.bn3 = norm_layer(inplanes)
        self.act3 = act_layer()

        self.drop_block = drop_block
        self.drop_path = drop_path

    def zero_init_last_bn(self):
        nn.init.zeros_(self.bn3.weight)

    def forward(self, x):
        residual = x

        x = self.conv1(x)
        x = self.bn1(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x = self.act1(x)

        x = self.conv2(x)
        x = self.bn2(x)
        if self.drop_block is not None:
            x = self.drop_block(x)
        x = self.act2(x)

        x = self.conv3(x)
        x = self.bn3(x)
        if self.drop_block is not None:
            x = self.drop_block(x)

        if self.drop_path is not None:
            x = self.drop_path(x)

        x += residual
        x = self.act3(x)

        return x



class Med_ConvBlock(nn.Module):

    def __init__(self, inplanes, act_layer=nn.LeakyReLU, groups=1, norm_layer=partial(nn.BatchNorm2d, eps=1e-6),
                 drop_block=None, drop_path=None, num_branch=4):
        super(ConvBlockDecode, self).__init__()
        self.num_branch = num_branch
        for i in range(num_branch):
            setattr(self, f"conv_{i}", Med_ConvBlockSingle(inplanes, act_layer, groups,
                 norm_layer, drop_block, drop_path))

    def zero_init_last_bn(self):
        for i in range(self.num_branch):
            nn.init.zeros_(getattr(getattr(self, f"conv_{i}"), "bn3")).weight

    def forward(self, x):
        xs = []
        cpb = x.shape[1] // self.num_branch
        for i in range(self.num_branch):
            _x = getattr(self, f"conv_{i}")(x[:, cpb*i : cpb*i+cpb, :, :])
            xs.append(_x)
        return torch.cat(xs, 1)


class ConvTransBlock(nn.Module):
    """
    Basic module for ConvTransformer, keep feature maps for CNN block and patch embeddings for transformer encoder block
    """

    def __init__(self, inplanes, outplanes, res_conv, stride, dw_stride, embed_dim, num_heads=12, mlp_ratio=4.,
                 qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
                 last_fusion=False, num_med_block=0, groups=1, decode=False, num_branch=4):

        super(ConvTransBlock, self).__init__()
        expansion = 4
        if decode:
            self.cnn_block = ConvBlockDecode(inplanes=inplanes, outplanes=outplanes, res_conv=res_conv, stride=stride, groups=groups)
        else:
            self.cnn_block = ConvBlock(inplanes=inplanes, outplanes=outplanes, res_conv=res_conv, stride=stride, groups=groups)

        if decode:
            if last_fusion:
                self.fusion_block = ConvBlockDecode(inplanes=outplanes, outplanes=outplanes, stride=2, res_conv=True, groups=groups)
            else:
                self.fusion_block = ConvBlockDecode(inplanes=outplanes, outplanes=outplanes, groups=groups)
        else:
            if last_fusion:
                self.fusion_block = ConvBlock(inplanes=outplanes, outplanes=outplanes, stride=2, res_conv=True, groups=groups)
            else:
                self.fusion_block = ConvBlock(inplanes=outplanes, outplanes=outplanes, groups=groups)

        if num_med_block > 0:
            self.med_block = []
            for i in range(num_med_block):
                self.med_block.append(Med_ConvBlock(inplanes=outplanes, groups=groups))
            self.med_block = nn.ModuleList(self.med_block)

        self.squeeze_block = FCUDown(inplanes=outplanes // expansion, outplanes=embed_dim, dw_stride=dw_stride)

        self.expand_block = FCUUp(inplanes=embed_dim, outplanes=outplanes // expansion, up_stride=dw_stride)

        self.trans_block = Block(
            dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
            drop=drop_rate, attn_drop=attn_drop_rate, drop_path=drop_path_rate, num_branch=num_branch)

        self.dw_stride = dw_stride
        self.embed_dim = embed_dim
        self.num_med_block = num_med_block
        self.last_fusion = last_fusion

    def forward(self, x, x_t):
        x, x2 = self.cnn_block(x)


        _, _, H, W = x2.shape
        x_st = self.squeeze_block(x2, x_t)
        x_t = self.trans_block(x_st + x_t)

        if self.num_med_block > 0:
            for m in self.med_block:
                x = m(x)

        x_t_r = self.expand_block(x_t, H // self.dw_stride, W // self.dw_stride)
        x = self.fusion_block(x, x_t_r, return_x_2=False)

        return x, x_t
















class encoder(nn.Module):

    def __init__(self, patch_size=16, in_chans=3, decode_embed=1000, base_channel=64, channel_ratio=4, num_med_block=0,
                 embed_dim=768, depth=12, num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0., im_size=224, num_branch=4, use_vae=True):

        # Transformer
        super().__init__()
        self.decode_embed = decode_embed
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        assert depth % 3 == 0

        num_patches = ((im_size // patch_size) ** 2) * 4
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))
        self.trans_dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule

        # Latent output
        self.last_pool = 4
        self.trans_norm = nn.LayerNorm(embed_dim)
        self.trans_cls_head = nn.Linear(embed_dim, decode_embed)
        self.pooling = nn.AdaptiveAvgPool2d(self.last_pool)
        for i in range(num_branch):
            setattr(self, f"conv_head{i}", nn.Linear(int(num_branch * base_channel * channel_ratio * self.last_pool * self.last_pool), decode_embed))
        
        self.conv_head = nn.Linear(4 * decode_embed, decode_embed)
        self.mean_head = nn.Linear(2 * decode_embed, decode_embed)
        self.var_head = nn.Linear(2 * decode_embed, decode_embed) if use_vae else None

        # Stem stage: get the feature maps by conv block (copied form resnet.py)
        self.num_branch = num_branch
        for i in range(num_branch):
            setattr(self, f"conv1_{i}", nn.Conv2d(in_chans, 64, kernel_size=7, stride=2, padding=3, bias=False))
            setattr(self, f"bn1_{i}", nn.BatchNorm2d(64))


        self.act1 = nn.LeakyReLU()
        # self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)  # 1 / 4 [56, 56]

        # 1 stage
        stage_1_channel = int(base_channel * channel_ratio)
        trans_dw_stride = patch_size // 2
        self.conv_1 = ConvBlock(inplanes=64, outplanes=stage_1_channel, res_conv=True, stride=1)
        for i in range(num_branch):
            setattr(self, f"trans_patch_conv_{i}", nn.Conv2d(64, embed_dim, kernel_size=trans_dw_stride, stride=trans_dw_stride, padding=0))
 
  
        
        self.trans_1 = Block(dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias,
                             qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=self.trans_dpr[0], num_branch=num_branch
                             )

        # 2~4 stage
        init_stage = 2
        fin_stage = depth // 3 + 1
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            res_conv = True if i == init_stage else False
            self.add_module('conv_trans_' + str(i),
                    ConvTransBlock(
                        stage_1_channel, stage_1_channel, res_conv, s, dw_stride=trans_dw_stride // 2, embed_dim=embed_dim,
                        num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=self.trans_dpr[i-1],
                        num_med_block=num_med_block, num_branch=num_branch
                    )
            )


        stage_2_channel = int(base_channel * channel_ratio * 2)
        # 5~8 stage
        init_stage = fin_stage # 5
        fin_stage = fin_stage + depth // 3 # 9
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            in_channel = stage_1_channel if i == init_stage else stage_2_channel
            res_conv = True if i == init_stage else False
            self.add_module('conv_trans_' + str(i),
                    ConvTransBlock(
                        in_channel, stage_2_channel, res_conv, s, dw_stride=trans_dw_stride // 4, embed_dim=embed_dim,
                        num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=self.trans_dpr[i-1],
                        num_med_block=num_med_block, num_branch=num_branch
                    )
            )

        stage_3_channel = int(base_channel * channel_ratio * 2 * 2)
        # 9~12 stage
        init_stage = fin_stage  # 9
        fin_stage = fin_stage + depth // 3  # 13
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            in_channel = stage_2_channel if i == init_stage else stage_3_channel
            res_conv = True if i == init_stage else False
            last_fusion = True if i == depth else False
            self.add_module('conv_trans_' + str(i),
                    ConvTransBlock(
                        in_channel, stage_3_channel, res_conv, s, dw_stride=trans_dw_stride // 8, embed_dim=embed_dim,
                        num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=self.trans_dpr[i-1],
                        num_med_block=num_med_block, last_fusion=last_fusion, num_branch=num_branch
                    )
            )
        self.fin_stage = fin_stage

        trunc_normal_(self.pos_embed, std=.02)
        trunc_normal_(self.cls_token, std=.02)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        elif isinstance(m, nn.BatchNorm2d):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)
        elif isinstance(m, nn.GroupNorm):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'cls_token'}


    def forward(self, x):
        B = x.shape[0]
        cls_tokens = self.cls_token.expand(B, -1, -1)
        pos_embed = self.pos_embed.repeat(B, 1, 1)
        

        # pdb.set_trace()
        # stem stage [N, 3, 224, 224] -> [N, 64, 56, 56]
        x_base = []
        cpb = x.shape[1] // self.num_branch
        for i in range(self.num_branch):
            _x = getattr(self, f"conv1_{i}")(x[:, i*cpb : i*cpb + cpb, :, :])
            _x = getattr(self, f"bn1_{i}")(_x)
            x_base.append(self.act1(_x))
        x_base = torch.cat(x_base, 1)

        # 1 stage
        x = self.conv_1(x_base, return_x_2=False)
        cpb = x_base.shape[1] // self.num_branch
        x_t = []
        for i in range(self.num_branch):
            x_t.append(getattr(self, f"trans_patch_conv_{i}")(x_base[:, i*cpb : i*cpb + cpb, :, :]).flatten(2).transpose(1, 2))
        
        x_t = torch.cat([cls_tokens] + x_t, dim=1)
        x_t = x_t + pos_embed
        x_t = self.trans_1(x_t)

    
        # 2 ~ final 
        for i in range(2, self.fin_stage):
            x, x_t = eval('self.conv_trans_' + str(i))(x, x_t)


        x_p = self.pooling(x)
        cpb = x_p.shape[1] // self.num_branch
        conv_latent = []
        for i in range(self.num_branch):
            conv_latent.append(getattr(self, f"conv_head{i}")(x_p[:, i*cpb : i*cpb + cpb, :, :].flatten(1)))
        conv_latent = self.conv_head(torch.cat(conv_latent, 1))

        # trans classification
        x_t = self.trans_norm(x_t)
        tran_latent = self.trans_cls_head(x_t[:, 0])
        mu = self.mean_head(torch.cat([conv_latent, tran_latent], 1))
        var = self.var_head(torch.cat([conv_latent, tran_latent], 1)) if self.var_head else None
        return mu, var
    




class decoder(nn.Module):

    def __init__(self, patch_size=16, embed_dim=768, base_channel=64, channel_ratio=4, num_med_block=0,
                 depth=12, im_size=224, first_up=2):

        # Transformer
        super().__init__()
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        assert depth % 3 == 0


        self.patch_size = patch_size
        self.im_size = im_size


        # Latent to map
        self.first_fc = nn.Linear(embed_dim, int(base_channel * (im_size // (16 * first_up)) * (im_size // (16 * first_up))))
        self.first_cnn = nn.Conv2d(base_channel, int(base_channel * channel_ratio * 4), kernel_size=1)
        self.frist_up = nn.Upsample(scale_factor=first_up)
        self.first_up_scale = first_up
        trans_dw_stride = patch_size // 16
        


        # 0~3 stage
        stage_1_channel = int(base_channel * channel_ratio * 2 * 2)
        init_stage = 0  # 0
        fin_stage = init_stage
        fin_stage = fin_stage + depth // 3  # 4
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            in_channel = stage_1_channel if i == init_stage else stage_1_channel
            res_conv = True if i == init_stage else False
            self.add_module('conv_trans_' + str(i),
                    CNNBlock(
                        in_channel, stage_1_channel, res_conv, s,
                        num_med_block=num_med_block, decode=True
                    )
            )

        # 3~7 stage
        stage_2_channel = int(base_channel * channel_ratio * 2)
        init_stage = fin_stage # 4
        fin_stage = fin_stage + depth // 3 # 8
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            in_channel = stage_1_channel if i == init_stage else stage_2_channel
            res_conv = True if i == init_stage else False
            self.add_module('conv_trans_' + str(i),
                    CNNBlock(
                        in_channel, stage_2_channel, res_conv, s, 
                        num_med_block=num_med_block, decode=True
                    )
            )


        # 8~11 stage
        stage_3_channel = int(base_channel * channel_ratio)
        init_stage = fin_stage
        fin_stage = init_stage + depth // 3
        for i in range(init_stage, fin_stage):
            s = 2 if i == init_stage else 1
            in_channel = stage_2_channel if i == init_stage else stage_3_channel
            last_fusion = True if i == fin_stage - 1 else False
            res_conv = True if i == init_stage or last_fusion else False
            channel = stage_3_channel // 2 if i == fin_stage - 1 else stage_3_channel
            self.add_module('conv_trans_' + str(i),
                    CNNBlock(
                        in_channel, channel, res_conv, s,
                        num_med_block=num_med_block, last_fusion=last_fusion, decode=True
                    )
            )
        
        self.fin_stage = fin_stage
        self.dw_stride = trans_dw_stride * 2 * 2 * 2 * 2



        self.conv_last = CNN(inplanes=stage_3_channel // 2, outplanes=3, res_conv=True, stride=1, act_layer=nn.Tanh)





        


        


        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv2d):
            nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        elif isinstance(m, nn.BatchNorm2d):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)
        elif isinstance(m, nn.GroupNorm):
            nn.init.constant_(m.weight, 1.)
            nn.init.constant_(m.bias, 0.)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'cls_token'}


    def forward(self, x):
        B, _ = x.shape
        x = self.first_fc(x).reshape(B, -1, (self.im_size // (16 * self.first_up_scale)), (self.im_size // (16 * self.first_up_scale)))
       
        x = self.first_cnn(x)
        x = self.frist_up(x)

        # 1 ~ final 
        for i in range(self.fin_stage):
            x = eval('self.conv_trans_' + str(i))(x)

        x = self.conv_last(x, return_x_2=False)
        return x


    
    def patchify(self, imgs):
        """
        imgs: (N, 3, H, W)
        x: (N, L, patch_size**2 *3)
        """
        p = self.patch_size
        assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0

        h = w = imgs.shape[2] // p
        x = imgs.reshape(shape=(imgs.shape[0], 3, h, p, w, p))
        x = torch.einsum('nchpwq->nhwpqc', x)
        x = x.reshape(shape=(imgs.shape[0], h * w, p**2 * 3))
        return x

    def unpatchify(self, x):
        """
        x: (N, L, patch_size**2 *3)
        imgs: (N, 3, H, W)
        """
        p = 14
        channel = self.last_channel
        h = w = int(x.shape[1]**.5)

        
        x = x.reshape(shape=(x.shape[0], h, w, p, p, 3))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], 3, h * p, h * p))
        return imgs
    
    



class cnn_attn_encoder_cnn_decoder(nn.Module):

    def __init__(self, patch_size=16, in_chans=3, decode_embed=384, base_channel=64, channel_ratio=4, num_med_block=0,
                 embed_dim=768, depth=12, num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0., im_size=224, first_up=2, num_branch=4, use_vae=True, **kwargs):
        
        super().__init__()
        
        self.encoder = encoder(patch_size=patch_size, in_chans=in_chans, decode_embed=decode_embed, base_channel=base_channel, 
                               channel_ratio=channel_ratio, num_med_block=num_med_block,embed_dim=embed_dim, depth=depth, 
                               num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop_rate=drop_rate, 
                               attn_drop_rate=attn_drop_rate, drop_path_rate=drop_path_rate, im_size=im_size, num_branch=num_branch, use_vae=use_vae)
        


        for i in range(4):
            setattr(self, f"decoder_{i}", decoder(patch_size=patch_size, base_channel=base_channel, 
                               channel_ratio=channel_ratio, num_med_block=num_med_block,embed_dim=decode_embed, depth=depth, 
                                im_size=im_size, first_up=first_up))


    def sample(self, mu, log_var):
        if log_var is not None:
            var = torch.exp(0.5 * log_var)
            z = torch.randn_like(mu)
            z = var * z + mu
        else:
            z = mu
        return z


    def forward(self, x):
        mu, var  = self.encoder(x)
        latent = self.sample(mu, var)
        pred = []
        for i in range(4):
            pred.append(getattr(self, f"decoder_{i}")(latent))
        pred = torch.cat(pred, 1)
        return pred, mu, var
        
