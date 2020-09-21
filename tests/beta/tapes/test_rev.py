# Copyright 2018-2020 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for the qubit parameter-shift QubitParamShiftTape"""
import pytest
from pennylane import numpy as np

import pennylane as qml
from pennylane.beta.interfaces.autograd import AutogradInterface
from pennylane.beta.tapes import ReversibleTape, QNode
from pennylane.beta.queuing import expval, var, sample, probs, MeasurementProcess


thetas = np.linspace(-2 * np.pi, 2 * np.pi, 8)


class TestExpectationJacobian:
    """Jacobian integration tests for qubit expectations."""

    @pytest.mark.parametrize("par", [1, -2, 1.623, -0.051, 0])  # intergers, floats, zero
    def test_ry_gradient(self, par, mocker, tol):
        """Test that various types and values of scalar multipliers for differentiable
        qfunc parameters yield the correct gradients."""

        with ReversibleTape() as tape:
            qml.RY(par, wires=[0])
            expval(qml.PauliX(0))

        tape.trainable_params = {0}

        dev = qml.device("default.qubit", wires=1)

        spy_numeric = mocker.spy(tape, "numeric_pd")
        spy_analytic = mocker.spy(tape, "analytic_pd")

        # gradients
        exact = np.cos(par)
        grad_F = tape.jacobian(dev, method="numeric")

        spy_numeric.assert_called()
        spy_analytic.assert_not_called()

        spy_device = mocker.spy(tape, "execute_device")
        grad_A = tape.jacobian(dev, method="analytic")

        spy_analytic.assert_called()
        spy_device.assert_called_once()  # check that the state was only pre-computed once

        # different methods must agree
        assert np.allclose(grad_F, exact, atol=tol, rtol=0)
        assert np.allclose(grad_A, exact, atol=tol, rtol=0)

    qubit_ops = [getattr(qml, name) for name in qml.ops._qubit__ops__]
    analytic_qubit_ops = {cls for cls in qubit_ops if cls.grad_method == "A"}
    analytic_qubit_ops = analytic_qubit_ops - {
        qml.CRX,
        qml.CRY,
        qml.CRZ,
        qml.CRot,
        qml.PhaseShift,
        qml.PauliRot,
        qml.MultiRZ,
        qml.U1,
        qml.U2,
        qml.U3,
    }

    @pytest.mark.parametrize("obs", [qml.PauliX, qml.PauliY])
    @pytest.mark.parametrize("op", analytic_qubit_ops)
    def test_gradients(self, op, obs, mocker, tol):
        """Tests that the gradients of circuits match between the
        finite difference and analytic methods."""
        args = np.linspace(0.2, 0.5, op.num_params)

        with ReversibleTape() as tape:
            qml.Hadamard(wires=0)
            qml.RX(0.543, wires=0)
            qml.CNOT(wires=[0, 1])

            op(*args, wires=range(op.num_wires))

            qml.Rot(1.3, -2.3, 0.5, wires=[0])
            qml.RZ(-0.5, wires=0)
            qml.RY(0.5, wires=1)
            qml.CNOT(wires=[0, 1])

            expval(obs(wires=0))
            expval(qml.PauliZ(wires=1))

        dev = qml.device("default.qubit", wires=2)
        res = tape.execute(dev)

        tape._update_gradient_info()
        tape.trainable_params = set(range(1, 1 + op.num_params))

        # check that every parameter is analytic
        for i in range(op.num_params):
            assert tape._par_info[1 + i]["grad_method"][0] == "A"

        spy = mocker.spy(ReversibleTape, "analytic_pd")
        grad_F = tape.jacobian(dev, method="numeric")
        grad_A = tape.jacobian(dev, method="analytic")

        spy.assert_called()
        assert np.allclose(grad_A, grad_F, atol=tol, rtol=0)

    @pytest.mark.parametrize("reused_p", thetas ** 3 / 19)
    @pytest.mark.parametrize("other_p", thetas ** 2 / 1)
    def test_fanout_multiple_params(self, reused_p, other_p, tol):
        """Tests that the correct gradient is computed for qnodes which
        use the same parameter in multiple gates."""

        from gate_data import Rotx as Rx, Roty as Ry, Rotz as Rz

        def expZ(state):
            return np.abs(state[0]) ** 2 - np.abs(state[1]) ** 2

        dev = qml.device("default.qubit", wires=1)
        extra_param = np.array(0.31, requires_grad=False)

        def cost(p1, p2):
            with AutogradInterface.apply(ReversibleTape()) as tape:
                qml.RX(extra_param, wires=[0])
                qml.RY(p1, wires=[0])
                qml.RZ(p2, wires=[0])
                qml.RX(p1, wires=[0])
                expval(qml.PauliZ(0))

            assert tape.trainable_params == {1, 2, 3}
            return tape.execute(dev)

        zero_state = np.array([1.0, 0.0])

        # analytic gradient
        grad_fn = qml.grad(cost)
        grad_A = grad_fn(reused_p, other_p)

        # manual gradient
        grad_true0 = (
            expZ(
                Rx(reused_p) @ Rz(other_p) @ Ry(reused_p + np.pi / 2) @ Rx(extra_param) @ zero_state
            )
            - expZ(
                Rx(reused_p) @ Rz(other_p) @ Ry(reused_p - np.pi / 2) @ Rx(extra_param) @ zero_state
            )
        ) / 2
        grad_true1 = (
            expZ(
                Rx(reused_p + np.pi / 2) @ Rz(other_p) @ Ry(reused_p) @ Rx(extra_param) @ zero_state
            )
            - expZ(
                Rx(reused_p - np.pi / 2) @ Rz(other_p) @ Ry(reused_p) @ Rx(extra_param) @ zero_state
            )
        ) / 2
        expected = grad_true0 + grad_true1  # product rule

        assert np.allclose(grad_A[0], expected, atol=tol, rtol=0)

    def test_gradient_gate_with_multiple_parameters(self, tol):
        """Tests that gates with multiple free parameters yield correct gradients."""
        x, y, z = [0.5, 0.3, -0.7]

        with ReversibleTape() as tape:
            qml.RX(0.4, wires=[0])
            qml.Rot(x, y, z, wires=[0])
            qml.RY(-0.2, wires=[0])
            expval(qml.PauliZ(0))

        tape.trainable_params = {1, 2, 3}

        dev = qml.device("default.qubit", wires=1)
        grad_A = tape.jacobian(dev, method="analytic")
        grad_F = tape.jacobian(dev, method="numeric")

        # gradient has the correct shape and every element is nonzero
        assert grad_A.shape == (1, 3)
        assert np.count_nonzero(grad_A) == 3
        # the different methods agree
        assert np.allclose(grad_A, grad_F, atol=tol, rtol=0)

    def test_gradient_repeated_gate_parameters(self, mocker, tol):
        """Tests that repeated use of a free parameter in a
        multi-parameter gate yield correct gradients."""
        dev = qml.device("default.qubit", wires=1)
        params = np.array([0.8, 1.3], requires_grad=True)

        def cost(params, method):
            with AutogradInterface.apply(ReversibleTape()) as tape:
                qml.RX(np.array(np.pi / 4, requires_grad=False), wires=[0])
                qml.Rot(params[1], params[0], 2 * params[0], wires=[0])
                expval(qml.PauliX(0))

            tape.jacobian_options = {"method": method}
            return tape.execute(dev)

        spy_numeric = mocker.spy(ReversibleTape, "numeric_pd")
        spy_analytic = mocker.spy(ReversibleTape, "analytic_pd")

        grad_fn = qml.grad(cost)
        grad_F = grad_fn(params, method="numeric")

        spy_numeric.assert_called()
        spy_analytic.assert_not_called()

        grad_A = grad_fn(params, method="analytic")

        spy_analytic.assert_called()

        # the different methods agree
        assert np.allclose(grad_A, grad_F, atol=tol, rtol=0)

    # def test_gradient_parameters_inside_array(self, tol):
    #     """Tests that free parameters inside an array passed to
    #     an Operation yield correct gradients."""
    #     dev = qml.device("default.qubit", wires=1)
    #     params = np.array([0.8, 1.3], requires_grad=True)

    #     def cost(params, method):
    #         with AutogradInterface.apply(ReversibleTape()) as tape:
    #             import autograd
    #             mat = np.array([params[1]])
    #             mat = np.hstack([mat, np.array([0.])])
    #             mat = np.vstack([mat, np.array([0., 1.])])
    #             qml.RX(params[0], wires=[0])
    #             qml.RY(params[0], wires=[0])
    #             expval(qml.Hermitian(mat, 0))

    #         tape.jacobian_options = {"method": method}
    #         tape._update_gradient_info()

    #         assert tape._par_info[0]["grad_method"] == "A"
    #         assert tape._par_info[1]["grad_method"] == "A"
    #         assert tape._par_info[2]["grad_method"] == "F"

    #         return tape.execute(dev)

    #     grad_fn = qml.grad(cost)
    #     grad_F = grad_fn(params, method="numeric")
    #     grad = grad_fn(params, method="best")

    #     assert np.allclose(grad, grad_F, atol=tol, rtol=0)

    def test_differentiate_all_positional(self, tol):
        """Tests that all positional arguments are differentiated."""
        dev = qml.device("default.qubit", wires=3)
        params = np.array([np.pi, np.pi / 2, np.pi / 3])

        with ReversibleTape() as tape:
            qml.RX(params[0], wires=0)
            qml.RX(params[1], wires=1)
            qml.RX(params[2], wires=2)

            for idx in range(3):
                expval(qml.PauliZ(idx))

        circuit_output = tape.execute(dev)
        expected_output = np.cos(params)
        assert np.allclose(circuit_output, expected_output, atol=tol, rtol=0)

        # circuit jacobians
        circuit_jacobian = tape.jacobian(dev, method="analytic")
        expected_jacobian = -np.diag(np.sin(params))
        assert np.allclose(circuit_jacobian, expected_jacobian, atol=tol, rtol=0)

    def test_differentiate_first_positional(self, tol):
        """Tests that the first positional arguments are differentiated."""
        dev = qml.device("default.qubit", wires=2)
        a = 0.7418

        with ReversibleTape() as tape:
            qml.RX(a, wires=0)
            expval(qml.PauliZ(0))

        circuit_output = tape.execute(dev)
        expected_output = np.cos(a)
        assert np.allclose(circuit_output, expected_output, atol=tol, rtol=0)

        # circuit jacobians
        circuit_jacobian = tape.jacobian(dev, method="analytic")
        expected_jacobian = -np.sin(a)
        assert np.allclose(circuit_jacobian, expected_jacobian, atol=tol, rtol=0)

    def test_differentiate_second_positional(self, tol):
        """Tests that the second positional arguments are differentiated."""
        dev = qml.device("default.qubit", wires=2)
        b = -5.0

        with ReversibleTape() as tape:
            qml.RX(b, wires=0)
            expval(qml.PauliZ(0))

        circuit_output = tape.execute(dev)
        expected_output = np.cos(b)
        assert np.allclose(circuit_output, expected_output, atol=tol, rtol=0)

        # circuit jacobians
        circuit_jacobian = tape.jacobian(dev, method="analytic")
        expected_jacobian = -np.sin(b)
        assert np.allclose(circuit_jacobian, expected_jacobian, atol=tol, rtol=0)

    def test_differentiate_second_third_positional(self, tol):
        """Tests that the second and third positional arguments are differentiated."""
        dev = qml.device("default.qubit", wires=2)

        a = 0.7418
        b = -5.0
        c = np.pi / 7

        with ReversibleTape() as tape:
            qml.RX(b, wires=0)
            qml.RX(c, wires=1)
            expval(qml.PauliZ(0))
            expval(qml.PauliZ(1))

        circuit_output = tape.execute(dev)
        expected_output = np.array([np.cos(b), np.cos(c)])
        assert np.allclose(circuit_output, expected_output, atol=tol, rtol=0)

        # circuit jacobians
        circuit_jacobian = tape.jacobian(dev, method="analytic")
        expected_jacobian = np.array([[-np.sin(b), 0.0], [0.0, -np.sin(c)]])
        assert np.allclose(circuit_jacobian, expected_jacobian, atol=tol, rtol=0)

    @pytest.mark.parametrize("theta", np.linspace(-2 * np.pi, 2 * np.pi, 7))
    @pytest.mark.parametrize("G", [qml.RX, qml.RY, qml.RZ])
    def test_pauli_rotation_gradient(self, G, theta, tol):
        """Tests that the automatic gradients of Pauli rotations are correct."""
        dev = qml.device("default.qubit", wires=1)

        with ReversibleTape() as tape:
            qml.QubitStateVector(np.array([1.0, -1.0]) / np.sqrt(2), wires=0)
            G(theta, wires=[0])
            expval(qml.PauliZ(0))

        tape.trainable_params = {1}

        autograd_val = tape.jacobian(dev, method="analytic")
        manualgrad_val = (
            tape.execute(dev, params=[theta + np.pi / 2])
            - tape.execute(dev, params=[theta - np.pi / 2])
        ) / 2
        assert np.allclose(autograd_val, manualgrad_val, atol=tol, rtol=0)

        # compare to finite differences
        numeric_val = tape.jacobian(dev, method="numeric")
        assert np.allclose(autograd_val, numeric_val, atol=tol, rtol=0)

    @pytest.mark.parametrize("theta", np.linspace(-2 * np.pi, 2 * np.pi, 7))
    def test_Rot_gradient(self, theta, tol):
        """Tests that the automatic gradient of a arbitrary Euler-angle-parameterized gate is correct."""
        dev = qml.device("default.qubit", wires=1)
        params = np.array([theta, theta ** 3, np.sqrt(2) * theta])

        with ReversibleTape() as tape:
            qml.QubitStateVector(np.array([1.0, -1.0]) / np.sqrt(2), wires=0)
            qml.Rot(*params, wires=[0])
            expval(qml.PauliZ(0))

        tape.trainable_params = {1, 2, 3}

        autograd_val = tape.jacobian(dev, method="analytic")
        manualgrad_val = np.zeros_like(autograd_val)

        for idx in list(np.ndindex(*params.shape)):
            s = np.zeros_like(params)
            s[idx] += np.pi / 2

            forward = tape.execute(dev, params=params + s)
            backward = tape.execute(dev, params=params - s)

            manualgrad_val[0, idx] = (forward - backward) / 2

        assert np.allclose(autograd_val, manualgrad_val, atol=tol, rtol=0)

        # compare to finite differences
        numeric_val = tape.jacobian(dev, method="numeric")
        assert np.allclose(autograd_val, numeric_val, atol=tol, rtol=0)

    @pytest.mark.parametrize("op, name", [(qml.CRX, "CRX"), (qml.CRY, "CRY"), (qml.CRZ, "CRZ")])
    def test_controlled_rotation_gates_exception(self, op, name):
        """Tests that an exception is raised when a controlled
        rotation gate is used with the ReversibleTape."""
        # remove this test when this support is added
        dev = qml.device("default.qubit", wires=2)

        with ReversibleTape() as tape:
            qml.PauliX(wires=0)
            op(0.542, wires=[0, 1])
            expval(qml.PauliZ(0))

        with pytest.raises(ValueError, match="The {} gate is not currently supported".format(name)):
            tape.jacobian(dev)

    def test_var_exception(self):
        """Tests that an exception is raised when variance
        is used with the ReversibleTape."""
        # remove this test when this support is added
        dev = qml.device("default.qubit", wires=2)

        with ReversibleTape() as tape:
            qml.PauliX(wires=0)
            qml.RX(0.542, wires=0)
            var(qml.PauliZ(0))

        with pytest.raises(ValueError, match="Variance is not supported"):
            tape.jacobian(dev)

    def test_probs_exception(self):
        """Tests that an exception is raised when probability
        is used with the ReversibleTape."""
        # remove this test when this support is added
        dev = qml.device("default.qubit", wires=2)

        with ReversibleTape() as tape:
            qml.PauliX(wires=0)
            qml.RX(0.542, wires=0)
            probs(wires=[0, 1])

        with pytest.raises(ValueError, match="Probability is not supported"):
            tape.jacobian(dev)

    def test_phaseshift_exception(self):
        """Tests that an exception is raised when a PhaseShift gate
        is used with the ReversibleTape."""
        # remove this test when this support is added
        dev = qml.device("default.qubit", wires=1)

        with ReversibleTape() as tape:
            qml.PauliX(wires=0)
            qml.PhaseShift(0.542, wires=0)
            expval(qml.PauliZ(0))

        with pytest.raises(ValueError, match="The PhaseShift gate is not currently supported"):
            tape.jacobian(dev)

    @pytest.mark.xfail(
        reason="The ReversibleTape does not support gradients of the PhaseShift gate."
    )
    def test_phaseshift_gradient(self, tol):
        """Test gradient of PhaseShift gate"""
        dev = qml.device("default.qubit", wires=1)

        a = 0.542  # any value of a should give zero gradient

        with ReversibleTape() as tape:
            qml.Hadamard(wires=0)
            qml.PhaseShift(a, wires=0)
            expval(qml.PauliZ(0))

        # get the analytic gradient
        gradA = tape.jacobian(dev, method="analytic")
        # get the finite difference gradient
        gradF = tape.jacobian(dev, method="numeric")

        # the expected gradient
        expected = 0

        assert np.allclose(gradF, expected, atol=tol, rtol=0)
        assert np.allclose(gradA, expected, atol=tol, rtol=0)

        with ReversibleTape() as tape:
            qml.Hadamard(a, wires=0)
            qml.PhaseShift(a, wires=0)
            expval(qml.PauliY(0))

        # get the analytic gradient
        gradA = np.array([1, 1]) @ tape.jacobian(dev, method="analytic")
        # get the finite difference gradient
        gradF = np.array([1, 1]) @ tape.jacobian(dev, method="numeric")

        # the expected gradient
        expected = -np.sin(a)

        assert np.allclose(gradF, expected, atol=tol, rtol=0)
        assert np.allclose(gradA, expected, atol=tol, rtol=0)

    @pytest.mark.xfail(
        reason="The ReversibleTape does not support gradients of controlled rotations"
    )
    @pytest.mark.parametrize("op", [qml.CRX, qml.CRY, qml.CRZ])
    def test_controlled_RX_gradient(self, op, tol):
        """Test gradient of controlled RX gate"""
        dev = qml.device("default.qubit", wires=2)

        a = 0.542  # any value of a should give zero gradient

        with ReversibleTape() as tape:
            qml.PauliX(wires=0)
            op(x, wires=[0, 1])
            expval(qml.PauliZ(0))

        # get the analytic gradient
        gradA = tape.jacobian(dev, method="analytic")
        # get the finite difference gradient
        gradF = tape.jacobian(dev, method="numeric")

        # the expected gradient
        expected = 0

        assert np.allclose(gradF, expected, atol=tol, rtol=0)
        assert np.allclose(gradA, expected, atol=tol, rtol=0)

        with ReversibleTape() as tape:
            qml.RX(a, wires=0)
            op(a, wires=[0, 1])
            expval(qml.PauliZ(0))

        # get the analytic gradient
        gradA = np.array([1, 1]) @ tape.jacobian(dev, method="analytic")
        # get the finite difference gradient
        gradF = np.array([1, 1]) @ tape.jacobian(dev, method="numeric")

        # the expected gradient
        expected = -np.sin(a)

        assert np.allclose(gradF, expected, atol=tol, rtol=0)
        assert np.allclose(gradA, expected, atol=tol, rtol=0)


