# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
from torchvision import datasets
from torchvision.transforms import ToTensor

from olive.data.registry import Registry


@Registry.register_post_process()
def mnist_post_process_for_multiple_ep(res):
    return res.argmax(1)


@Registry.register_dataset()
def mnist_dataset_for_multiple_ep(data_dir, *args, **kwargs):
    return datasets.MNIST(data_dir, transform=ToTensor())
