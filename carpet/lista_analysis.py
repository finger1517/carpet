""" Module to define Optimization Neural Net. """
# Authors: Hamza Cherkaoui <hamza.cherkaoui@inria.fr>
# Authors: Thomas Moreau <thomas.moreau@inria.fr>
# License: BSD (3-clause)

import torch
import numpy as np
from .checks import check_tensor
from .utils import init_vuz
from .lista_synthesis import ListaLASSO
from .proximity_tv import ProxTV_l1, RegTV
from .proximity import pseudo_soft_th_tensor
from .lista_base import ListaBase, DOC_LISTA
from .parameters import ravel_group_params, unravel_group_params


LEARN_PROX_PER_LAYER = 'per-layer'
LEARN_PROX_GLOBAL = 'global'
LEARN_PROX_FALSE = 'none'
ALL_LEARN_PROX = [LEARN_PROX_FALSE, LEARN_PROX_GLOBAL, LEARN_PROX_PER_LAYER]


class _ListaAnalysis(ListaBase):

    _output = 'u-analysis'

    def _loss_fn(self, x, lbda, u):
        r"""Loss function for the primal.

            :math:`L(u) = 1/2 ||x - Au||_2^2 - lbda ||D u||_1`
        """
        n_samples = x.shape[0]
        residual = u.matmul(self.A_) - x
        loss = 0.5 * (residual * residual).sum()
        if self.use_moreau:
            loss = RegTV.apply(loss, u, lbda)
        else:
            loss = loss + lbda * torch.abs(u[:, 1:] - u[:, :-1]).sum()
        return loss / n_samples


