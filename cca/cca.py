import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import Dataset, DataLoader
from torchvision.models import resnet18

__all__ = ["svcca_distance", "pwcca_distance", "CCAHook"]


def svd_reduction(tensor: torch.Tensor, accept_rate=0.99):
    left, diag, right = torch.svd(tensor)
    full = diag.abs().sum()
    ratio = diag.abs().cumsum(dim=0) / full
    num = torch.where(ratio < accept_rate,
                      torch.ones(1).to(ratio.device),
                      torch.zeros(1).to(ratio.device)
                      ).sum()
    return tensor @ right[:, :int(num)]


def zero_mean(tensor: torch.Tensor, dim):
    return tensor - tensor.mean(dim=dim, keepdim=True)


def _svd_cca(x, y):
    u_1, s_1, v_1 = x.svd()
    u_2, s_2, v_2 = y.svd()
    uu = u_1.t() @ u_2
    try:
        u, diag, v = (uu).svd()
    except RuntimeError as e:
        raise e
    a = v_1 @ s_1.reciprocal().diag() @ u
    b = v_2 @ s_2.reciprocal().diag() @ v
    return a, b, diag


def _cca(x, y, method):
    """
    Canonical Correlation Analysis,
    cf. Press 2011 "Cannonical Correlation Clarified by Singular Value Decomposition"
    :param x: data matrix [data, neurons]
    :param y: data matrix [data, neurons]
    :param method: computational method "svd"  or "qr"
    :return: _cca vectors for input x, _cca vectors for input y, canonical correlations
    """
    assert x.size(0) == y.size(0), f"Number of data needs to be same but {x.size(0)} and {y.size(0)}"
    assert x.size(0) >= x.size(1) and y.size(0) >= y.size(1), f"data[0] should be larger than data[1]"
    assert method in ("svd", "qr"), "Unknown method"

    x = zero_mean(x, dim=0)
    y = zero_mean(y, dim=0)
    return _svd_cca(x, y)


def svcca_distance(x, y, method="svd"):
    """
    SVCCA distance proposed in Raghu et al. 2017
    :param x: data matrix [data, neurons]
    :param y: data matrix [data, neurons]
    :param method: computational method "svd" (default) or "qr"
    """
    x = svd_reduction(x)
    y = svd_reduction(y)
    div = min(x.size(1), y.size(1))
    a, b, diag = _cca(x, y, method=method)
    return 1 - diag.sum() / div


def pwcca_distance(x, y, method="svd"):
    """
    Project Weighting CCA proposed in Marcos et al. 2018
    :param x: data matrix [data, neurons]
    :param y: data matrix [data, neurons]
    :param method: computational method "svd" (default) or "qr"
    """
    a, b, diag = _cca(x, y, method=method)
    alpha = (x @ a).abs().sum(dim=0)
    alpha = alpha / alpha.sum()
    return 1 - alpha @ diag


def _conv2d_reshape(tensor, size):
    b, c, h, w = tensor.shape
    if size is not None:
        if (size, size) > (h, w):
            raise RuntimeError(f"`size` should be smaller than the tensor's size but ({h}, {w})")
        tensor = F.adaptive_avg_pool2d(tensor, size)
    tensor = tensor.reshape(b, c, -1).permute(2, 0, 1)
    return tensor


def _conv2d(tensor1, tensor2, cca_function, size):
    if tensor1.shape != tensor2.shape:
        raise RuntimeError("tensors' shapes are incompatible!")
    tensor1 = _conv2d_reshape(tensor1, size)
    tensor2 = _conv2d_reshape(tensor2, size)
    return torch.Tensor([cca_function(t1, t2)
                         for t1, t2 in zip(tensor1, tensor2)]).mean()


class CCAHook(object):
    _supported_modules = (nn.Conv2d, nn.Linear)
    _cca_distance_function = {"svcca": svcca_distance,
                              "pwcca": pwcca_distance}

    def __init__(self, model: nn.Module, name: str, *, cca_distance: str or function = pwcca_distance, svd_cpu=True):
        """
        Hook to calculate CCA distance between outputs of layers
        >>> model = resnet18()
        >>> hook1 = CCAHook(model, "layer3.0.conv1")
        >>> hook2 = CCAHook(model, "layer3.0.conv2")
        >>> model.eval()
        >>> model(torch.randn(1200, 3, 224, 224))
        >>> hook1.distance(hook2, 8)
        :param model: nn.Module model
        :param name: name of the layer you use
        :param cca_distance ("pwcca_distance" or "svcca_distance"). "pwcca_distance" by default
        :param svd_cpu: specifies if you use cpu for SVD (maybe faster). True by default
        """

        self.model = model
        self.name = name
        _dict = {n: m for n, m in self.model.named_modules()}
        if self.name not in _dict.keys():
            raise NameError(f"No such name ({self.name}) in the model")
        if type(_dict[self.name]) not in self._supported_modules:
            raise TypeError(f"{type(_dict[self.name])} is not supported")

        self._module = _dict[self.name]
        self._module = {n: m for n, m in self.model.named_modules()}[self.name]
        self._key = f"_{self.name}_hooked_value"
        setattr(self._module, self._key, None)
        self._register_hook()
        if type(cca_distance) == str:
            cca_distance = self._cca_distance_function[cca_distance]
        self._cca_distance = cca_distance
        if svd_cpu:
            from multiprocessing import cpu_count

            torch.set_num_threads(cpu_count())

        self._cpu = svd_cpu and torch.cuda.is_available()

    def clear(self):
        """
        clear the hooked tensor
        """
        setattr(self._module, self._key, None)

    def distance(self, other, size: int or tuple = None):
        """
        returns cca distance between the hooked tensor and `other`'s hooked tensor.
        :param other: CCAHook
        :param size: if two tensor's size are
        :return: CCA distance
        """
        tensor1 = self._get_hooked_value()
        tensor2 = other._get_hooked_value()
        if tensor1.dim() != tensor2.dim():
            raise RuntimeError("tensor dimensions are incompatible!")
        if self._cpu:
            tensor1 = tensor1.to("cpu")
            tensor2 = tensor2.to("cpu")
        if type(self._module) == nn.Linear:
            return self._cca_distance(tensor1, tensor2).item()
        elif type(self._module) == nn.Conv2d:
            return _conv2d(tensor1, tensor2, self._cca_distance, size).item()

    @staticmethod
    def data(dataset: Dataset, batch_size: int, *, num_workers: int = 2):
        """
        returns batch of data to calculate CCA distance
        :param dataset: torch.utils.data.Dataset
        :param batch_size:
        :param num_workers:
        :return: tensor
        """
        data_loader = DataLoader(dataset, batch_size=batch_size, num_workers=num_workers, shuffle=True)
        input, _ = next(iter(data_loader))
        return input

    def _register_hook(self):
        key = self._key

        def hook(module, _, output):
            setattr(module, key, output)

        self._module.register_forward_hook(hook)

    def _get_hooked_value(self):
        value = getattr(self._module, self._key)
        if value is None:
            raise RuntimeError("Please do model.forward() before CCA!")
        return value
