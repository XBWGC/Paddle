# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import numpy as np
import unittest
import sys
import os
import paddle
import paddle.fluid as fluid
from paddle.jit import to_static
from paddle.utils.cpp_extension import load
from paddle.optimizer.lr import LRScheduler
import tempfile

SEED = 2022


class SimpleLayer(paddle.nn.Layer):

    def __init__(self, use_ipu=False):
        super(SimpleLayer, self).__init__()
        self.use_ipu = use_ipu
        self.conv = paddle.nn.Conv2D(in_channels=3,
                                     out_channels=1,
                                     kernel_size=2,
                                     stride=1)

    def forward(self, x, target=None):
        x = self.conv(x)
        x = paddle.fluid.layers.flatten(x, axis=1)
        if target is not None:
            x = paddle.fluid.layers.softmax(x)
            loss = paddle.fluid.layers.cross_entropy(x, target)
            if self.use_ipu:
                loss = paddle.incubate.identity_loss(loss, 1)
            else:
                loss = paddle.mean(loss)
            return x, loss
        return x


class TestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        paddle.disable_static()
        cls.save_path = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.save_path.cleanup()

    def _test(self, use_ipu=False):
        paddle.seed(SEED)
        np.random.seed(SEED)
        model = SimpleLayer(use_ipu)
        specs = [
            paddle.static.InputSpec(name="x",
                                    shape=[32, 3, 10, 10],
                                    dtype="float32"),
            paddle.static.InputSpec(name="target", shape=[32], dtype="int64"),
        ]
        model = paddle.jit.to_static(model, input_spec=specs)
        optim = paddle.optimizer.Adam(learning_rate=0.01,
                                      parameters=model.parameters())
        data = paddle.uniform((32, 3, 10, 10), dtype='float32')
        label = paddle.randint(0, 10, shape=[32], dtype='int64')
        model_path = '{}/model_state_dict_{}.pdparams'.format(
            self.save_path, 'ipu' if use_ipu else 'cpu')
        optim_path = '{}/optim_state_dict_{}.pdopt'.format(
            self.save_path, 'ipu' if use_ipu else 'cpu')

        if use_ipu:
            device = paddle.set_device('ipu')
            ipu_strategy = paddle.static.IpuStrategy()
            ipu_strategy.set_graph_config(num_ipus=1,
                                          is_training=True,
                                          micro_batch_size=1,
                                          enable_manual_shard=False)
            ipu_strategy.set_precision_config(enable_fp16=True)
            ipu_strategy.set_optimizer(optim)
            data = data.astype(np.float16)

        result = []
        for epoch in range(100):
            # ipu only needs call model() to do forward/backward/grad_update
            pred, loss = model(data, label)
            if not use_ipu:
                loss.backward()
                optim.step()
                optim.clear_grad()

            result.append(loss)

        if use_ipu:
            paddle.fluid.core.IpuBackend.get_instance().weights_to_host()

        paddle.save(model.state_dict(), model_path)
        paddle.save(optim.state_dict(), optim_path)

        model.set_state_dict(paddle.load(model_path))
        optim.set_state_dict(paddle.load(optim_path))

        for epoch in range(100):
            # ipu only needs call model() to do forward/backward/grad_update
            pred, loss = model(data, label)
            if not use_ipu:
                loss.backward()
                optim.step()
                optim.clear_grad()

            result.append(loss)

        return np.array(result)

    def test_training(self):
        cpu_loss = self._test(False).flatten()
        ipu_loss = self._test(True).flatten()

        self.assertTrue(np.allclose(ipu_loss, cpu_loss, atol=1e-2))


if __name__ == "__main__":
    unittest.main()