class ListaTV(_ListaAnalysis):
    __doc__ = DOC_LISTA.format(
        type='learned-TV original',
        problem_name='TV',
        descr='original parametrization from Gregor and Le Cun (2010)'
    )

    def __init__(self, A, n_layers, initial_parameters=None, learn_th=True,
                 learn_prox=LEARN_PROX_FALSE, use_moreau=False,
                 n_inner_layers=500, max_iter=100, net_solver_type="recursive",
                 name=None, verbose=0, device=None):
        self.learn_prox = learn_prox
        self.use_moreau = use_moreau
        self.n_inner_layers = n_inner_layers
        if self.learn_prox == LEARN_PROX_PER_LAYER:
            self.prox_tv = {}

        n_atoms = A.shape[0]
        self.A = np.array(A)
        self.I_k = np.eye(n_atoms)
        self.D = (np.eye(n_atoms, k=-1) - np.eye(n_atoms, k=0))[:, :-1]

        self.A_ = check_tensor(self.A, device=device)
        self.inv_A_ = torch.pinverse(self.A_)
        self.l_ = np.linalg.norm(self.A, ord=2) ** 2

        if name is None:
            name = f'LPGD - Lista[{learn_prox}-{n_inner_layers}]'

        super().__init__(n_layers=n_layers, learn_th=learn_th,
                         max_iter=max_iter, net_solver_type=net_solver_type,
                         initial_parameters=initial_parameters, name=name,
                         verbose=verbose, device=device)

    def get_global_parameters(self, initial_parameters):

        if self.learn_prox in (LEARN_PROX_GLOBAL, LEARN_PROX_FALSE):
            initial_parameters_prox = unravel_group_params(
                initial_parameters.get('prox', {})
            )
            self.prox_tv = ListaLASSO(
                A=self.I_k, n_layers=self.n_inner_layers,
                learn_th=True, name="Prox-TV-Lista",
                initial_parameters=initial_parameters_prox,
                device=self.device)

            if self.learn_prox == LEARN_PROX_GLOBAL:
                self._register_parameters(
                    ravel_group_params(self.prox_tv.parameter_groups),
                    group_name='prox'
                )
                self.force_learn_groups.append('prox')

        elif self.learn_prox == LEARN_PROX_PER_LAYER:
            # Make sure that all prox have been correctly initialized with
            # initial parameters
            for layer_id in range(self.n_layers):
                if layer_id in self.prox_tv:
                    continue
                layer_params = self.parameter_groups[f'layer-{layer_id}']
                initial_parameters_prox = unravel_group_params(
                    {k.split(':', 1)[1]: v for k, v in layer_params.items()
                     if k.startswith('prox:')}
                )
                self._initialize_prox_tv_per_layer(
                    layer_id, layer_params, initial_parameters_prox
                )
        else:
            raise NotImplementedError(
                f"Parameter learn_prox should be in {ALL_LEARN_PROX}. "
                f"Got '{self.learn_prox}'.")

    def get_initial_layer_parameters(self, layer_id):
        layer_params = dict()
        layer_params['Wu'] = self.I_k - self.A.dot(self.A.T) / self.l_
        layer_params['Wx'] = self.A.T / self.l_

        if self.learn_th:
            layer_params['threshold'] = np.array(1.0 / self.l_)

        # Only create a network per-layer if learn_prox is LEARN_PROX_PER_LAYER
        if self.learn_prox == LEARN_PROX_PER_LAYER:
            layer_params = self._initialize_prox_tv_per_layer(
                layer_id, layer_params
            )

        return layer_params

    def _initialize_prox_tv_per_layer(self, layer_id, layer_params,
                                      initial_parameters_prox=None):
        """Create a Lista network to solve the proxTV sub problem.

        Make sure to register correctly its parameter so that the training is
        done properly for all net_solver_type values.
        """
        layer_prox_tv = ListaLASSO(
            A=self.I_k, n_layers=self.n_inner_layers, learn_th=True,
            initial_parameters=initial_parameters_prox,
            name=f"Prox-TV-Lista[layer={layer_id}]", device=self.device
        )
        self.prox_tv[layer_id] = layer_prox_tv
        for k, p in ravel_group_params(
                layer_prox_tv.parameter_groups).items():
            layer_params[f'prox:{k}'] = p
        return layer_params

    def forward(self, x, lbda, output_layer=None):
        """ Forward pass of the network. """
        output_layer = self.check_output_layer(output_layer)

        # initialized variables
        _, u, _ = init_vuz(self.A, self.D, x, inv_A=self.inv_A_,
                           device=self.device)

        for layer_id in range(output_layer):
            layer_params = self.parameter_groups[f'layer-{layer_id}']
            # retrieve parameters
            Wx = layer_params['Wx']
            Wu = layer_params['Wu']

            mul_lbda = layer_params.get('threshold', 1.0 / self.l_)
            mul_lbda = check_tensor(mul_lbda, device=self.device)
            if self.learn_prox == LEARN_PROX_PER_LAYER:
                prox_tv = self.prox_tv[layer_id]
            else:
                prox_tv = self.prox_tv

            # apply one 'iteration'. We need an extra integration step as
            # prox_tv is a synthesis algorithm which outputs the synthesis
            # variable z and not the analysis one u.
            u = u.matmul(Wu) + x.matmul(Wx)
            z = prox_tv(x=u, lbda=lbda * mul_lbda)
            u = torch.cumsum(z, dim=1)

        return u


class LpgdTautString(_ListaAnalysis):
    __doc__ = DOC_LISTA.format(
        type='learned-PGD with taut-string for prox operator',
        problem_name='TV',
        descr='unconstrained parametrization'
    )

    def __init__(self, A, n_layers, learn_th=False, use_moreau=True,
                 max_iter=100, net_solver_type="recursive",
                 initial_parameters=None, name="LPGD - Taut-string",
                 verbose=0, device=None):
        if device is not None and 'cuda' in device:
            import warnings
            warnings.warn("Cannot use LpgdTautString on cuda device. "
                          "Falling back to CPU.")
            device = 'cpu'

        self.use_moreau = use_moreau

        n_atoms = A.shape[0]
        self.A = np.array(A)
        self.I_k = np.eye(n_atoms)
        self.D = (np.eye(n_atoms, k=-1) - np.eye(n_atoms, k=0))[:, :-1]

        self.A_ = check_tensor(self.A, device=device)
        self.inv_A_ = torch.pinverse(self.A_)
        self.l_ = np.linalg.norm(self.A, ord=2) ** 2

        super().__init__(n_layers=n_layers, learn_th=learn_th,
                         max_iter=max_iter, net_solver_type=net_solver_type,
                         initial_parameters=initial_parameters, name=name,
                         verbose=verbose, device=device)

    def get_initial_layer_parameters(self, layer_id):
        layer_params = dict()
        layer_params['Wu'] = self.I_k - self.A.dot(self.A.T) / self.l_
        layer_params['Wx'] = self.A.T / self.l_
        if self.learn_th:
            layer_params['threshold'] = np.array(1.0 / self.l_)
        return layer_params

    def forward(self, x, lbda, output_layer=None):
        """ Forward pass of the network. """
        output_layer = self.check_output_layer(output_layer)

        # initialized variables
        _, u, _ = init_vuz(self.A, self.D, x, inv_A=self.inv_A_,
                           device=self.device)
        mul_lbda = check_tensor(1.0 / self.l_, device=self.device)

        for layer_id in range(output_layer):
            layer_params = self.parameter_groups[f'layer-{layer_id}']
            # retrieve parameters
            Wx = layer_params['Wx']
            Wu = layer_params['Wu']
            if self.learn_th:
                mul_lbda = layer_params['threshold']

            # apply one 'iteration'
            u = u.matmul(Wu) + x.matmul(Wx)
            u = ProxTV_l1.apply(u, lbda * mul_lbda)

        return u


