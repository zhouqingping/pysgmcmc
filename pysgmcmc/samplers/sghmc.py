# vim: foldmethod=marker
import tensorflow as tf
from pysgmcmc.sampling import BurnInMCMCSampler

# safe operations for division and square root
from pysgmcmc.tensor_utils import (
    vectorize, unvectorize, safe_divide, safe_sqrt,
)


class SGHMCSampler(BurnInMCMCSampler):
    """ Stochastic Gradient Hamiltonian Monte-Carlo Sampler that uses a burn-in
        procedure to adapt its own hyperparameters during the initial stages
        of sampling.

        See [1] for more details on this burn-in procedure.
        See [2] for more details on Stochastic Gradient Hamiltonian Monte-Carlo.

        [1] J. T. Springenberg, A. Klein, S. Falkner, F. Hutter
            Bayesian Optimization with Robust Bayesian Neural Networks.
            In Advances in Neural Information Processing Systems 29 (2016).

        [2] T. Chen, E. B. Fox, C. Guestrin
            Stochastic Gradient Hamiltonian Monte Carlo
            In Proceedings of Machine Learning Research 32 (2014).
    """

    def __init__(self, params, cost_fun, seed=None, batch_generator=None, epsilon=0.01,
                 session=tf.get_default_session(), burn_in_steps=3000,
                 scale_grad=1.0, dtype=tf.float64, mdecay=0.05):
        """ Initialize the sampler parameters and set up a tensorflow.Graph
            for later queries.

        Parameters
        ----------
        params : list of tensorflow.Variable objects
            Target parameters for which we want to sample new values.

        cost_fun : callable
            Function that takes `params` as input and returns a
            1-d `tensorflow.Tensor` that contains the cost-value.
            Frequently denoted with `U` in literature.

        seed : int, optional
            Random seed to use.
            Defaults to `None`.

        batch_generator : BatchGenerator, optional
            Iterable which returns dictionaries to feed into
            tensorflow.Session.run() calls to evaluate the cost function.
            Defaults to `None` which indicates that no batches shall be fed.

        epsilon : float, optional
            Value that is used as learning rate parameter for the sampler,
            also denoted as discretization parameter in literature.
            Defaults to `0.01`.

        session : tensorflow.Session, optional
            Session object which knows about the external part of the graph
            (which defines `Cost`, and possibly batches).
            Used internally to evaluate (burn-in/sample) the sampler.

        burn_in_steps: int, optional
            Number of burn-in steps to perform. In each burn-in step, this
            sampler will adapt its own internal parameters to decrease its error.
            For reference see: TODO ADD PAPER REFERENCE HERE

        scale_grad : float, optional
            Value that is used to scale the magnitude of the noise used
            during sampling. In a typical batches-of-data setting this usually
            corresponds to the number of examples in the entire dataset.
            Defaults to `1.0` which corresponds to no scaling.

        dtype : tensorflow.DType, optional
            Type of elements of `tensorflow.Tensor` objects used in this sampler.
            Defaults to `tensorflow.float64`.

        mdecay : float, optional
            (Constant) momentum decay per time-step.
            Defaults to `0.05`.

        Examples
        ----------
        Simple, plain example:
        TODO: Add 2D Gaussian Case here

        Simple example that uses batches:
        TODO: Add simplified batch example here

        See Also
        ----------
        tensorflow_mcmc.sampling.mcmc_base_classes.BurnInMCMCSampler:
            Base class for `SGHMCSampler` that specifies how actual sampling
            is performed (using iterator protocol, e.g. `next(sampler)`).

        """

        # Set up BurnInMCMCSampler base class:
        # initialize member variables common to all samplers
        # and run initializers for all uninitialized variables in `params`
        # (to avoid errors in the graph definitions below).
        super().__init__(params=params, burn_in_steps=burn_in_steps,
                         seed=seed, batch_generator=batch_generator,
                         dtype=dtype, session=session)

        #  Initialize graph constants {{{ #

        Noise = tf.constant(0., name="noise", dtype=dtype)
        Epsilon = tf.constant(epsilon, name="epsilon", dtype=dtype)

        Scale_grad = tf.constant(scale_grad, dtype=dtype, name="scale_grad")
        Epsilon_scaled = tf.divide(Epsilon, tf.sqrt(Scale_grad), name="epsilon_scaled")

        Mdecay = tf.constant(mdecay, name="mdecay", dtype=dtype)

        #  }}} Initialize graph constants #

        self.Cost = cost_fun(*params)

        grads = [vectorize(gradient) for gradient in
                 tf.gradients(self.Cost, params)]

        #  Initialize internal sampler parameters {{{ #

        Tau = [tf.Variable(tf.ones_like(Param, dtype=dtype),
                           dtype=dtype, name="Tau_{}".format(i),
                           trainable=False)
               for i, Param in enumerate(self.vectorized_params)]

        R = [tf.Variable(1. / (Tau[i].initialized_value() + 1),
                         name="R_{}".format(i), trainable=False)
             for i, Param in enumerate(self.vectorized_params)]

        G = [tf.Variable(tf.ones_like(Param, dtype=dtype),
                         dtype=dtype, name="G_{}".format(i),
                         trainable=False)
             for i, Param in enumerate(self.vectorized_params)]

        V_hat = [tf.Variable(tf.ones_like(Param, dtype=dtype),
                             dtype=dtype, name="V_hat_{}".format(i),
                             trainable=False)
                 for i, Param in enumerate(self.vectorized_params)]

        # Initialize Mass matrix inverse
        self.Minv = [tf.Variable(tf.divide(tf.constant(1., dtype=dtype),
                                 tf.sqrt(V_hat[i].initialized_value())),
                                 name="Minv_{}".format(i), trainable=False)
                     for i, Param in enumerate(self.vectorized_params)]

        # Initialize momentum
        V = [tf.Variable(tf.zeros_like(Param, dtype=dtype),
                         dtype=dtype, name="V_{}".format(i),
                         trainable=False)
             for i, Param in enumerate(self.vectorized_params)]

        #  }}} Initialize internal sampler parameters #

        self.Minv_t = [None] * len(params)  # gets burned-in XXX Move to BurnInMCMCSampler?

        # R_t = 1/ (Tau + 1), shouldn't it be: 1 / tau according to terms?
        # It is not, and changing it to that breaks everything!
        # Why?

        for i, (Param, Grad) in enumerate(zip(params, grads)):
            Vectorized_Param = self.vectorized_params[i]
            #  Burn-in logic {{{ #
            R_t = tf.assign(R[i], 1. / (Tau[i] + 1), name="R_t_{}".format(i))

            # R_t should always use the old value of Tau
            with tf.control_dependencies([R_t]):
                Tau_t = tf.assign_add(
                    Tau[i],
                    safe_divide(-G[i] * G[i] * Tau[i], V_hat[i]) + 1,
                    name="Tau_t_{}".format(i)
                )

                # Minv = V_hat^{-1/2} = 1 / sqrt(V_hat)
                self.Minv_t[i] = tf.assign(
                    self.Minv[i],
                    safe_divide(1., safe_sqrt(V_hat[i])),
                    name="Minv_t_{}".format(i)
                )
                # Tau_t, Minv_t should always use the old values of G, V_hat
                with tf.control_dependencies([Tau_t, self.Minv_t[i]]):
                    G_t = tf.assign_add(
                        G[i],
                        -R_t * G[i] + R_t * Grad,
                        name="G_t_{}".format(i)
                    )

                    V_hat_t = tf.assign_add(
                        V_hat[i],
                        - R_t * V_hat[i] + R_t * Grad ** 2,
                        name="V_hat_t_{}".format(i)
                    )

            #  }}} Burn-in logic #

                    with tf.control_dependencies([G_t, V_hat_t]):

                        #  Draw random normal sample {{{ #

                        # Equation 10, variance of normal sample

                        # 2 * epsilon ** 2 * mdecay * Minv - 0 (noise is 0) - epsilon ** 4
                        # = 2 * epsilon ** 2 * epsilon * V_hat^{-1/2} * C * Minv
                        # = 2 * epsilon ** 3 * V_hat^{-1/2} * C * V_hat^{-1/2} - epsilon ** 4

                        # (co-) variance of normal sample
                        Noise_scale = (
                            tf.constant(2., dtype=dtype) *
                            Epsilon_scaled ** tf.constant(2., dtype=dtype) *
                            Mdecay * self.Minv_t[i] - tf.constant(2., dtype=dtype) *
                            Epsilon_scaled ** tf.constant(3., dtype) *
                            tf.square(self.Minv_t[i]) * Noise - Epsilon_scaled ** 4
                        )

                        # turn into stddev
                        Sigma = tf.sqrt(tf.maximum(Noise_scale, 1e-16),
                                        name="Sigma_{}".format(i))

                        Sample = self._draw_noise_sample(Sigma=Sigma,
                                                         Shape=Vectorized_Param.shape)

                        #  }}} Draw random sample #

                        #  HMC Update {{{ #

                        # Equation 10: right side, where:
                        # Minv = V_hat^{-1/2}, Mdecay = epsilon * V_hat^{-1/2} C
                        V_t = tf.assign_add(
                            V[i],
                            - Epsilon ** 2 * self.Minv_t[i] * Grad -
                            Mdecay * V[i] + Sample,
                            name="V_t_{}".format(i)
                        )

                        # Equation 10: left side
                        Vectorized_Theta_t = tf.assign_add(
                            Vectorized_Param, V_t
                        )

                        self.Theta_t[i] = tf.assign(
                            Param,
                            unvectorize(
                                Vectorized_Theta_t, original_shape=Param.shape
                            ),
                            name="Theta_t_{}".format(i)
                        )

                        #  }}} HMC Update #