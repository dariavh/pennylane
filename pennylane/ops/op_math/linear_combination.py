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
This submodule contains the discrete-variable quantum operations that perform
arithmetic operations on their input states.
"""
# pylint: disable=too-many-arguments,too-many-instance-attributes
import itertools
import numbers
from collections.abc import Iterable
from copy import copy
import functools
from typing import List
import numpy as np
import scipy


import pennylane as qml
from .composite import CompositeOp
from pennylane.operation import Observable, Tensor, _UNSET_BATCH_SIZE, Operator
from pennylane.wires import Wires

OBS_MAP = {"PauliX": "X", "PauliY": "Y", "PauliZ": "Z", "Hadamard": "H", "Identity": "I"}


def _compute_grouping_indices(ops, grouping_type="qwc", method="rlf"):
    # todo: directly compute the
    # indices, instead of extracting groups of ops first
    observable_groups = qml.pauli.group_ops(
        ops, coefficients=None, grouping_type=grouping_type, method=method
    )

    ops = copy(ops)

    indices = []
    available_indices = list(range(len(ops)))
    for partition in observable_groups:  # pylint:disable=too-many-nested-blocks
        indices_this_group = []
        for pauli_word in partition:
            # find index of this pauli word in remaining original ops,
            for ind, observable in enumerate(ops):
                if qml.pauli.are_identical_pauli_words(pauli_word, observable):
                    indices_this_group.append(available_indices[ind])
                    # delete this observable and its index, so it cannot be found again
                    ops.pop(ind)
                    available_indices.pop(ind)
                    break
        indices.append(tuple(indices_this_group))

    return tuple(indices)


class LinearCombination(CompositeOp):
    r"""Operator representing a linear combination of operators (LCO).

    The LCO is represented as a linear combination of other operators, e.g.,
    :math:`\sum_{k=0}^{N-1} c_k O_k`, where the :math:`c_k` are trainable parameters.

    Args:
        coeffs (tensor_like): coefficients of the LCO expression
        ops (Iterable[Observable]): ops in the LCO expression, of same length as coeffs
        simplify (bool): Specifies whether the LCO is simplified upon initialization
                         (like-terms are combined). The default value is `False`.
        grouping_type (str): If not None, compute and store information on how to group commuting
            ops upon initialization. This information may be accessed when QNodes containing this
            LCO are executed on devices. The string refers to the type of binary relation between Pauli words.
            Can be ``'qwc'`` (qubit-wise commuting), ``'commuting'``, or ``'anticommuting'``.
        method (str): The graph coloring heuristic to use in solving minimum clique cover for grouping, which
            can be ``'lf'`` (Largest First) or ``'rlf'`` (Recursive Largest First). Ignored if ``grouping_type=None``.
        id (str): name to be assigned to this LCO instance

    **Example:**

    A LCO can be created by simply passing the list of coefficients
    as well as the list of ops:

    >>> coeffs = [0.2, -0.543]
    >>> obs = [qml.PauliX(0) @ qml.PauliZ(1), qml.PauliZ(0) @ qml.Hadamard(2)]
    >>> H = qml.dot(coeffs, obs)
    >>> print(H)
      (-0.543) [Z0 H2]
    + (0.2) [X0 Z1]

    The coefficients can be a trainable tensor, for example:

    >>> coeffs = tf.Variable([0.2, -0.543], dtype=tf.double)
    >>> obs = [qml.PauliX(0) @ qml.PauliZ(1), qml.PauliZ(0) @ qml.Hadamard(2)]
    >>> H = qml.dot(coeffs, obs)
    >>> print(H)
      (-0.543) [Z0 H2]
    + (0.2) [X0 Z1]

    The user can also provide custom ops:

    >>> obs_matrix = np.array([[0.5, 1.0j, 0.0, -3j],
                               [-1.0j, -1.1, 0.0, -0.1],
                               [0.0, 0.0, -0.9, 12.0],
                               [3j, -0.1, 12.0, 0.0]])
    >>> obs = qml.Hermitian(obs_matrix, wires=[0, 1])
    >>> H = qml.dot((0.8, ), (obs, ))
    >>> print(H)
    (0.8) [Hermitian0,1]

    Alternatively, the :func:`~.molecular_hamiltonian` function from the
    :doc:`/introduction/chemistry` module can be used to generate a molecular
    Hamiltonian.

    In many cases, LCOs can be constructed using Pythonic arithmetic operations.
    For example:

    >>> qml.dot([1.], [qml.PauliX(0)]) + 2 * qml.PauliZ(0) @ qml.PauliZ(1)

    is equivalent to the following LCO:

    >>> qml.dot([1, 2], [qml.PauliX(0), qml.PauliZ(0) @ qml.PauliZ(1)])

    While scalar multiplication requires native python floats or integer types,
    addition, subtraction, and tensor multiplication of LCOs with LCOs or
    other ops is possible with tensor-valued coefficients, i.e.,

    >>> H1 = qml.dot(torch.tensor([1.]), [qml.PauliX(0)])
    >>> H2 = qml.dot(torch.tensor([2., 3.]), [qml.PauliY(0), qml.PauliX(1)])
    >>> obs3 = [qml.PauliX(0), qml.PauliY(0), qml.PauliX(1)]
    >>> H3 = qml.dot(torch.tensor([1., 2., 3.]), obs3)
    >>> H3.compare(H1 + H2)
    True

    A LCO can store information on which commuting ops should be measured together in
    a circuit:

    >>> obs = [qml.PauliX(0), qml.PauliX(1), qml.PauliZ(0)]
    >>> coeffs = np.array([1., 2., 3.])
    >>> H = qml.dot(coeffs, obs, grouping_type='qwc')
    >>> H.grouping_indices
    [[0, 1], [2]]

    This attribute can be used to compute groups of coefficients and ops:

    >>> grouped_coeffs = [coeffs[indices] for indices in H.grouping_indices]
    >>> grouped_obs = [[H.ops[i] for i in indices] for indices in H.grouping_indices]
    >>> grouped_coeffs
    [tensor([1., 2.], requires_grad=True), tensor([3.], requires_grad=True)]
    >>> grouped_obs
    [[qml.PauliX(0), qml.PauliX(1)], [qml.PauliZ(0)]]

    Devices that evaluate a LCO expectation by splitting it into its local ops can
    use this information to reduce the number of circuits evaluated.

    Note that one can compute the ``grouping_indices`` for an already initialized LCO by
    using the :func:`compute_grouping <pennylane.LinearCombination.compute_grouping>` method.
    """

    num_wires = qml.operation.AnyWires
    grad_method = "A"  # supports analytic gradients
    batch_size = None
    ndim_params = None  # could be (0,) * len(coeffs), but it is not needed. Define at class-level
    _op_symbol = "+"
    _math_op = qml.math.sum

    def _flatten(self):
        # note that we are unable to restore grouping type or method without creating new properties
        return (self.data, self._ops), (self.grouping_indices,)

    @classmethod
    def _unflatten(cls, data, metadata):
        new_op = cls(data[0], data[1])
        new_op._grouping_indices = metadata[0]  # pylint: disable=protected-access
        return new_op

    def __init__(
        self,
        coeffs,
        ops: List[Observable],
        simplify=False,
        grouping_type=None,
        method="rlf",
        id=None,
    ):
        if qml.math.shape(coeffs)[0] != len(ops):
            raise ValueError(
                "Could not create valid linear combination of operators; "
                "number of coefficients and operators does not match."
            )

        for obs in ops:
            if not isinstance(obs, Observable):
                raise ValueError("Could not create circuits. Some or all ops are not valid.")

        self._coeffs = coeffs
        self._ops = list(ops)
        self.operands = (
            qml.s_prod(c, op) for c, op in zip(coeffs, ops)
        )  # generator to avoid explicit construction

        # TODO: avoid having multiple ways to store ops and coeffs,
        # ideally only use parameters for coeffs, and hyperparameters for ops
        self._hyperparameters = {"ops": self._ops}

        self._wires = qml.wires.Wires.all_wires([op.wires for op in self.ops], sort=True)

        # attribute to store indices used to form groups of
        # commuting ops, since recomputation is costly
        self._grouping_indices = None

        if simplify:
            self.simplify()
        if grouping_type is not None:
            with qml.QueuingManager.stop_recording():
                self._grouping_indices = _compute_grouping_indices(
                    self.ops, grouping_type=grouping_type, method=method
                )

        # coeffs_flat = [self._coeffs[i] for i in range(qml.math.shape(self._coeffs)[0])]

        # Things from CompositeOp __init__
        self._id = id
        self.queue_idx = None
        self._name = self.__class__.__name__
        self._hash = None
        self._has_overlapping_wires = None
        self._overlapping_ops = None
        self._pauli_rep = self._build_pauli_rep()
        self.queue()
        self._batch_size = _UNSET_BATCH_SIZE

    def _build_pauli_rep(self):
        if all(op_pauli_rep := [op.pauli_rep for op in self._ops]):
            coeffs = self._coeffs
            return sum(
                (c * op for c, op in zip(coeffs, op_pauli_rep)), start=coeffs[0] * op_pauli_rep[0]
            )
        return None

    @classmethod
    def _sort(cls, op_list, wire_map: dict = None) -> List[Operator]:
        """Sort algorithm that sorts a list of sum summands by their wire indices.

        Args:
            op_list (List[.Operator]): list of operators to be sorted
            wire_map (dict): Dictionary containing the wire values as keys and its indexes as values.
                Defaults to None.

        Returns:
            List[.Operator]: sorted list of operators
        """

        if isinstance(op_list, tuple):
            op_list = list(op_list)

        def _sort_key(op: Operator) -> tuple:
            """Sorting key used in the `sorted` python built-in function.

            Args:
                op (.Operator): Operator.

            Returns:
                Tuple[int, int, str]: Tuple containing the minimum wire value, the number of wires
                    and the string of the operator. This tuple is used to compare different operators
                    in the sorting algorithm.
            """
            wires = op.wires
            if wire_map is not None:
                wires = wires.map(wire_map)
            return sorted(list(map(str, wires)))[0], len(wires), str(op)

        return sorted(op_list, key=_sort_key)

    @property
    def is_hermitian(self):
        """If all of the terms in the sum are hermitian, then the Sum is hermitian."""
        if self.pauli_rep is not None:
            coeffs_list = self._coeffs
            if not qml.math.is_abstract(coeffs_list[0]):
                return not any(qml.math.iscomplex(c) for c in coeffs_list)

        return all(s.is_hermitian for s in self)

    def matrix(self, wire_order=None):
        r"""Representation of the operator as a matrix in the computational basis.

        If ``wire_order`` is provided, the numerical representation considers the position of the
        operator's wires in the global wire order. Otherwise, the wire order defaults to the
        operator's wires.

        If the matrix depends on trainable parameters, the result
        will be cast in the same autodifferentiation framework as the parameters.

        A ``MatrixUndefinedError`` is raised if the matrix representation has not been defined.

        .. seealso:: :meth:`~.Operator.compute_matrix`

        Args:
            wire_order (Iterable): global wire order, must contain all wire labels from the
            operator's wires

        Returns:
            tensor_like: matrix representation
        """
        coeffs, ops = self.coeffs, self.ops
        gen = (
            (c * qml.matrix(op) if isinstance(op, qml.Hamiltonian) else c * op.matrix(), op.wires)
            for c, op in zip(coeffs, ops)
        )

        reduced_mat, sum_wires = qml.math.reduce_matrices(gen, reduce_func=qml.math.add)

        wire_order = wire_order or self.wires

        return qml.math.expand_matrix(reduced_mat, sum_wires, wire_order=wire_order)

    def _check_batching(self):
        """Override for LinearCombination, batching is not yet supported."""

    def label(self, decimals=None, base_label=None, cache=None):
        decimals = None if (len(self.parameters) > 3) else decimals
        return super().label(decimals=decimals, base_label=base_label or "𝓗", cache=cache)

    @property
    def coeffs(self):
        """Return the coefficients defining the LCO.

        Returns:
            Iterable[float]): coefficients in the LCO expression
        """
        return self._coeffs

    @property
    def ops(self):
        """Return the operators defining the LCO.

        Returns:
            Iterable[Observable]): ops in the LCO expression
        """
        return self._ops

    def terms(self):
        r"""Representation of the operator as a linear combination of other operators.

         .. math:: O = \sum_i c_i O_i

         .. seealso:: :meth:`~.LinearCombination.terms`

        Returns:
            tuple[Iterable[tensor_like or float], list[.Operator]]: coefficients and operations

        **Example**
        >>> coeffs = [1., 2.]
        >>> ops = [qml.PauliX(0), qml.PauliZ(0)]
        >>> H = qml.dot(coeffs, ops)

        >>> H.terms()
        [1., 2.], [qml.PauliX(0), qml.PauliZ(0)]

        The coefficients are differentiable and can be stored as tensors:
        >>> import tensorflow as tf
        >>> H = qml.dot([tf.Variable(1.), tf.Variable(2.)], [qml.PauliX(0), qml.PauliZ(0)])
        >>> t = H.terms()

        >>> t[0]
        [<tf.Tensor: shape=(), dtype=float32, numpy=1.0>, <tf.Tensor: shape=(), dtype=float32, numpy=2.0>]
        """
        return self.parameters, self.ops

    @property
    def wires(self):
        r"""The sorted union of wires from all operators.

        Returns:
            (Wires): Combined wires present in all terms, sorted.
        """
        return self._wires

    @property
    def name(self):
        return "LinearCombination"

    @property
    def grouping_indices(self):
        """Return the grouping indices attribute.

        Returns:
            list[list[int]]: indices needed to form groups of commuting ops
        """
        return self._grouping_indices

    @grouping_indices.setter
    def grouping_indices(self, value):
        """Set the grouping indices, if known without explicit computation, or if
        computation was done externally. The groups are not verified.

        **Example**

        Examples of valid groupings for the LCO

        >>> H = qml.dot([qml.PauliX('a'), qml.PauliX('b'), qml.PauliY('b')])

        are

        >>> H.grouping_indices = [[0, 1], [2]]

        or

        >>> H.grouping_indices = [[0, 2], [1]]

        since both ``qml.PauliX('a'), qml.PauliX('b')`` and ``qml.PauliX('a'), qml.PauliY('b')`` commute.


        Args:
            value (list[list[int]]): List of lists of indexes of the ops in ``self.ops``. Each sublist
                represents a group of commuting ops.
        """

        if (
            not isinstance(value, Iterable)
            or any(not isinstance(sublist, Iterable) for sublist in value)
            or any(i not in range(len(self.ops)) for i in [i for sl in value for i in sl])
        ):
            raise ValueError(
                f"The grouped index value needs to be a tuple of tuples of integers between 0 and the "
                f"number of ops in the LCO; got {value}"
            )
        # make sure all tuples so can be hashable
        self._grouping_indices = tuple(tuple(sublist) for sublist in value)

    def compute_grouping(self, grouping_type="qwc", method="rlf"):
        """
        Compute groups of indices corresponding to commuting ops of this
        LCO, and store it in the ``grouping_indices`` attribute.

        Args:
            grouping_type (str): The type of binary relation between Pauli words used to compute the grouping.
                Can be ``'qwc'``, ``'commuting'``, or ``'anticommuting'``.
            method (str): The graph coloring heuristic to use in solving minimum clique cover for grouping, which
                can be ``'lf'`` (Largest First) or ``'rlf'`` (Recursive Largest First).
        """

        with qml.QueuingManager.stop_recording():
            self._grouping_indices = _compute_grouping_indices(
                self.ops, grouping_type=grouping_type, method=method
            )

    def sparse_matrix(self, wire_order=None):
        r"""Computes the sparse matrix representation of a LCO in the computational basis.

        Args:
            wire_order (Iterable): global wire order, must contain all wire labels from the operator's wires.
                If not provided, the default order of the wires (self.wires) of the LCO is used.

        Returns:
            csr_matrix: a sparse matrix in scipy Compressed Sparse Row (CSR) format with dimension
            :math:`(2^n, 2^n)`, where :math:`n` is the number of wires

        **Example:**

        >>> coeffs = [1, -0.45]
        >>> obs = [qml.PauliZ(0) @ qml.PauliZ(1), qml.PauliY(0) @ qml.PauliZ(1)]
        >>> H = qml.dot(coeffs, obs)
        >>> H_sparse = H.sparse_matrix()
        >>> H_sparse
        <4x4 sparse matrix of type '<class 'numpy.complex128'>'
                with 8 stored elements in Compressed Sparse Row format>

        The resulting sparse matrix can be either used directly or transformed into a numpy array:

        >>> H_sparse.toarray()
        array([[ 1.+0.j  ,  0.+0.j  ,  0.+0.45j,  0.+0.j  ],
               [ 0.+0.j  , -1.+0.j  ,  0.+0.j  ,  0.-0.45j],
               [ 0.-0.45j,  0.+0.j  , -1.+0.j  ,  0.+0.j  ],
               [ 0.+0.j  ,  0.+0.45j,  0.+0.j  ,  1.+0.j  ]])
        """
        if wire_order is None:
            wires = self.wires
        else:
            wires = wire_order
        n = len(wires)
        matrix = scipy.sparse.csr_matrix((2**n, 2**n), dtype="complex128")

        coeffs = qml.math.toarray(self.data)

        temp_mats = []
        for coeff, op in zip(coeffs, self.ops):
            obs = []
            for o in qml.operation.Tensor(op).obs:
                if len(o.wires) > 1:
                    # todo: deal with operations created from multi-qubit operations such as Hermitian
                    raise ValueError(
                        f"Can only sparsify LCOs whose constituent ops consist of "
                        f"(tensor products of) single-qubit operators; got {op}."
                    )
                obs.append(o.matrix())

            # Array to store the single-wire ops which will be Kronecker producted together
            mat = []
            # i_count tracks the number of consecutive single-wire identity matrices encountered
            # in order to avoid unnecessary Kronecker products, since I_n x I_m = I_{n+m}
            i_count = 0
            for wire_lab in wires:
                if wire_lab in op.wires:
                    if i_count > 0:
                        mat.append(scipy.sparse.eye(2**i_count, format="coo"))
                    i_count = 0
                    idx = op.wires.index(wire_lab)
                    # obs is an array storing the single-wire ops which
                    # make up the full LCO term
                    sp_obs = scipy.sparse.coo_matrix(obs[idx])
                    mat.append(sp_obs)
                else:
                    i_count += 1

            if i_count > 0:
                mat.append(scipy.sparse.eye(2**i_count, format="coo"))

            red_mat = (
                functools.reduce(lambda i, j: scipy.sparse.kron(i, j, format="coo"), mat) * coeff
            )

            temp_mats.append(red_mat.tocsr())
            # Value of 100 arrived at empirically to balance time savings vs memory use. At this point
            # the `temp_mats` are summed into the final result and the temporary storage array is
            # cleared.
            if (len(temp_mats) % 100) == 0:
                matrix += sum(temp_mats)
                temp_mats = []

        matrix += sum(temp_mats)
        return matrix

    def simplify(self):
        r"""Simplifies the LCO by combining like-terms.

        **Example**

        >>> ops = [qml.PauliY(2), qml.PauliX(0) @ qml.Identity(1), qml.PauliX(0)]
        >>> H = qml.dot([1, 1, -2], ops)
        >>> H.simplify()
        >>> print(H)
          (-1) [X0]
        + (1) [Y2]

        .. warning::

            Calling this method will reset ``grouping_indices`` to None, since
            the ops it refers to are updated.
        """

        # Todo: make simplify return a new operation, so
        # it does not mutate this one

        new_coeffs = []
        new_ops = []

        for i in range(len(self.ops)):  # pylint: disable=consider-using-enumerate
            op = self.ops[i]
            c = self.coeffs[i]
            op = op if isinstance(op, Tensor) else Tensor(op)

            ind = next((j for j, o in enumerate(new_ops) if op.compare(o)), None)
            if ind is not None:
                new_coeffs[ind] += c
                if np.isclose(qml.math.toarray(new_coeffs[ind]), np.array(0.0)):
                    del new_coeffs[ind]
                    del new_ops[ind]
            else:
                new_ops.append(op.prune())
                new_coeffs.append(c)

        # hotfix: We `self.data`, since `self.parameters` returns a copy of the data and is now returned in
        # self.terms(). To be improved soon.
        self.data = tuple(new_coeffs)
        # hotfix: We overwrite the hyperparameter entry, which is now returned in self.terms().
        # To be improved soon.
        self.hyperparameters["ops"] = new_ops

        self._coeffs = qml.math.stack(new_coeffs) if new_coeffs else []
        self._ops = new_ops
        self._wires = qml.wires.Wires.all_wires([op.wires for op in self.ops], sort=True)
        # reset grouping, since the indices refer to the old ops and coefficients
        self._grouping_indices = None
        return self

    def __str__(self):
        def wires_print(ob: Observable):
            """Function that formats the wires."""
            return ",".join(map(str, ob.wires.tolist()))

        list_of_coeffs = self.data  # list of scalar tensors
        paired_coeff_obs = list(zip(list_of_coeffs, self.ops))
        paired_coeff_obs.sort(key=lambda pair: (len(pair[1].wires), qml.math.real(pair[0])))

        terms_ls = []

        for coeff, obs in paired_coeff_obs:
            if isinstance(obs, Tensor):
                obs_strs = [f"{OBS_MAP.get(ob.name, ob.name)}{wires_print(ob)}" for ob in obs.obs]
                ob_str = " ".join(obs_strs)
            elif isinstance(obs, Observable):
                ob_str = f"{OBS_MAP.get(obs.name, obs.name)}{wires_print(obs)}"

            term_str = f"({coeff}) [{ob_str}]"

            terms_ls.append(term_str)

        return "  " + "\n+ ".join(terms_ls)

    def __repr__(self):
        # Constructor-call-like representation
        return f"<LinearCombination: terms={qml.math.shape(self.coeffs)[0]}, wires={self.wires.tolist()}>"

    def _ipython_display_(self):  # pragma: no-cover
        """Displays __str__ in ipython instead of __repr__
        See https://ipython.readthedocs.io/en/stable/config/integrating.html
        """
        if len(self.ops) < 15:
            print(str(self))
        else:  # pragma: no-cover
            print(repr(self))

    def _obs_data(self):
        r"""Extracts the data from a LCO and serializes it in an order-independent fashion.

        This allows for comparison between LCOs that are equivalent, but are defined with terms and tensors
        expressed in different orders. For example, `qml.PauliX(0) @ qml.PauliZ(1)` and
        `qml.PauliZ(1) @ qml.PauliX(0)` are equivalent ops with different orderings.

        .. Note::

            In order to store the data from each term of the LCO in an order-independent serialization,
            we make use of sets. Note that all data contained within each term must be immutable, hence the use of
            strings and frozensets.

        **Example**

        >>> H = qml.dot([1, 1], [qml.PauliX(0) @ qml.PauliX(1), qml.PauliZ(0)])
        >>> print(H._obs_data())
        {(1, frozenset({('PauliX', <Wires = [1]>, ()), ('PauliX', <Wires = [0]>, ())})),
         (1, frozenset({('PauliZ', <Wires = [0]>, ())}))}
        """
        data = set()

        coeffs_arr = qml.math.toarray(self.coeffs)
        for co, op in zip(coeffs_arr, self.ops):
            obs = op.non_identity_obs if isinstance(op, Tensor) else [op]
            tensor = []
            for ob in obs:
                parameters = tuple(
                    str(param) for param in ob.parameters
                )  # Converts params into immutable type
                if isinstance(ob, qml.GellMann):
                    parameters += (ob.hyperparameters["index"],)
                tensor.append((ob.name, ob.wires, parameters))
            data.add((co, frozenset(tensor)))

        return data

    def compare(self, other):
        r"""Determines whether the operator is equivalent to another.

        Currently only supported for :class:`~LinearCombination`, :class:`~.Observable`, or :class:`~.Tensor`.
        LCOs/ops are equivalent if they represent the same operator
        (their matrix representations are equal), and they are defined on the same wires.

        .. Warning::

            The compare method does **not** check if the matrix representation
            of a :class:`~.Hermitian` observable is equal to an equivalent
            observable expressed in terms of Pauli matrices, or as a
            linear combination of Hermitians.
            To do so would require the matrix form of LCOs and Tensors
            be calculated, which would drastically increase runtime.

        Returns:
            (bool): True if equivalent.

        **Examples**

        >>> H = qml.dot(
        ...     [0.5, 0.5],
        ...     [qml.PauliZ(0) @ qml.PauliY(1), qml.PauliY(1) @ qml.PauliZ(0) @ qml.Identity("a")]
        ... )
        >>> obs = qml.PauliZ(0) @ qml.PauliY(1)
        >>> print(H.compare(obs))
        True

        >>> H1 = qml.dot([1, 1], [qml.PauliX(0), qml.PauliZ(1)])
        >>> H2 = qml.dot([1, 1], [qml.PauliZ(0), qml.PauliX(1)])
        >>> H1.compare(H2)
        False

        >>> ob1 = qml.dot([1], [qml.PauliX(0)])
        >>> ob2 = qml.Hermitian(np.array([[0, 1], [1, 0]]), 0)
        >>> ob1.compare(ob2)
        False
        """
        if isinstance(other, LinearCombination):
            self.simplify()
            other.simplify()
            return self._obs_data() == other._obs_data()  # pylint: disable=protected-access

        if isinstance(other, (Tensor, Observable)):
            self.simplify()
            return self._obs_data() == {
                (1, frozenset(other._obs_data()))  # pylint: disable=protected-access
            }

        raise ValueError(
            "Can only compare a LinearCombination, and a LinearCombination/Observable/Tensor."
        )

    def __matmul__(self, H):
        r"""The tensor product operation between a LinearCombination and a LinearCombination/Tensor/Observable."""
        coeffs1 = copy(self.coeffs)
        ops1 = self.ops.copy()

        if isinstance(H, LinearCombination):
            shared_wires = Wires.shared_wires([self.wires, H.wires])
            if len(shared_wires) > 0:
                raise ValueError(
                    "LinearCombinations can only be multiplied together if they act on "
                    "different sets of wires"
                )

            coeffs2 = H.coeffs
            ops2 = H.ops

            coeffs = qml.math.kron(coeffs1, coeffs2)
            ops_list = itertools.product(ops1, ops2)
            terms = [qml.operation.Tensor(t[0], t[1]) for t in ops_list]
            return qml.dot(coeffs, terms, simplify=True)

        if isinstance(H, (Tensor, Observable)):
            terms = [op @ copy(H) for op in ops1]

            return qml.dot(coeffs1, terms, simplify=True)

        return NotImplemented

    def __rmatmul__(self, H):
        r"""The tensor product operation (from the right) between a LinearCombination and
        a LinearCombination/Tensor/Observable (ie. LinearCombination.__rmul__(H) = H @ LinearCombination).
        """
        if isinstance(H, LinearCombination):  # can't be accessed by '@'
            return H.__matmul__(self)

        coeffs1 = copy(self.coeffs)
        ops1 = self.ops.copy()

        if isinstance(H, (Tensor, Observable)):
            terms = [copy(H) @ op for op in ops1]

            return qml.dot(coeffs1, terms, simplify=True)

        return NotImplemented

    def __add__(self, H):
        r"""The addition operation between a LinearCombination and a LinearCombination/Tensor/Observable."""
        ops = self.ops.copy()
        self_coeffs = copy(self.coeffs)

        if isinstance(H, numbers.Number) and H == 0:
            return self

        if isinstance(H, LinearCombination):
            coeffs = qml.math.concatenate([self_coeffs, copy(H.coeffs)], axis=0)
            ops.extend(H.ops.copy())
            return qml.dot(coeffs, ops, simplify=True)

        if isinstance(H, (Tensor, Observable)):
            coeffs = qml.math.concatenate(
                [self_coeffs, qml.math.cast_like([1.0], self_coeffs)], axis=0
            )
            ops.append(H)
            return qml.dot(coeffs, ops, simplify=True)

        return NotImplemented

    __radd__ = __add__

    def __mul__(self, a):
        r"""The scalar multiplication operation between a scalar and a LCO."""
        if isinstance(a, (int, float)):
            self_coeffs = copy(self.coeffs)
            coeffs = qml.math.multiply(a, self_coeffs)
            return qml.dot(coeffs, self.ops.copy())

        return NotImplemented

    __rmul__ = __mul__

    def __sub__(self, H):
        r"""The subtraction operation between a LinearCombination and a LinearCombination/Tensor/Observable."""
        if isinstance(H, (LinearCombination, Tensor, Observable)):
            return self + (-1 * H)
        return NotImplemented

    def __iadd__(self, H):
        r"""The inplace addition operation between a LinearCombination and a LinearCombination/Tensor/Observable."""
        if isinstance(H, numbers.Number) and H == 0:
            return self

        if isinstance(H, LinearCombination):
            self._coeffs = qml.math.concatenate([self._coeffs, H.coeffs], axis=0)
            self._ops.extend(H.ops.copy())
            self.simplify()
            return self

        if isinstance(H, (Tensor, Observable)):
            self._coeffs = qml.math.concatenate(
                [self._coeffs, qml.math.cast_like([1.0], self._coeffs)], axis=0
            )
            self._ops.append(H)
            self.simplify()
            return self

        return NotImplemented

    def __imul__(self, a):
        r"""The inplace scalar multiplication operation between a scalar and a LinearCombination."""
        if isinstance(a, (int, float)):
            self._coeffs = qml.math.multiply(a, self._coeffs)
            return self

        return NotImplemented

    def __isub__(self, H):
        r"""The inplace subtraction operation between a LinearCombination and a LinearCombination/Tensor/Observable."""
        if isinstance(H, (LinearCombination, Tensor, Observable)):
            self.__iadd__(H.__mul__(-1))
            return self
        return NotImplemented

    def queue(self, context=qml.QueuingManager):
        """Queues a qml.LinearCombination instance"""
        for o in self.ops:
            context.remove(o)
        context.append(self)
        return self

    def map_wires(self, wire_map: dict):
        """Returns a copy of the current LinearCombination with its wires changed according to the given
        wire map.

        Args:
            wire_map (dict): dictionary containing the old wires as keys and the new wires as values

        Returns:
            .LinearCombination: new LinearCombination
        """
        cls = self.__class__
        new_op = cls.__new__(cls)
        new_op.data = copy(self.data)
        new_op._wires = Wires(  # pylint: disable=protected-access
            [wire_map.get(wire, wire) for wire in self.wires]
        )
        new_op._ops = [  # pylint: disable=protected-access
            op.map_wires(wire_map) for op in self.ops
        ]
        for attr, value in vars(self).items():
            if attr not in {"data", "_wires", "_ops"}:
                setattr(new_op, attr, value)
        new_op.hyperparameters["ops"] = new_op._ops  # pylint: disable=protected-access
        return new_op