class CoupledCondatVu(_ListaAnalysis):
    __doc__ = DOC_LISTA.format(
        type='learned-Condat-Vu-coupled',
        problem_name='TV',
        descr='one weight parametrization from Chen et al (2018)'
    )

    def __init__(self, A, n_layers, learn_th=True, max_iter=100,
                 use_moreau=False, net_solver_type="recursive",
                 initial_parameters=None, name="learned-Condat-Vu-coupled",
                 verbose=0, device=None):
        self.use_moreau = use_moreau

        n_atoms = A.shape[0]
        self.A = np.array(A)
        self.D = (np.eye(n_atoms, k=-1) - np.eye(n_atoms, k=0))[:, :-1]

        self.A_ = check_tensor(self.A, device=device)
        self.D_ = check_tensor(self.D, device=device)
        self.inv_A_ = torch.pinverse(self.A_)

        self.l_A = np.linalg.norm(self.A, ord=2) ** 2
        self.l_D = np.linalg.norm(self.D, ord=2) ** 2

        # Condat-Vu parameters
        self.rho = 1.0
        self.sigma = 0.5
        self.tau = 1.0 / (self.l_A / 2.0 + self.sigma * self.l_D**2)

        super().__init__(n_layers=n_layers, learn_th=learn_th,
                         max_iter=max_iter, net_solver_type=net_solver_type,
                         initial_parameters=initial_parameters, name=name,
                         verbose=verbose, device=device)

    def get_initial_layer_parameters(self, layer_id):
        layer_params = dict(W_coupled=self.D)
        if self.learn_th:
            layer_params['threshold'] = np.array(1.0)
        return layer_params

    def forward(self, x, lbda, output_layer=None):
        """ Forward pass of the network. """
        output_layer = self.check_output_layer(output_layer)

        # initialized variables
        v, u, _ = init_vuz(self.A, self.D, x, inv_A=self.inv_A_,
                           device=self.device)
        v_old, u_old = v.clone(), u.clone()

        for layer_id in range(output_layer):
            layer_params = self.parameter_groups[f'layer-{layer_id}']
            # retrieve parameters
            W = layer_params['W_coupled']
            mul_lbda = layer_params.get('threshold', 1.0)
            mul_lbda = check_tensor(mul_lbda, device=self.device)
            sigma = self.sigma
            tau = self.tau
            rho = self.rho

            # primal descent
            u_new = u_old + (
                - tau * (u_old.matmul(self.A_) - x).matmul(self.A_.t())
                - tau * v_old.matmul(W.t()))
            # dual ascent
            v_ = v_old + sigma * (2 * u_new - u_old).matmul(W)
            v_new = v_ - sigma * pseudo_soft_th_tensor(
                v_ / sigma, lbda * mul_lbda, 1.0 / sigma)
            # update
            u = rho * u_new + (1.0 - rho) * u_old
            v = rho * v_new + (1.0 - rho) * v_old
            # storing
            u_old = u
            v_old = v

        return u


