import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
import torch.nn.utils.spectral_norm as SpectralNorm
from math import sqrt
from pytorch_wavelets import DWTForward, DWTInverse

def pixel_norm(x, epsilon=1e-8):
    return x * torch.rsqrt(torch.mean(torch.pow(x, 2), dim=1, keepdims=True) + epsilon)

def initialize_weights(net_l, scale=1):
    if not isinstance(net_l, list):
        net_l = [net_l]
    for net in net_l:
        for m in net.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.Conv3d):
                init.kaiming_normal_(m.weight, a=0, mode='fan_in')
                m.weight.data *= scale  # for residual block
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                init.kaiming_normal_(m.weight, a=0, mode='fan_in')
                m.weight.data *= scale
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                init.constant_(m.weight, 1)
                init.constant_(m.bias.data, 0.0)


def make_layer(block, n_layers, return_layers=False):
    layers = []
    for _ in range(n_layers):
        layers.append(block())
    if return_layers:
        return nn.Sequential(*layers), layers
    else:
        return nn.Sequential(*layers)


class ResidualBlock(nn.Module):
    '''Residual block with BN
    ---Conv-BN-ReLU-Conv-+-
     |________________|
    '''

    def __init__(self, nf=64):
        super(ResidualBlock, self).__init__()
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)
        self.conv1 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.BN1 = nn.BatchNorm2d(nf)
        self.conv2 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.BN2 = nn.BatchNorm2d(nf)

        # initialization
        initialize_weights([self.conv1, self.conv2], 0.1)

    def forward(self, x):
        identity = x
        out = self.lrelu(self.BN1(self.conv1(x)))
        out = self.BN2(self.conv2(out))
        return identity + out

class ResidualBlockSpectralNorm(nn.Module):
    '''Residual block with Spectral Normalization.
    ---SpecConv-ReLU-SpecConv-+-
     |________________|
    '''

    def __init__(self, nf, total_residual_blocks):
        super(ResidualBlockSpectralNorm, self).__init__()
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)
        self.conv1 = SpectralNorm(nn.Conv2d(nf, nf, 3, 1, 1, bias=True))
        self.conv2 = SpectralNorm(nn.Conv2d(nf, nf, 3, 1, 1, bias=True))

        initialize_weights([self.conv1, self.conv2], 1)

    def forward(self, x):
        identity = x
        out = self.lrelu(self.conv1(x))
        out = self.conv2(out)
        return identity + out

class ResidualBlock_noBN(nn.Module):
    '''Residual block w/o BN
    ---Conv-ReLU-Conv-+-
     |________________|
    '''

    def __init__(self, nf=64):
        super(ResidualBlock_noBN, self).__init__()
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)
        self.conv1 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)
        self.conv2 = nn.Conv2d(nf, nf, 3, 1, 1, bias=True)

        # initialization
        initialize_weights([self.conv1, self.conv2], 0.1)

    def forward(self, x):
        identity = x
        out = self.lrelu(self.conv1(x))
        out = self.conv2(out)
        return identity + out


def flow_warp(x, flow, interp_mode='bilinear', padding_mode='zeros'):
    """Warp an image or feature map with optical flow
    Args:
        x (Tensor): size (N, C, H, W)
        flow (Tensor): size (N, H, W, 2), normal value
        interp_mode (str): 'nearest' or 'bilinear'
        padding_mode (str): 'zeros' or 'border' or 'reflection'

    Returns:
        Tensor: warped image or feature map
    """
    assert x.size()[-2:] == flow.size()[1:3]
    B, C, H, W = x.size()
    # mesh grid
    grid_y, grid_x = torch.meshgrid(torch.arange(0, H), torch.arange(0, W))
    grid = torch.stack((grid_x, grid_y), 2).float()  # W(x), H(y), 2
    grid.requires_grad = False
    grid = grid.type_as(x)
    vgrid = grid + flow
    # scale grid to [-1,1]
    vgrid_x = 2.0 * vgrid[:, :, :, 0] / max(W - 1, 1) - 1.0
    vgrid_y = 2.0 * vgrid[:, :, :, 1] / max(H - 1, 1) - 1.0
    vgrid_scaled = torch.stack((vgrid_x, vgrid_y), dim=3)
    output = F.grid_sample(x, vgrid_scaled, mode=interp_mode, padding_mode=padding_mode)
    return output