class TestHelperFunctions:
    """Tests for additional helper functions."""

    one_qubit_vec1 = np.array([1, 1])
    one_qubit_vec2 = np.array([1, 1j])
    two_qubit_vec = np.array([1, 1, 1, -1]).reshape([2, 2])
    single_qubit_obs1 = qml.PauliZ(0)
    single_qubit_obs2 = qml.PauliY(0)
    two_qubit_obs = qml.Hermitian(np.eye(4), wires=[0, 1])

    @pytest.mark.parametrize(
        "wires, vec1, obs, vec2, expected",
        [
            (1, one_qubit_vec1, single_qubit_obs1, one_qubit_vec1, 0),
            (1, one_qubit_vec2, single_qubit_obs1, one_qubit_vec2, 0),
            (1, one_qubit_vec1, single_qubit_obs1, one_qubit_vec2, 1 - 1j),
            (1, one_qubit_vec2, single_qubit_obs1, one_qubit_vec1, 1 + 1j),
            (1, one_qubit_vec1, single_qubit_obs2, one_qubit_vec1, 0),
            (1, one_qubit_vec2, single_qubit_obs2, one_qubit_vec2, 2),
            (1, one_qubit_vec1, single_qubit_obs2, one_qubit_vec2, 1 + 1j),
            (1, one_qubit_vec2, single_qubit_obs2, one_qubit_vec1, 1 - 1j),
            (2, two_qubit_vec, single_qubit_obs1, two_qubit_vec, 0),
            (2, two_qubit_vec, single_qubit_obs2, two_qubit_vec, 0),
            (2, two_qubit_vec, two_qubit_obs, two_qubit_vec, 4),
        ],
    )
    def test_matrix_elem(self, wires, vec1, obs, vec2, expected):
        """Tests for the helper function _matrix_elem"""
        dev = qml.device("default.qubit", wires=wires)
        tape = ReversibleTape()
        res = tape._matrix_elem(vec1, obs, vec2, dev)
        assert res == expected


