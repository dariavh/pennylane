# Copyright 2024 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This submodule defines the abstract classes and primitives for capture.
"""

from functools import lru_cache

import pennylane as qml

from .measure import _get_measure_primitive

has_jax = True
try:
    import jax
except ImportError:
    has_jax = False


@lru_cache()
def _get_qnode_prim():

    qnode_prim = jax.core.Primitive("qnode")
    qnode_prim.multiple_results = True

    @qnode_prim.def_impl
    def _(*args, shots, device, qnode_kwargs, qfunc_jaxpr):
        def qfunc(*inner_args):
            return jax.core.eval_jaxpr(qfunc_jaxpr.jaxpr, qfunc_jaxpr.consts, *inner_args)

        qnode = qml.QNode(qfunc, device, **qnode_kwargs)
        return qnode._impl_call(*args, shots=shots)  # pylint: disable=protected-access

    measure_prim = _get_measure_primitive()

    # pylint: disable=unused-argument
    @qnode_prim.def_abstract_eval
    def _(*args, shots, device, qnode_kwargs, qfunc_jaxpr):
        mps = qfunc_jaxpr.out_avals
        return measure_prim.abstract_eval(*mps, shots=shots, num_device_wires=len(device.wires))[0]

    return qnode_prim


# pylint: disable=protected-access
def _get_device_shots(device) -> "qml.measurements.Shots":
    if isinstance(device, qml.devices.LegacyDevice):
        if device._shot_vector:
            return qml.measurements.Shots(device._raw_shot_sequence)
        return qml.measurements.Shots(device.shots)
    return device.shots


def qnode_call(qnode, *args, **kwargs):
    """A capture compatible call to a qnode."""
    shots = kwargs.pop("shots", _get_device_shots(qnode.device))
    shots = qml.measurements.Shots(shots)
    if kwargs:
        raise NotImplementedError

    if not qnode.device.wires:
        raise NotImplementedError("devices must specify wires for integration with plxpr capture.")

    qfunc_jaxpr = jax.make_jaxpr(qnode.func)(*args)
    qnode_kwargs = {"diff_method": qnode.diff_method, **qnode.execute_kwargs}
    qnode_prim = _get_qnode_prim()

    return qnode_prim.bind(
        *args, shots=shots, device=qnode.device, qnode_kwargs=qnode_kwargs, qfunc_jaxpr=qfunc_jaxpr
    )