class PixelUnshuffle(nn.Module):
    def __init__(self, reduction_factor):
        super(PixelUnshuffle, self).__init__()
        self.r = reduction_factor

    def forward(self, x):
        (b, f, w, h) = x.shape
        x = x.contiguous().view(b, f, w // self.r, self.r, h // self.r, self.r)
        x = x.permute(0, 1, 3, 5, 2, 4).contiguous().view(b, f * (self.r ** 2), w // self.r, h // self.r)
        return x


# simply define a silu function
def silu(input):
    '''
    Applies the Sigmoid Linear Unit (SiLU) function element-wise:
        SiLU(x) = x * sigmoid(x)
    '''
    return input * torch.sigmoid(input)

# create a class wrapper from PyTorch nn.Module, so
# the function now can be easily used in models
class SiLU(nn.Module):
    '''
    Applies the Sigmoid Linear Unit (SiLU) function element-wise:
        SiLU(x) = x * sigmoid(x)
    Shape:
        - Input: (N, *) where * means, any number of additional
          dimensions
        - Output: (N, *), same shape as the input
    References:
        -  Related paper:
        https://arxiv.org/pdf/1606.08415.pdf
    Examples:
        >>> m = silu()
        >>> input = torch.randn(2)
        >>> output = m(input)
    '''
    def __init__(self):
        '''
        Init method.
        '''
        super().__init__() # init the base class

    def forward(self, input):
        '''
        Forward pass of the function.
        '''
        return silu(input)


''' Convenience class with Conv->BN->ReLU. Includes weight initialization and auto-padding for standard
    kernel sizes. '''
class ConvBnRelu(nn.Module):
    def __init__(self, filters_in, filters_out, kernel_size=3, stride=1, activation=True, norm=True, bias=True):
        super(ConvBnRelu, self).__init__()
        padding_map = {1: 0, 3: 1, 5: 2, 7: 3}
        assert kernel_size in padding_map.keys()
        self.conv = nn.Conv2d(filters_in, filters_out, kernel_size, stride, padding_map[kernel_size], bias=bias)
        if norm:
            self.bn = nn.BatchNorm2d(filters_out)
        else:
            self.bn = None
        if activation:
            self.relu = nn.ReLU()
        else:
            self.relu = None

        # Init params.
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu' if self.relu else 'linear')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.conv(x)
        if self.bn:
            x = self.bn(x)
        if self.relu:
            return self.relu(x)
        else:
            return x


''' Convenience class with Conv->BN->SiLU. Includes weight initialization and auto-padding for standard
    kernel sizes. '''
class ConvBnSilu(nn.Module):
    def __init__(self, filters_in, filters_out, kernel_size=3, stride=1, activation=True, norm=True, bias=True, weight_init_factor=1):
        super(ConvBnSilu, self).__init__()
        padding_map = {1: 0, 3: 1, 5: 2, 7: 3}
        assert kernel_size in padding_map.keys()
        self.conv = nn.Conv2d(filters_in, filters_out, kernel_size, stride, padding_map[kernel_size], bias=bias)
        if norm:
            self.bn = nn.BatchNorm2d(filters_out)
        else:
            self.bn = None
        if activation:
            self.silu = SiLU()
        else:
            self.silu = None

        # Init params.
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu' if self.silu else 'linear')
                m.weight.data *= weight_init_factor
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.conv(x)
        if self.bn:
            x = self.bn(x)
        if self.silu:
            return self.silu(x)
        else:
            return x


''' Convenience class with Conv->BN->LeakyReLU. Includes weight initialization and auto-padding for standard
    kernel sizes. '''
class ConvBnLelu(nn.Module):
    def __init__(self, filters_in, filters_out, kernel_size=3, stride=1, activation=True, norm=True, bias=True, weight_init_factor=1):
        super(ConvBnLelu, self).__init__()
        padding_map = {1: 0, 3: 1, 5: 2, 7: 3}
        assert kernel_size in padding_map.keys()
        self.conv = nn.Conv2d(filters_in, filters_out, kernel_size, stride, padding_map[kernel_size], bias=bias)
        if norm:
            self.bn = nn.BatchNorm2d(filters_out)
        else:
            self.bn = None
        if activation:
            self.lelu = nn.LeakyReLU(negative_slope=.1)
        else:
            self.lelu = None

        # Init params.
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, a=.1, mode='fan_out',
                                        nonlinearity='leaky_relu' if self.lelu else 'linear')
                m.weight.data *= weight_init_factor
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.conv(x)
        if self.bn:
            x = self.bn(x)
        if self.lelu:
            return self.lelu(x)
        else:
            return x


''' Convenience class with Conv->GroupNorm->LeakyReLU. Includes weight initialization and auto-padding for standard
    kernel sizes. '''
class ConvGnLelu(nn.Module):
    def __init__(self, filters_in, filters_out, kernel_size=3, stride=1, activation=True, norm=True, bias=True, num_groups=8, weight_init_factor=1):
        super(ConvGnLelu, self).__init__()
        padding_map = {1: 0, 3: 1, 5: 2, 7: 3}
        assert kernel_size in padding_map.keys()
        self.conv = nn.Conv2d(filters_in, filters_out, kernel_size, stride, padding_map[kernel_size], bias=bias)
        if norm:
            self.gn = nn.GroupNorm(num_groups, filters_out)
        else:
            self.gn = None
        if activation:
            self.lelu = nn.LeakyReLU(negative_slope=.1)
        else:
            self.lelu = None

        # Init params.
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, a=.1, mode='fan_out',
                                        nonlinearity='leaky_relu' if self.lelu else 'linear')
                m.weight.data *= weight_init_factor
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.conv(x)
        if self.gn:
            x = self.gn(x)
        if self.lelu:
            return self.lelu(x)
        else:
            return x

