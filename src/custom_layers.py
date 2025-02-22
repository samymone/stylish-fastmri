import sys
import pathlib as pb

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import spectral_norm

DIR_PATH = pb.Path(__file__).resolve().parent
sys.path.append(str(DIR_PATH))
import custom_ops



class SoftThresholding(nn.Module):
    def __init__(self):
        super().__init__()
        self.lambd = nn.Parameter(torch.ones(1))
        
    def forward(self, u):
        return custom_ops.soft_thresholding(u, self.lambd)


class SpectralConv2d(nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.module = spectral_norm(nn.Conv2d(*args, **kwargs))
        
    def forward(self, x):
        return self.module(x)
    
    
class AdaIN(nn.Module):
    def __init__(self, in_channels, out_channels=None, eps=1e-5):
        super().__init__()
        if out_channels is None:
            out_channels = in_channels
        self.beta = spectral_norm(nn.Linear(in_channels, out_channels))
        self.gamma = spectral_norm(nn.Linear(in_channels, out_channels))
        self.eps = eps
        
    def forward(self, x, y):
        beta = self.beta(y)
        gamma = self.gamma(y)
        x = (x - x.mean(dim=(1, 2, 3), keepdim=True)) \
            * torch.rsqrt(x.std(dim=(1, 2, 3), keepdim=True) + 1e-5)
        return x * gamma[:, :, None, None] + beta[:, :, None, None]
    
    
class NoiseApplier(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.scale = nn.Parameter(torch.empty(channels), requires_grad=True)
        torch.nn.init.xavier_normal_(self.scale.data)

    def forward(self, x, noise=None):
        b, _, h, w = x.shape
        dtype, device = x.dtype, x.device
        
        if noise is None:
            # Explicit noise in the argument is needed for proper validation
            noise = torch.randn((b, 1, h, w), dtype=dtype, device=device)
        
        return x + self.scale.view(1, -1, 1, 1) * noise
        
        
class StylishUNet(nn.Module):
    def __init__(
        self
        , num_classes=1
        , min_channels=32
        , max_channels=512
        , num_down_blocks=4
    
        , use_texture_injection=False
        , use_noise_injection=False
    ):

        assert num_down_blocks > 1
        assert max_channels // (2 ** num_down_blocks) == min_channels

        super().__init__()
        self.num_classes = num_classes
        self.num_down_blocks = num_down_blocks
        self.min_side = 2 ** self.num_down_blocks

        # Initial feature extractor from RGB image
        self.init_block = nn.Conv2d(3, min_channels, kernel_size=1)

        self.encoder_blocks = nn.ModuleList()
        self.encoder_down_blocks = nn.ModuleList()
        self.decoder_blocks = nn.ModuleList()
        self.decoder_up_blocks = nn.ModuleList()
        self.soft_thresholders = nn.ModuleList()
        
        if use_noise_injection:
            self.encoder_noise_applier_blocks = nn.ModuleList()
            self.decoder_noise_applier_blocks = nn.ModuleList()
        if use_texture_injection:
            self.encoder_adain_blocks = nn.ModuleList()
            self.decoder_adain_blocks = nn.ModuleList()
        
        for b in range(1, num_down_blocks + 1):
            ## Encoder part
            in_channels = min_channels if b == 1 else out_channels
            out_channels = max_channels // 2 ** (num_down_blocks - b)

            encoder_block = self._construct_block(in_channels)
            self.encoder_blocks.append(encoder_block)

            encoder_down_block = self._construct_down_block(in_channels, out_channels)
            self.encoder_down_blocks.append(encoder_down_block)
            
            self.soft_thresholders.append(SoftThresholding())

            if out_channels != max_channels:
                decoder_block = self._construct_block(
                    out_channels
                    , in_channels=out_channels * 2
                    , prepend_with_dropout=True
                )
            else:
                # Bottleneck
                decoder_block = self._construct_block(out_channels)
            self.decoder_blocks.insert(0, decoder_block)

            decoder_up_block = self._construct_up_block(out_channels, in_channels)
            self.decoder_up_blocks.insert(0, decoder_up_block)
            
            if use_noise_injection:
                self.encoder_noise_applier_blocks.insert(0, NoiseApplier(in_channels))
                self.decoder_noise_applier_blocks.insert(0, NoiseApplier(out_channels))
            if use_texture_injection:
                self.encoder_adain_blocks.insert(0, AdaIN(in_channels))
                self.decoder_adain_blocks.insert(0, AdaIN(out_channels))

        self.final_block = self._construct_block(
            min_channels
            , in_channels=min_channels * 2
            , prepend_with_dropout=True
        )
        
        if use_noise_injection:
            self.decoder_noise_applier_blocks.append(NoiseApplier(min_channels))
        if use_texture_injection:
            self.decoder_adain_blocks.append(AdaIN(min_channels))

        self.conv_out = nn.Conv2d(min_channels, num_classes, kernel_size=1)

    def forward(self, inputs, textures=None, noise=None):
        *_, h, w = inputs.shape
        valid_h = self._find_closest_to(h, divisible_by=self.min_side)
        valid_w = self._find_closest_to(w, divisible_by=self.min_side)
        x = F.interpolate(inputs, size=(valid_h, valid_w), mode='bilinear')

        x = self.init_block(x)

        down_features = []
        for i, (e, ed) in enumerate(zip(self.encoder_blocks, self.encoder_down_blocks)):
            x = e(x)
            
            if hasattr(self, 'encoder_noise_applier_blocks'):
                x = self.encoder_noise_applier_blocks[i](x, noise)
            if hasattr(self, 'encoder_adain_blocks'):
                x = self.encoder_adain_blocks[i](x, textures)
                
            down_features.insert(0, x.clone())
            x = ed(x)

        for i, (d, du) in enumerate(zip(self.decoder_blocks, self.decoder_up_blocks)):
            if i < 0:
                # Bottleneck
                x = d(x)
            else:
                x = torch.cat([
                    x
                    , self.soft_thresholders[i](down_features[i])
                ], dim=1)
                x = d(x)
                
            if hasattr(self, 'decoder_noise_applier_blocks'):
                x = self.decoder_noise_applier_blocks[i](x, noise)
            if hasattr(self, 'decoder_adain_blocks'):
                x = self.decoder_adain_blocks[i](x, textures)
            
            x = du(x)

        x = torch.cat([
            x
            , self.soft_thresholders[0](down_features[0])
        ], dim=1)
        x = self.final_block(x)
        
        if hasattr(self, 'decoder_noise_applier_blocks'):
            x = self.decoder_noise_applier_blocks[-1](x, noise)
        if hasattr(self, 'decoder_adain_blocks'):
            x = self.decoder_adain_blocks[-1](x, textures)
        
        x = self.conv_out(x)
        x = F.interpolate(x, size=(h, w), mode='bilinear')
        logits = x

        assert logits.shape == (inputs.shape[0], self.num_classes, inputs.shape[2], inputs.shape[3]), 'Wrong shape of the logits'
        
        return logits

    def _construct_block(self, channels, in_channels=None, prepend_with_dropout=False):
        block_layers = [
            SpectralConv2d(channels, channels, kernel_size=3, padding=1, bias=False)
            , nn.BatchNorm2d(channels)
            , nn.LeakyReLU(inplace=True)
            , SpectralConv2d(channels, channels, kernel_size=3, padding=1, bias=False)
            , nn.BatchNorm2d(channels)
            , nn.LeakyReLU(inplace=True)
        ]

        if in_channels is not None:
            # To fuse concatenated tensor
            block_layers.insert(0, nn.Conv2d(in_channels, channels, kernel_size=1))

        if prepend_with_dropout:
            block_layers.insert(0, nn.Dropout2d(0.5))

        return nn.Sequential(*block_layers)

    def _construct_down_block(self, in_channels, out_channels):
        block_layers = [
            nn.MaxPool2d(2)
            , SpectralConv2d(in_channels, out_channels, kernel_size=1)
        ]
        return nn.Sequential(*block_layers)

    def _construct_up_block(self, in_channels, out_channels):
        return nn.ConvTranspose2d(in_channels, out_channels, kernel_size=3, stride=2, padding=1, output_padding=1)

    @classmethod
    def _find_closest_to(cls, num, divisible_by):
        if num % divisible_by == 0:
            closest = num
        else:
            lesser = num - (num % divisible_by)
            bigger = (num + divisible_by) - (num % divisible_by)
            closest = lesser if abs(num - lesser) < abs(num - bigger) else bigger
        return closest


class DataConsistedStylishUNet(StylishUNet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
    def forward(self, x, known_freq, mask, textures=None, noise=None):
        data_consistency = custom_ops.data_consistency(x, known_freq, mask)
        x = torch.cat([x, data_consistency], dim=1)
        return super().forward(x, textures, noise)
