# This code is part of Qiskit.
#
# (C) Copyright IBM 2018, 2019, 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.
"""
Quantum error class for Qiskit Aer noise model
"""
import copy
import logging
import warnings
from typing import Iterable, Union, Tuple, List

import numpy as np
from qiskit.circuit import QuantumCircuit, Instruction
from qiskit.circuit.library.standard_gates import IGate
from qiskit.exceptions import QiskitError
from qiskit.extensions import UnitaryGate
from qiskit.quantum_info.operators.base_operator import BaseOperator
from qiskit.quantum_info.operators.channel import Kraus, SuperOp
from qiskit.quantum_info.operators.mixins import TolerancesMixin
from qiskit.quantum_info.operators.predicates import is_identity_matrix

from .errorutils import kraus2instructions
from .errorutils import standard_gate_unitary
from .errorutils import standard_gates_instructions
from ..noiseerror import NoiseError

logger = logging.getLogger(__name__)

QuantumNoiseType = type(Union[BaseOperator,
                              QuantumCircuit,
                              List[Tuple[Instruction, List[int]]],
                              Tuple[Instruction, List[int]],
                              Instruction])


class QuantumError(BaseOperator, TolerancesMixin):
    """
    Quantum error class for Qiskit Aer noise model

    WARNING: The init interface for this class is not finalized and may
             change in future releases. For maximum backwards compatibility
             use the QuantumError generating functions in the `noise.errors`
             module.
    """

    def __init__(self,
                 noise_ops: Union[QuantumNoiseType,
                                  Iterable[Tuple[QuantumNoiseType, float]]],
                 number_of_qubits=None,
                 standard_gates=False):
        """
        Create a quantum error for a noise model.

        Noise ops may either be specified as a ``quantum channel``
        for a general CPTP map, or as a list of ``(circuit, p)`` pairs
        where ``circuit`` is a circuit (or instruction) for the noise, and
        ``p`` is the probability of the error circuit. Any type of input
        will be converted to the probabilistic mixture of circuit format.

        **Example**

        An example noise_ops for a bit-flip error with error probability
        ``p = 0.1`` is:

        .. code-block:: python

            noise_ops = [(IGate(), 0.9),
                         (XGate(), 0.1)]

        The same error represented as a Kraus channel can be input as:

        .. code-block:: python

            noise_ops = Kraus([np.sqrt(0.9) * np.array([[1, 0], [0, 1]]),
                               np.sqrt(0.1) * np.array([[0, 1], [1, 0]])])

        Args:
            noise_ops: A list of noise ops. See additional information.
            number_of_qubits (int): [DEPRECATED] specify the number of qubits for the
                                    error. If None this will be determined
                                    automatically (default None).
            standard_gates (bool): [DEPRECATED] Check if input matrices are standard gates.
            atol (double): [DEPRECATED] Threshold for testing if probabilities are
                           equal to 0 or 1 (Default: 1e-8).
        Raises:
            NoiseError: If input noise_ops are not a CPTP map.
        """
        # Shallow copy constructor
        if isinstance(noise_ops, QuantumError):
            self._circs = noise_ops.circuits
            self._probs = noise_ops.probabilities
            super().__init__(num_qubits=noise_ops.num_qubits)
            return

        # Convert list of arrarys to kraus instruction (for old API support) TODO: to be removed
        if isinstance(noise_ops, (List, Tuple)) and isinstance(noise_ops[0], np.ndarray):
            warnings.warn(
                'Constructing QuantumError with list of arrays representing a Kraus channel'
                ' has been deprecated as of qiskit-aer 0.8.0 and will be removed no earlier than'
                ' 3 months from that release date. Use QuantumError(Kraus()) instead.',
                DeprecationWarning, stacklevel=2)
            if standard_gates:
                noise_ops = kraus2instructions(
                    noise_ops, standard_gates, atol=self.atol)
            else:
                try:
                    noise_ops = Kraus(noise_ops)
                    noise_ops = [((noise_ops.to_instruction(),
                                   list(range(noise_ops.num_qubits))), 1.0)]
                except QiskitError:
                    raise NoiseError("Cannot convert Kraus to Instruction: channel is not CPTP")

        # Convert zipped object to list (to enable multiple iteration over it)
        if isinstance(noise_ops, zip):
            noise_ops = list(noise_ops)

        # Single circuit case
        if not isinstance(noise_ops, Iterable) or \
                (isinstance(noise_ops, Tuple) and isinstance(noise_ops[0], Instruction)):
            noise_ops = [(noise_ops, 1.0)]

        # Remove zero probability circuits
        if any([isinstance(p, complex) or (p < -self.atol) for _, p in noise_ops]):
            raise NoiseError("Probabilities are invalid: {}".format([p for _, p in noise_ops]))
        noise_ops = [(op, prob) for op, prob in noise_ops if prob > 0]

        ops, probs = zip(*noise_ops)  # unzip

        if standard_gates:
            ops = [standard_gates_instructions(op) for op in ops]
            warnings.warn(
                '"standard_gates" option in the constructor of QuantumError has been deprecated'
                ' as of qiskit-aer 0.8.0 in favor of externalizing such an unrolling functionality'
                ' and will be removed no earlier than 3 months from that release date.',
                DeprecationWarning, stacklevel=2)

        # Initialize internal variables with error checking
        total_probs = np.sum(probs)
        if abs(total_probs - 1) > self.atol:
            raise NoiseError("Probabilities are not normalized: {} != 1".format(total_probs))
        # Rescale probabilities if their sum is ok to avoid accumulation of rounding errors
        self._probs = list(np.array(probs) / total_probs)

        # Convert instructions to circuits
        # pylint: disable=too-many-return-statements
        def to_circuit(op: QuantumNoiseType):
            if isinstance(op, QuantumCircuit):
                return op
            elif isinstance(op, Tuple):
                inst, qubits = op
                circ = QuantumCircuit(max(qubits) + 1)
                circ.append(inst, qargs=qubits)
                return circ
            elif isinstance(op, Instruction):
                circ = QuantumCircuit(op.num_qubits)
                circ.append(op, qargs=list(range(op.num_qubits)))
                return circ
            elif isinstance(op, BaseOperator):
                # Try to convert an operator subclass into Instruction first
                if hasattr(op, 'to_instruction'):
                    try:
                        return to_circuit(op.to_instruction())
                    except QiskitError:
                        raise NoiseError(
                            "Fail to convert {} to Instruction.".format(op.__class__.__name__))
                # Try to convert an operator subclass into Kraus
                kraus_op = Kraus(op)
                if isinstance(kraus_op, Kraus):
                    if kraus_op.is_cptp():
                        return to_circuit(kraus_op.to_instruction())
                    else:
                        raise NoiseError("Input quantum channel is not CPTP.")
                else:
                    raise NoiseError("Fail to convert {} to Kraus.".format(op.__class__.__name__))
            elif isinstance(op, List):
                if isinstance(op[0], Tuple):
                    num_qubits = max([max(qubits) for _, qubits in op]) + 1
                    circ = QuantumCircuit(num_qubits)
                    for inst, qubits in op:
                        circ.append(inst, qargs=qubits)
                    return circ
                # Support for old-style json-like input TODO: to be removed
                elif isinstance(op[0], dict):
                    warnings.warn(
                        'Constructing QuantumError with list of dict representing a mixed channel'
                        ' has been deprecated as of qiskit-aer 0.8.0 and will be removed'
                        ' no earlier than 3 months from that release date.',
                        DeprecationWarning, stacklevel=3)
                    # Convert json-like to non-kraus Instruction
                    num_qubits = max([max(dic['qubits']) for dic in op]) + 1
                    circ = QuantumCircuit(num_qubits)
                    for dic in op:
                        if dic['name'] == 'reset':
                            from qiskit.circuit import Reset
                            circ.append(Reset(), qargs=dic['qubits'])
                        elif dic['name'] == 'kraus':
                            circ.append(Instruction(name='kraus',
                                                    num_qubits=len(dic['qubits']),
                                                    num_clbits=0,
                                                    params=dic['params']),
                                        qargs=dic['qubits'])
                        elif dic['name'] == 'unitary':
                            circ.append(UnitaryGate(data=dic['params'][0]),
                                        qargs=dic['qubits'])
                        else:
                            circ.append(UnitaryGate(label=dic['name'],
                                                    data=standard_gate_unitary(dic['name'])),
                                        qargs=dic['qubits'])
                    return circ
                else:
                    raise NoiseError("Invalid noise op type (list of {}): {}".format(
                        op[0].__class__.__name__, op))

            raise NoiseError("Invalid noise op type {}: {}".format(op.__class__.__name__, op))

        circs = [to_circuit(op) for op in ops]

        num_qubits = max([qc.num_qubits for qc in circs])
        if number_of_qubits is not None:
            num_qubits = number_of_qubits
            warnings.warn(
                '"number_of_qubits" in the constructor of QuantumError has been deprecated'
                ' as of qiskit-aer 0.8.0 in favor of determining it automatically'
                ' and will be removed no earlier than 3 months from that release date.',
                DeprecationWarning, stacklevel=2)
        self._circs = [self._enlarge_qreg(qc, num_qubits) for qc in circs]

        # Check validity of circuits
        for circ in self._circs:
            if circ.clbits:
                raise NoiseError("Circuit with classical register cannot be a channel")
            if circ.num_qubits != num_qubits:
                raise NoiseError("Number of qubits used in noise ops must be the same")

        super().__init__(num_qubits=num_qubits)

    def __repr__(self):
        """Display QuantumError."""
        return "QuantumError({})".format(
            list(zip(self.circuits, self.probabilities)))

    def __str__(self):
        """Print error information."""
        output = "QuantumError on {} qubits. Noise circuits:".format(
            self.num_qubits)
        for j, pair in enumerate(zip(self.probabilities, self.circuits)):
            output += "\n  P({0}) = {1}, Circuit = \n{2}".format(
                j, pair[0], pair[1])
        return output

    def __eq__(self, other):
        """Test if two QuantumErrors are equal as SuperOps"""
        if not isinstance(other, QuantumError):
            return False
        return self.to_quantumchannel() == other.to_quantumchannel()

    def copy(self):
        """Make a copy of current QuantumError."""
        # pylint: disable=no-value-for-parameter
        # The constructor of subclasses from raw data should be a copy
        return copy.deepcopy(self)

    @classmethod
    def set_atol(cls, value):
        """Set the class default absolute tolerance parameter for float comparisons."""
        warnings.warn(
            'QuantumError.set_atol(value) has been deprecated as of qiskit-aer 0.8.0'
            ' and will be removed no earlier than 3 months from that release date.'
            ' Use QuantumError.atol = value instead.',
            DeprecationWarning, stacklevel=2)
        QuantumError.atol = value

    @classmethod
    def set_rtol(cls, value):
        """Set the class default relative tolerance parameter for float comparisons."""
        warnings.warn(
            'QuantumError.set_rtol(value) has been deprecated as of qiskit-aer 0.8.0'
            ' and will be removed no earlier than 3 months from that release date.'
            ' Use QuantumError.rtol = value instead.',
            DeprecationWarning, stacklevel=2)
        QuantumError.rtol = value

    @property
    def size(self):
        """Return the number of error circuit."""
        return len(self.circuits)

    @property
    def number_of_qubits(self):
        """Return the number of qubits for the error."""
        warnings.warn(
            '"number_of_qubits" property has been renamed to num_qubits and deprecated as of'
            ' qiskit-aer 0.8.0, and will be removed no earlier than 3 months'
            ' from that release date. Use "num_qubits" instead.',
            DeprecationWarning, stacklevel=2)
        return self.num_qubits

    @property
    def circuits(self):
        """Return the list of error circuits."""
        return self._circs

    @property
    def probabilities(self):
        """Return the list of error probabilities."""
        return self._probs

    def ideal(self):
        """Return True if current error object is an identity"""
        circ, prob = self.error_term(0)
        if prob == 1 and len(circ) == 1:
            # check if circ is identity gate up to global phase
            gate = circ[0][0]
            if isinstance(gate, IGate) or \
                    (isinstance(gate, UnitaryGate) and
                     is_identity_matrix(gate.to_matrix(),
                                        ignore_phase=True,
                                        atol=self.atol, rtol=self.rtol)):
                logger.debug("Error object is ideal")
                return True
        return False

    def to_quantumchannel(self):
        """Convert the QuantumError to a SuperOp quantum channel.
        Required to enable SuperOp(QuantumError)."""
        # Initialize as an empty superoperator of the correct size
        dim = 2 ** self.num_qubits
        ret = SuperOp(np.zeros([dim * dim, dim * dim]))
        for circ, prob in zip(self.circuits, self.probabilities):
            component = prob * SuperOp(circ)
            ret = ret + component
        return ret

    def to_instruction(self):
        """Convert the QuantumError to a circuit Instruction."""
        return self.to_quantumchannel().to_instruction()

    def error_term(self, position):
        """
        Return a single term from the error.

        Args:
            position (int): the position of the error term.

        Returns:
            tuple: A pair `(p, circuit)` for error term at `position` < size
            where `p` is the probability of the error term, and `circuit`
            is the list of qobj instructions for the error term.

        Raises:
            NoiseError: If the position is greater than the size of
            the quantum error.
        """
        if position < self.size:
            return self.circuits[position], self.probabilities[position]
        else:
            raise NoiseError("Position {} is greater than the number".format(
                position) + "of error outcomes {}".format(self.size))

    def to_dict(self):
        """Return the current error as a dictionary."""
        error = {
            "type": "qerror",
            "operations": [],
            "instructions": [self._qc_to_json(qc) for qc in self.circuits],
            "probabilities": list(self.probabilities)
        }
        return error

    @staticmethod
    def _qc_to_json(qc: QuantumCircuit):
        ret = []
        for inst, qargs, _ in qc:
            name = inst.label if isinstance(inst, UnitaryGate) and inst.label else inst.name
            dic = {'name': name,
                   'qubits': [q.index for q in qargs]}
            if inst.name == 'kraus':
                dic['params'] = inst.params
            ret.append(dic)
        return ret

    def compose(self, other, qargs=None, front=False) -> 'QuantumError':
        if not isinstance(other, QuantumError):
            return SuperOp(self).compose(other, qargs=qargs, front=front)

        if self.num_qubits != other.num_qubits:
            raise NoiseError("Number of qubis of other ({}) must be the same as self ({})".format(
                other.num_qubits, self.num_qubits))

        circs = [self._compose_circ(lqc, rqc, qubits=qargs, front=front)
                 for lqc in self.circuits
                 for rqc in other.circuits]
        probs = [lpr * rpr
                 for lpr in self.probabilities
                 for rpr in other.probabilities]
        return QuantumError(zip(circs, probs))

    @staticmethod
    def _enlarge_qreg(qc: QuantumCircuit, num_qubits: int):
        if qc.num_qubits < num_qubits:
            enlarged = QuantumCircuit(num_qubits)
            return enlarged.compose(qc)
        return qc

    @staticmethod
    def _compose_circ(lqc: QuantumCircuit, rqc: QuantumCircuit, qubits, front):
        if lqc.num_qubits < rqc.num_qubits:
            lqc = QuantumError._enlarge_qreg(lqc, rqc.num_qubits)
        return lqc.compose(rqc, qubits=qubits, front=front)

    def tensor(self, other) -> 'QuantumError':
        if not isinstance(other, QuantumError):
            return SuperOp(self).tensor(other)

        circs = [lqc.tensor(rqc)
                 for lqc in self.circuits
                 for rqc in other.circuits]
        probs = [lpr * rpr
                 for lpr in self.probabilities
                 for rpr in other.probabilities]
        return QuantumError(zip(circs, probs))

    def expand(self, other) -> 'QuantumError':
        return other.tensor(self)

    # Overloads
    def __rmul__(self, other):
        raise NotImplementedError(
            "'QuantumError' does not support scalar multiplication.")

    def __truediv__(self, other):
        raise NotImplementedError("'QuantumError' does not support division.")

    def __add__(self, other):
        raise NotImplementedError("'QuantumError' does not support addition.")

    def __sub__(self, other):
        raise NotImplementedError(
            "'QuantumError' does not support subtraction.")

    def __neg__(self):
        raise NotImplementedError("'QuantumError' does not support negation.")