''' Convenience class with Conv->BN->SiLU. Includes weight initialization and auto-padding for standard
    kernel sizes. '''
class ConvGnSilu(nn.Module):
    def __init__(self, filters_in, filters_out, kernel_size=3, stride=1, activation=True, norm=True, bias=True, num_groups=8, weight_init_factor=1):
        super(ConvGnSilu, self).__init__()
        padding_map = {1: 0, 3: 1, 5: 2, 7: 3}
        assert kernel_size in padding_map.keys()
        self.conv = nn.Conv2d(filters_in, filters_out, kernel_size, stride, padding_map[kernel_size], bias=bias)
        if norm:
            self.gn = nn.GroupNorm(num_groups, filters_out)
        else:
            self.gn = None
        if activation:
            self.silu = SiLU()
        else:
            self.silu = None

        # Init params.
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu' if self.silu else 'linear')
                m.weight.data *= weight_init_factor
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.conv(x)
        if self.gn:
            x = self.gn(x)
        if self.silu:
            return self.silu(x)
        else:
            return x

# Block that upsamples 2x and reduces incoming filters by 2x. It preserves structure by taking a passthrough feed
# along with the feature representation.
class ExpansionBlock(nn.Module):
    def __init__(self, filters_in, filters_out=None, block=ConvGnSilu):
        super(ExpansionBlock, self).__init__()
        if filters_out is None:
            filters_out = filters_in // 2
        self.decimate = block(filters_in, filters_out, kernel_size=1, bias=False, activation=False, norm=True)
        self.process_passthrough = block(filters_out, filters_out, kernel_size=3, bias=True, activation=False, norm=True)
        self.conjoin = block(filters_out*2, filters_out, kernel_size=3, bias=False, activation=True, norm=False)
        self.process = block(filters_out, filters_out, kernel_size=3, bias=False, activation=True, norm=True)

    # input is the feature signal with shape  (b, f, w, h)
    # passthrough is the structure signal with shape (b, f/2, w*2, h*2)
    # output is conjoined upsample with shape (b, f/2, w*2, h*2)
    def forward(self, input, passthrough):
        x = F.interpolate(input, scale_factor=2, mode="nearest")
        x = self.decimate(x)
        p = self.process_passthrough(passthrough)
        x = self.conjoin(torch.cat([x, p], dim=1))
        return self.process(x)


