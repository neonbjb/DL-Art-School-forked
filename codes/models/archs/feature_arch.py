import torch
import torch.nn as nn
import torchvision
import torch.nn.functional as F
from models.archs.arch_util import ConvGnSilu

# Utilizes pretrained torchvision modules for feature extraction

class VGGFeatureExtractor(nn.Module):
    def __init__(self, feature_layer=34, use_bn=False, use_input_norm=True,
                 device=torch.device('cpu')):
        super(VGGFeatureExtractor, self).__init__()
        self.use_input_norm = use_input_norm
        if use_bn:
            model = torchvision.models.vgg19_bn(pretrained=True)
        else:
            model = torchvision.models.vgg19(pretrained=True)
        if self.use_input_norm:
            mean = torch.Tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1).to(device)
            # [0.485 - 1, 0.456 - 1, 0.406 - 1] if input in range [-1, 1]
            std = torch.Tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1).to(device)
            # [0.229 * 2, 0.224 * 2, 0.225 * 2] if input in range [-1, 1]
            self.register_buffer('mean', mean)
            self.register_buffer('std', std)
        self.features = nn.Sequential(*list(model.features.children())[:(feature_layer + 1)])
        # No need to BP to variable
        for k, v in self.features.named_parameters():
            v.requires_grad = False

    def forward(self, x, interpolate_factor=1):
        if interpolate_factor > 1:
            x = F.interpolate(x, scale_factor=interpolate_factor, mode='bicubic')

        if self.use_input_norm:
            x = (x - self.mean) / self.std
        output = self.features(x)
        return output


class TrainableVGGFeatureExtractor(nn.Module):
    def __init__(self, feature_layer=34, use_bn=False, use_input_norm=True,
                 device=torch.device('cpu')):
        super(TrainableVGGFeatureExtractor, self).__init__()
        self.use_input_norm = use_input_norm
        if use_bn:
            model = torchvision.models.vgg19_bn(pretrained=False)
        else:
            model = torchvision.models.vgg19(pretrained=False)
        if self.use_input_norm:
            mean = torch.Tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1).to(device)
            # [0.485 - 1, 0.456 - 1, 0.406 - 1] if input in range [-1, 1]
            std = torch.Tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1).to(device)
            # [0.229 * 2, 0.224 * 2, 0.225 * 2] if input in range [-1, 1]
            self.register_buffer('mean', mean)
            self.register_buffer('std', std)
        self.features = nn.Sequential(*list(model.features.children())[:(feature_layer + 1)])

    def forward(self, x, interpolate_factor=1):
        if interpolate_factor > 1:
            x = F.interpolate(x, scale_factor=interpolate_factor, mode='bicubic')
        # Assume input range is [0, 1]
        if self.use_input_norm:
            x = (x - self.mean) / self.std
        output = self.features(x)
        return output


class TrainableVGGFeatureExtractorWithQuality(nn.Module):
    def __init__(self, feature_layer=34, use_bn=False, use_input_norm=True,
                 device=torch.device('cpu')):
        super(TrainableVGGFeatureExtractorWithQuality, self).__init__()
        self.use_input_norm = use_input_norm
        if use_bn:
            model = torchvision.models.vgg19_bn(pretrained=False)
        else:
            model = torchvision.models.vgg19(pretrained=False)
        if self.use_input_norm:
            mean = torch.Tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1).to(device)
            # [0.485 - 1, 0.456 - 1, 0.406 - 1] if input in range [-1, 1]
            std = torch.Tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1).to(device)
            # [0.229 * 2, 0.224 * 2, 0.225 * 2] if input in range [-1, 1]
            self.register_buffer('mean', mean)
            self.register_buffer('std', std)
        self.features = nn.Sequential(*list(model.features.children())[:(feature_layer + 1)])
        end_filters = 512
        self.qual1 = ConvGnSilu(end_filters, end_filters // 2, bias=True)
        self.qual2 = ConvGnSilu(end_filters // 2, end_filters // 4, bias=False)
        self.qual3 = ConvGnSilu(end_filters // 4, 1, bias=False, norm=False, activation=False)

    def forward(self, x, interpolate_factor=1):
        if interpolate_factor > 1:
            x = F.interpolate(x, scale_factor=interpolate_factor, mode='bicubic')
        # Assume input range is [0, 1]
        if self.use_input_norm:
            x = (x - self.mean) / self.std
        output = self.features(x)

        qual = self.qual1(output)
        qual = self.qual2(qual)
        qual = self.qual3(qual)
        return output, qual


class WideResnetFeatureExtractor(nn.Module):
    def __init__(self, use_input_norm=True, device=torch.device('cpu')):
        print("Using wide resnet extractor.")
        super(WideResnetFeatureExtractor, self).__init__()
        self.use_input_norm = use_input_norm
        self.model = torchvision.models.wide_resnet50_2(pretrained=True)
        if self.use_input_norm:
            mean = torch.Tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1).to(device)
            # [0.485 - 1, 0.456 - 1, 0.406 - 1] if input in range [-1, 1]
            std = torch.Tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1).to(device)
            # [0.229 * 2, 0.224 * 2, 0.225 * 2] if input in range [-1, 1]
            self.register_buffer('mean', mean)
            self.register_buffer('std', std)
        # No need to BP to variable
        for p in self.model.parameters():
            p.requires_grad = False

    def forward(self, x):
        # Assume input range is [0, 1]
        if self.use_input_norm:
            x = (x - self.mean) / self.std
        x = self.model.conv1(x)
        x = self.model.bn1(x)
        x = self.model.relu(x)
        x = self.model.maxpool(x)
        x = self.model.layer1(x)
        x = self.model.layer2(x)
        x = self.model.layer3(x)
        return x