class TestIntegration:
    """Test integration of the ReversibleTape into the codebase"""

    def test_qnode(self, mocker, tol):
        """Test that specifying diff_method allows the reversible
        method to be selected"""
        args = np.array([0.54, 0.1, 0.5], requires_grad=True)
        dev = qml.device("default.qubit", wires=2)

        def circuit(x, y, z):
            qml.Hadamard(wires=0)
            qml.RX(0.543, wires=0)
            qml.CNOT(wires=[0, 1])

            qml.Rot(x, y, z, wires=0)

            qml.Rot(1.3, -2.3, 0.5, wires=[0])
            qml.RZ(-0.5, wires=0)
            qml.RY(0.5, wires=1)
            qml.CNOT(wires=[0, 1])

            return expval(qml.PauliX(0) @ qml.PauliZ(1))

        qnode1 = QNode(circuit, dev, diff_method="reversible")
        spy = mocker.spy(ReversibleTape, "analytic_pd")

        grad_fn = qml.grad(qnode1)
        grad_A = grad_fn(*args)

        spy.assert_called()
        assert isinstance(qnode1.qtape, ReversibleTape)

        qnode2 = QNode(circuit, dev, diff_method="finite-diff")
        grad_fn = qml.grad(qnode2)
        grad_F = grad_fn(*args)

        assert not isinstance(qnode2.qtape, ReversibleTape)
        assert np.allclose(grad_A, grad_F, atol=tol, rtol=0)