# Block that upsamples 2x and reduces incoming filters by 2x. It preserves structure by taking a passthrough feed
# along with the feature representation.
# Differs from ExpansionBlock because it performs all processing in 2xfilter space and decimates at the last step.
class ExpansionBlock2(nn.Module):
    def __init__(self, filters_in, filters_out=None, block=ConvGnSilu):
        super(ExpansionBlock2, self).__init__()
        if filters_out is None:
            filters_out = filters_in // 2
        self.decimate = block(filters_in, filters_out, kernel_size=1, bias=False, activation=False, norm=True)
        self.process_passthrough = block(filters_out, filters_out, kernel_size=3, bias=True, activation=False, norm=True)
        self.conjoin = block(filters_out*2, filters_out*2, kernel_size=3, bias=False, activation=True, norm=False)
        self.reduce = block(filters_out*2, filters_out, kernel_size=3, bias=False, activation=True, norm=True)

    # input is the feature signal with shape  (b, f, w, h)
    # passthrough is the structure signal with shape (b, f/2, w*2, h*2)
    # output is conjoined upsample with shape (b, f/2, w*2, h*2)
    def forward(self, input, passthrough):
        x = F.interpolate(input, scale_factor=2, mode="nearest")
        x = self.decimate(x)
        p = self.process_passthrough(passthrough)
        x = self.conjoin(torch.cat([x, p], dim=1))
        return self.reduce(x)


# Similar to ExpansionBlock but does not upsample.
class ConjoinBlock(nn.Module):
    def __init__(self, filters_in, filters_out=None, block=ConvGnSilu, norm=True):
        super(ConjoinBlock, self).__init__()
        if filters_out is None:
            filters_out = filters_in
        self.decimate = block(filters_in*2, filters_out, kernel_size=1, bias=False, activation=False, norm=norm)
        self.process = block(filters_out, filters_out, kernel_size=3, bias=False, activation=True, norm=norm)

    # input is the feature signal with shape  (b, f, w, h)
    # passthrough is the structure signal with shape (b, f/2, w*2, h*2)
    # output is conjoined upsample with shape (b, f/2, w*2, h*2)
    def forward(self, input, passthrough):
        x = torch.cat([input, passthrough], dim=1)
        x = self.decimate(x)
        return self.process(x)


# Similar to ExpansionBlock2 but does not upsample.
class ConjoinBlock2(nn.Module):
    def __init__(self, filters_in, filters_out=None, block=ConvGnSilu, norm=True):
        super(ConjoinBlock2, self).__init__()
        if filters_out is None:
            filters_out = filters_in
        self.process = block(filters_in*2, filters_in*2, kernel_size=3, bias=False, activation=True, norm=norm)
        self.decimate = block(filters_in*2, filters_out, kernel_size=1, bias=False, activation=False, norm=norm)

    def forward(self, input, passthrough):
        x = torch.cat([input, passthrough], dim=1)
        x = self.process(x)
        return self.decimate(x)


# Basic convolutional upsampling block that uses interpolate.
class UpconvBlock(nn.Module):
    def __init__(self, filters_in, filters_out=None, block=ConvGnSilu, norm=True, activation=True, bias=False):
        super(UpconvBlock, self).__init__()
        self.process = block(filters_out, filters_out, kernel_size=3, bias=bias, activation=activation, norm=norm)

    def forward(self, x):
        x = F.interpolate(x, scale_factor=2, mode="nearest")
        return self.process(x)

class ExtractDiscreteWavelet(nn.Module):
    def __init__(self, passno=1):
        super(ExtractDiscreteWavelet, self).__init__()
        self.transform = DWTForward(J=passno+1, mode='periodization', wave='db3')

    def forward(self, x):
        residual, hi = self.transform(x)
        x = hi[-1]
        b, c, d, w, h = x.shape
        return x.view(b, c*d, w, h)