class StepCondatVu(_ListaAnalysis):
    __doc__ = DOC_LISTA.format(
        type='learned-Condat-Vu-step',
        problem_name='TV',
        descr='Primal and dual step learn'
    )

    def __init__(self, A, n_layers, learn_th=False, max_iter=100,
                 use_moreau=False, net_solver_type="recursive",
                 initial_parameters=None, name="learned-Condat-Vu-step",
                 verbose=0, device=None):
        self.use_moreau = use_moreau

        n_atoms = A.shape[0]
        self.A = np.array(A)
        self.D = (np.eye(n_atoms, k=-1) - np.eye(n_atoms, k=0))[:, :-1]

        self.A_ = check_tensor(self.A, device=device)
        self.D_ = check_tensor(self.D, device=device)
        self.inv_A_ = torch.pinverse(self.A_)

        self.l_A = np.linalg.norm(self.A, ord=2) ** 2
        self.l_D = np.linalg.norm(self.D, ord=2) ** 2

        # Parameter for accelerated condat-vu algorithm
        self.rho = 1.0

        if learn_th:
            print("In StepIstaLASSO learn_th can't be enable, ignore it.")

        super().__init__(n_layers=n_layers, learn_th=False,
                         max_iter=max_iter, net_solver_type=net_solver_type,
                         initial_parameters=initial_parameters, name=name,
                         verbose=verbose, device=device)

    def get_initial_layer_parameters(self, layer_id):
        return dict(sigma=np.array(.5))

    def forward(self, x, lbda, output_layer=None):
        """ Forward pass of the network. """
        output_layer = self.check_output_layer(output_layer)

        # initialized variables
        v, u, _ = init_vuz(self.A, self.D, x, inv_A=self.inv_A_,
                           device=self.device)
        v_old, u_old, _ = init_vuz(self.A, self.D, x, device=self.device)

        for layer_id in range(output_layer):
            layer_params = self.parameter_groups[f'layer-{layer_id}']
            # retrieve parameters
            sigma = layer_params['sigma']
            sigma = torch.clamp(sigma, 0.5, 2.0)  # TODO constraint learning
            tau = 1.0 / (self.l_A / 2.0 + sigma * self.l_D**2)
            rho = self.rho

            # primal descent
            u_new = u_old + (
                - tau * (u_old.matmul(self.A_) - x).matmul(self.A_.t())
                - tau * v_old.matmul(self.D_.t()))
            # dual ascent
            v_ = v_old + sigma * (2 * u_new - u_old).matmul(self.D_)
            v_new = v_ - sigma * pseudo_soft_th_tensor(
                v_ / sigma, lbda, 1.0 / sigma)
            # update
            u = rho * u_new + (1.0 - rho) * u_old
            v = rho * v_new + (1.0 - rho) * v_old
            # storing
            u_old = u
            v_old = v

        return u


class StepSubGradTV(_ListaAnalysis):
    __doc__ = DOC_LISTA.format(
        type='learned-TV step',
        problem_name='TV',
        descr='only learn a step size'
    )

    def __init__(self, A, n_layers, learn_th=True, use_moreau=False,
                 max_iter=100, net_solver_type="recursive",
                 initial_parameters=None, name="learned-TV Sub Gradient",
                 verbose=0, device=None):
        self.use_moreau = use_moreau

        n_atoms = A.shape[0]
        self.A = np.array(A)
        self.D = (np.eye(n_atoms, k=-1) - np.eye(n_atoms, k=0))[:, :-1]

        self.A_ = check_tensor(self.A, device=device)
        self.D_ = check_tensor(self.D, device=device)
        self.inv_A_ = torch.pinverse(self.A_)

        if learn_th:
            print("In StepSubGradTV learn_th can't be enable, ignore it.")

        super().__init__(n_layers=n_layers, learn_th=False,
                         max_iter=max_iter, net_solver_type=net_solver_type,
                         initial_parameters=initial_parameters, name=name,
                         verbose=verbose, device=device)

    def get_initial_layer_parameters(self, layer_id):
        init_step_size = 1e-10
        return dict(step_size=np.array(init_step_size))

    def forward(self, x, lbda, output_layer=None):
        """ Forward pass of the network. """
        output_layer = self.check_output_layer(output_layer)

        # initialized variables
        _, u, _ = init_vuz(self.A, self.D, x, device=self.device)

        for layer_id in range(output_layer):
            layer_params = self.parameter_groups[f'layer-{layer_id}']
            # retrieve parameters
            step_size = layer_params['step_size']

            # apply one 'iteration'
            residual = (u.matmul(self.A_) - x).matmul(self.A_.t())
            reg = u.matmul(self.D_).sign().matmul(self.D_.t())
            grad = residual + lbda * reg
            u = u - step_size * grad

        return u
