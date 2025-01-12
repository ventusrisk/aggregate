import scipy.stats as ss
import numpy as np
from matplotlib import pyplot as plt
import matplotlib.ticker as ticker
from scipy.integrate import quad
import pandas as pd
import collections
import logging
import json
from .utils import sln_fit, sgamma_fit, ft, ift, \
    axiter_factory, estimate_agg_percentile, suptitle_and_tight, html_title, \
    MomentAggregator, xsden_to_meancv, round_bucket, make_ceder_netter, MomentWrangler
from .spectral import Distortion
from scipy import interpolate
from scipy.optimize import newton
from IPython.core.display import display
from scipy.special import kv, gammaln, hyp1f1
from scipy.optimize import broyden2, newton_krylov
from scipy.optimize.nonlin import NoConvergence
from scipy.interpolate import interp1d
import itertools
from collections import namedtuple
from numpy.linalg import inv  # , pinv, det, matrix_rank

# from types import MethodType
# from scipy.special import ndtri  # inverse normal - this actually does the work

logger = logging.getLogger('aggregate')


class Frequency(object):
    """
    Manages Frequency distributions: creates moment function and MGF.

    freq_moms(n): returns EN, EN^2 and EN^3 when EN=n

    mgf(n, z): returns the moment generating function applied to z when EN=n

    **Available Frequency Distributions**

    **Non-Mixture** Types

    * ``fixed``: no parameters
    * ``bernoulli``: exp_en interpreted as a probability, must be < 1
    * ``binomial``: Binomial(n, p) where p = freq_a, and n = exp_en
    * ``poisson``: Poisson(freq_a)
    * ``poisson``: geometric(freq_a)
    * ``pascal``: pascal-poisson distribution, a poisson stopped sum of negative binomial; exp_en gives the overall
      claim count. freq_a is the CV of the negative binomial distribution and freq_b is the
      number of claimants per claim (or claims per occurrence). Hence the Poisson component
      has mean exp_en / freq_b and the number of claims per occurrence has mean freq_b and
      cv freq_a

    **Mixture** Types

    These distributions are G-mixed Poisson, so N | G ~ Poisson(n G). They are labelled by
    the name of the mixing distribution or the common name for the resulting frequency
    distribution. See Panjer and Willmot or JKK.

    In all cases freq_a is the CV of the mixing distribution which corresponds to the
    asympototic CV of the frequency distribution and of any aggregate when the severity has a variance.

    * ``gamma``: negative binomial, freq_a = cv of gamma distribution
    * ``delaporte``: shifted gamma, freq_a = cv of mixing disitribution, freq_b = proportion of
      certain claims = shift. freq_b must be between 0 and 1.
    * ``ig``: inverse gaussian, freq_a = cv of mixing distribution
    * ``sig``: shifted inverse gaussian, freq_a = cv of mixing disitribution, freq_b = proportion of
      certain claims = shift. freq_b must be between 0 and 1.
    * ``sichel``: generalized inverse gaussian mixing distribution, freq_a = cv of mixing distribution and
      freq_b = lambda value. The beta and mu parameters solved to match moments. Note lambda =
      -0.5 corresponds to inverse gaussian and 0.5 to reciprocal inverse gauusian. Other special
      cases are available.
    * ``sichel.gamma``: generalized inverse gaussian mixture where the parameters match the moments of a
      delaporte distribution with given freq_a and freq_b
    * ``sichel.ig``: generalized inverse gaussian mixture where the parameters match the moments of a
      shifted inverse gaussian distribution with given freq_a and freq_b. This parameterization
      has poor numerical stability and may fail.
    * ``beta``: beta mixing with freq_a = Cv where beta is supported on the interval [0, freq_b]. This
      method should be used carefully. It has poor numerical stability and can produce bizzare
      aggregates when the alpha or beta parameters are < 1 (so there is a mode at 0 or freq_b).

    :param freq_name:
    :param freq_a:
    :param freq_b:

    """

    __slots__ = ['freq_moms', 'mgf', 'freq_name', 'freq_a', 'freq_b']

    def __init__(self, freq_name, freq_a, freq_b):
        """
        creates the mgf and moment function

        moment function(n) returns EN, EN^2, EN^3 when EN=n

        mgf(n, z) is the mgf evaluated at log(z) when EN=n

        """
        self.freq_name = freq_name
        self.freq_a = freq_a
        self.freq_b = freq_b
        logger.debug(
            f'Frequency.__init__ | creating new Frequency {self.freq_name} at {super(Frequency, self).__repr__()}')

        if self.freq_name == 'fixed':
            def _freq_moms(n):
                # fixed distribution N=n certainly
                freq_2 = n ** 2
                freq_3 = n * freq_2
                return n, freq_2, freq_3

            def mgf(n, z):
                return z ** n

        elif self.freq_name == 'bernoulli':
            def _freq_moms(n):
                # code for bernoulli n, E(N^k) = E(N) = n
                # n in this case only means probability of claim (=expected claim count)
                freq_2 = n
                freq_3 = n
                return n, freq_2, freq_3

            def mgf(n, z):
                # E(e^tlog(z)) = p z + (1-p), z = ft(severity)
                return z * n + np.ones_like(z) * (1 - n)

        elif self.freq_name == 'binomial':
            def _freq_moms(n):
                # binomial(N, p) with mean n, N=n/p
                # http://mathworld.wolfram.com/BinomialDistribution.html
                p = self.freq_a
                N = n / p  # correct mean
                freq_1 = N * p
                freq_2 = N * p * (1 - p + N * p)
                freq_3 = N * p * (1 + p * (N - 1) * (3 + p * (N - 2)))
                return freq_1, freq_2, freq_3

            def mgf(n, z):
                N = n / self.freq_a
                return (z * self.freq_a + np.ones_like(z) * (1 - self.freq_a)) ** N

        elif self.freq_name == 'poisson' and self.freq_a == 0:
            def _freq_moms(n):
                # Poisson
                freq_2 = n * (1 + n)
                freq_3 = n * (1 + n * (3 + n))
                return n, freq_2, freq_3

            def mgf(n, z):
                return np.exp(n * (z - 1))

        elif self.freq_name == 'geometric' and self.freq_a == 0:
            # as for poisson, single parameter
            # https://mathworld.wolfram.com/GeometricDistribution.html and Wikipedia
            # e.g. tester: agg =uw('agg GEOM 3 claims sev dhistogram xps [1] [1] geometric')
            def _freq_moms(n):
                p = 1 / (n + 1)
                freq_2 = (2 - p) * (1 - p) / p ** 2
                freq_3 = (1 - p) * (6 + (p - 6) * p) / p ** 3
                return n, freq_2, freq_3

            def mgf(n, z):
                p = 1 / (n + 1)
                return p / (1 - (1 - p) * z)

        elif self.freq_name == 'pascal':
            # solve for local c to hit overall c=ν^2 value input
            ν = self.freq_a  # desired overall cv
            κ = self.freq_b  # claims per occurrence

            def _freq_moms(n):
                c = (n * ν ** 2 - 1 - κ) / κ
                # a = 1 / c
                # θ = κ * c
                λ = n / κ  # poisson parameter for number of claims
                g = κ * λ * (
                        2 * c ** 2 * κ ** 2 + 3 * c * κ ** 2 * λ + 3 * c * κ ** 2 + 3 * c * κ + κ ** 2 * λ ** 2 +
                        3 * κ ** 2 * λ + κ ** 2 + 3 * κ * λ + 3 * κ + 1)
                return n, n * (κ * (1 + c + λ) + 1), g

            def mgf(n, z):
                c = (n * ν ** 2 - 1 - κ) / κ
                a = 1 / c
                θ = κ * c
                λ = n / κ  # poisson parameter for number of claims
                return np.exp(λ * ((1 - θ * (z - 1)) ** -a - 1))

        elif self.freq_name == 'empirical':
            # stated en here...need to reach up to agg to set that?!
            # parameters are entered as nps, to a is n values and b is probability masses

            def _freq_moms(n):
                # independent of n, it will be -1
                en = np.sum(self.freq_a * self.freq_b)
                en2 = np.sum(self.freq_a ** 2 * self.freq_b)
                en3 = np.sum(self.freq_a ** 3 * self.freq_b)
                return en, en2, en3

            def mgf(n, z):
                # again, independent of n, not going overboard in method here...
                return self.freq_b @ np.power(z, self.freq_a.reshape((self.freq_a.shape[0], 1)))

        # the remaining options are all mixed poisson ==================================================
        # the factorial moments of the mixed poisson are the noncentral moments of the mixing distribution
        # so for each case we compute the noncentral moments of mix and then convert factorial to non-central
        # the mixing distributions have mean 1 so they can be scaled as appropriate
        # they all use the same f
        elif self.freq_name == 'gamma':
            # gamma parameters a (shape) and  theta (scale)
            # a = 1/c, theta = c
            c = self.freq_a * self.freq_a
            a = 1 / c
            θ = c
            g = 1 + 3 * c + 2 * c * c

            def _freq_moms(n):
                freq_2 = n * (1 + (1 + c) * n)
                freq_3 = n * (1 + n * (3 * (1 + c) + n * g))
                return n, freq_2, freq_3

            def mgf(n, z):
                return (1 - θ * n * (z - 1)) ** -a

        elif self.freq_name == 'delaporte':
            # shifted gamma, freq_a is CV mixing and freq_b  = proportion of certain claims (f for fixed claims)
            ν = self.freq_a
            c = ν * ν
            f = self.freq_b
            # parameters of mixing distribution (excluding the n)
            a = (1 - f) ** 2 / c
            θ = (1 - f) / a
            g = 2 * ν ** 4 / (1 - f) + 3 * c + 1

            def _freq_moms(n):
                freq_2 = n * (1 + (1 + c) * n)
                freq_3 = n * (1 + n * (3 * (1 + c) + n * g))
                return n, freq_2, freq_3

            def mgf(n, z):
                return np.exp(f * n * (z - 1)) * (1 - θ * n * (z - 1)) ** -a

        elif self.freq_name == 'ig':
            # inverse Gaussian distribution
            ν = self.freq_a
            c = ν ** 2
            μ = c
            λ = 1 / μ
            # skewness and E(G^3)
            γ = 3 * np.sqrt(μ)
            g = γ * ν ** 3 + 3 * c + 1

            def _freq_moms(n):
                freq_2 = n * (1 + (1 + c) * n)
                freq_3 = n * (1 + n * (3 * (1 + c) + n * g))
                return n, freq_2, freq_3

            def mgf(n, z):
                return np.exp(1 / μ * (1 - np.sqrt(1 - 2 * μ ** 2 * λ * n * (z - 1))))

        elif self.freq_name == 'sig':
            # shifted pig with a proportion of certain claims
            ν = self.freq_a
            f = self.freq_b
            c = ν * ν  # contagion
            μ = c / (1 - f) ** 2
            λ = (1 - f) / μ
            γ = 3 * np.sqrt(μ)
            g = γ * ν ** 3 + 3 * c + 1

            def _freq_moms(n):
                freq_2 = n * (1 + (1 + c) * n)
                freq_3 = n * (1 + n * (3 * (1 + c) + n * g))
                return n, freq_2, freq_3

            def mgf(n, z):
                return np.exp(f * n * (z - 1)) * np.exp(1 / μ * (1 - np.sqrt(1 - 2 * μ ** 2 * λ * n * (z - 1))))

        elif self.freq_name == 'beta':
            # beta-Poisson mixture [0, b] with mean 1 and cv ν
            # warning: numerically unstable
            ν = self.freq_a  # cv of beta
            c = ν * ν
            r = self.freq_b  # rhs of beta which must be > 1 for mean to equal 1
            assert r > 1

            # mean = a / (a + b) = n / r, var = a x b / [(a + b)^2( a + b + 1)] = c x mean

            def _freq_moms(n):
                b = (r - n * (1 + c)) * (r - n) / (c * n * r)
                a = n / (r - n) * b
                g = r ** 3 * np.exp(gammaln(a + b) + gammaln(a + 3) - gammaln(a + b + 3) - gammaln(a))
                freq_2 = n * (1 + (1 + c) * n)
                freq_3 = n * (1 + n * (3 * (1 + c) + n * g))
                return n, freq_2, freq_3

            def mgf(n, z):
                b = (r - n * (1 + c)) * (r - n) / (c * n * r)
                a = (r - n * (1 + c)) / (c * r)
                return hyp1f1(a, a + b, r * (z - 1))

        elif self.freq_name[0:6] == 'sichel':
            # flavors: sichel.gamma = match to delaporte moments, .ig = match to spig moments (not very numerically
            # stable)
            # sichel: treat freq_b as lambda
            _type = self.freq_name.split('.')
            add_sichel = True
            ν = self.freq_a
            c = ν * ν
            if len(_type) > 1:
                # .gamma or .ig forms
                f = self.freq_b
                λ = -0.5
                μ = 1
                β = ν ** 2
                if _type[1] == 'gamma':
                    # sichel_case 2: match delaporte moments
                    # G = f + G'; E(G') = 1 - f, SD(G) = SD(G') = ν, skew(G') = skew(G)
                    # a = ((1 - f) / ν) ** 2
                    # FWIW θ = ν / (1 - f)  # (1 - f) / a
                    target = np.array([1, ν, 2 * ν / (1 - f)])  # / np.sqrt(a)])
                elif _type[1] == 'ig':
                    # match shifted IG moments
                    # μ = (ν / (1 - f)) ** 2
                    target = np.array([1, ν, 3.0 * ν / (1 - f)])  # np.sqrt(μ)])
                else:
                    raise ValueError(f'Inadmissible frequency type {self.freq_name}...')

                def f(arrIn):
                    """
                    calibration function to match target mean, cv and skewness (keeps the scale about the same)
                    :param arrIn:
                    :return:
                    """
                    μ, β, λ = arrIn
                    # mu and beta are positive...
                    μ = np.exp(μ)
                    β = np.exp(β)
                    ex1, ex2, ex3 = np.array([μ ** r * kv(λ + r, μ / β) / kv(λ, μ / β) for r in (1, 2, 3)])
                    sd = np.sqrt(ex2 - ex1 * ex1)
                    skew = (ex3 - 3 * ex2 * ex1 + 2 * ex1 ** 3) / (sd ** 3)
                    return np.array([ex1, sd, skew]) - target

                try:
                    params = broyden2(f, (np.log(μ), np.log(β), λ), verbose=False, iter=10000,
                                      f_rtol=1e-11)  # , f_rtol=1e-9)  , line_search='wolfe'
                    if np.linalg.norm(params) > 20:
                        λ = -0.5
                        μ = 1
                        β = ν ** 2
                        params1 = newton_krylov(f, (np.log(μ), np.log(β), λ), verbose=False, iter=10000, f_rtol=1e-11)
                        logger.warning(
                            f'Frequency.__init__ | {self.freq_name} type Broyden gave large result {params},'
                            f'Newton Krylov {params1}')
                        if np.linalg.norm(params) > np.linalg.norm(params1):
                            params = params1
                            logger.warning('Frequency.__init__ | using Newton K')
                except NoConvergence as e:
                    print('ERROR: broyden did not converge')
                    print(e)
                    add_sichel = False
                    raise e
            else:
                # pure sichel, match cv and use
                λ = self.freq_b
                target = np.array([1, ν])
                μ = 1
                β = ν ** 2

                def f(arrIn):
                    """
                    calibration function to match target mean = 1 and cv
                    :param arrIn:
                    :return:
                    """
                    μ, β = arrIn
                    # mu and beta are positive...
                    μ = np.exp(μ)
                    β = np.exp(β)
                    ex1, ex2 = np.array([μ ** r * kv(λ + r, μ / β) / kv(λ, μ / β) for r in (1, 2)])
                    sd = np.sqrt(ex2 - ex1 * ex1)
                    return np.array([ex1, sd]) - target

                try:
                    params = broyden2(f, (np.log(μ), np.log(β)), verbose=False, iter=10000,
                                      f_rtol=1e-11)  # , f_rtol=1e-9)  , line_search='wolfe'

                except NoConvergence as e:
                    print('ERROR: broyden did not converge')
                    print(e)
                    add_sichel = False
                    raise e

            # if parameters found...
            logger.debug(f'{self.freq_name} type, params from Broyden {params}')
            if add_sichel:
                if len(_type) == 1:
                    μ, β = params
                else:
                    μ, β, λ = params
                μ, β = np.exp(μ), np.exp(β)
                g = μ ** 2 * kv(λ + 2, μ / β) / kv(λ, μ / β)

                def _freq_moms(n):
                    freq_2 = n * (1 + (1 + c) * n)
                    freq_3 = n * (1 + n * (3 * (1 + c) + n * g))
                    return n, freq_2, freq_3

                def mgf(n, z):
                    kernel = n * (z - 1)
                    inner = np.sqrt(1 - 2 * β * kernel)
                    return inner ** (-λ) * kv(λ, μ * inner / β) / kv(λ, μ / β)

        else:
            raise ValueError(f'Inadmissible frequency type {self.freq_name}...')

        self.freq_moms = _freq_moms
        self.mgf = mgf

    def __str__(self):
        """
        wrap default with name
        :return:
        """
        return f'Frequency object of type {self.freq_name}\n{super(Frequency, self).__repr__()}'


class Aggregate(Frequency):
    """
    Aggregate distribution class manages creation and calculation of aggregate distributions.
        Aggregate allows for very flexible creation of Aggregate distributions. Severity
        can express a limit profile, a mixed severity or both. Mixed frequency types share
        a mixing distribution across all broadcast terms to ensure an appropriate inter-
        class correlation.

    Limit Profiles
        The exposure variables can be vectors to express a *limit profile*.
        All ```exp_[en|prem|loss|count]``` related elements are broadcast against one-another.
        For example

        ::

        [100 200 400 100] premium at 0.65 lr [1000 2000 5000 10000] xs 1000

        expresses a limit profile with 100 of premium at 1000 x 1000; 200 at 2000 x 1000
        400 at 5000 x 1000 and 100 at 10000 x 1000. In this case all the loss ratios are
        the same, but they could vary too, as could the attachments.

    Mixtures
        The severity variables can be vectors to express a *mixed severity*. All ``sev_``
        elements are broadcast against one-another. For example

        ::

            sev lognorm 1000 cv [0.75 1.0 1.25 1.5 2] wts [0.4, 0.2, 0.1, 0.1, 0.1]

        expresses a mixture of five lognormals with a mean of 1000 and CVs as indicated with
        weights 0.4, 0.2, 0.1, 0.1, 0.1. Equal weights can be express as wts=[5], or the
        relevant number of components.

    Limit Profiles and Mixtures
        Limit profiles and mixtures can be combined. Each mixed severity is applied to each
        limit profile component. For example

        ::

            ag = uw('agg multiExp [10 20 30] claims [100 200 75] xs [0 50 75]
                sev lognorm 100 cv [1 2] wts [.6 .4] mixed gamma 0.4')```

        creates an aggregate with six severity subcomponents

        +---+-------+------------+--------+
        | # | limit | attachment | claims |
        +===+=======+============+========+
        | 0 | 100   |  0         |  6     |
        +---+-------+------------+--------+
        | 1 | 100   |  0         |  4     |
        +---+-------+------------+--------+
        | 2 | 200   | 50         | 12     |
        +---+-------+------------+--------+
        | 3 | 200   | 50         |  8     |
        +---+-------+------------+--------+
        | 4 |  75   | 75         | 18     |
        +---+-------+------------+--------+
        | 5 |  75   | 75         | 12     |
        +---+-------+------------+--------+

    Circumventing Products
        It is sometimes desirable to enter two or more lines each with a different severity but
        with a shared mixing variable. For example to model the current accident year and a run-
        off reserve, where the current year is gamma mean 100 cv 1 and the reserves are
        larger lognormal mean 150 cv 0.5 claims requires

        ::

            agg MixedPremReserve [100 200] claims sev [gamma lognorm] [100 150] cv [1 0.5] mixed gamma 0.4

        so that the result is not the four-way exposure / severity product but just a two-way
        combination. These two cases are distinguished looking at the total weights. If the weights sum to
        one then the result is an exposure / severity product. If the weights are missing or sum to the number
        of severity components (i.e. are all equal to 1) then the result is a row by row combination.

    Other Programs
        Below are a series of programs illustrating the different ways exposure, frequency and severity can be
        broadcast together, several different types of severity and all the different types of severity.

        ::

            test_string_0 = '''
            # use to create sev and aggs so can illustrate use of sev. and agg. below

            sev sev1 lognorm 10 cv .3

            agg Agg0 1 claim sev lognorm 10 cv .09 fixed

            '''

            test_string_1 = f'''
            agg Agg1  1 claim sev {10*np.exp(-.3**2/2)} @ lognorm .3      fixed note{{sigma=.3 mean=10}}
            agg Agg2  1 claim sev {10*np.exp(-.3**2/2)} @ lognorm .3 # 5  fixed note{{shifted right by 5}}''' \
            '''
            agg Agg3  1 claim sev 10 @ lognorm 0.5 cv .3                  fixed note{mean 0.5 scaled by 10 and cv 0.3}
            agg Agg4  1 claim sev 10 @ lognorm 1 cv .5 + 5                fixed note{shifted right by 5}

            agg Agg5  1 claim sev 10 @ gamma .3                           fixed note{gamma distribution....can use any two parameter scipy.stats distribution plus expon, uniform and normal}
            agg Agg6  1 claim sev 10 @ gamma 1 cv .3 # 5                  fixed note{mean 10 x 1, cv 0.3 shifted right by 5}

            agg Agg7  1 claim sev 2 @ pareto 1.6  # -2                      fixed note{pareto alpha=1.6 lambda=2}
            agg Agg8  1 claim sev 2 @ uniform 5 # 2.5                     fixed note{uniform 2.5 to 12.5}

            agg Agg9  1 claim 10 x  2 sev lognorm 20 cv 1.5               fixed note{10 x 2 layer, 1 claim}
            agg Agg10 10 loss 10 xs 2 sev lognorm 20 cv 1.5               fixed note{10 x 2 layer, total loss 10, derives requency}
            agg Agg11 14 prem at .7    10 x 1 sev lognorm 20 cv 1.5       fixed note{14 prem at .7 lr derive frequency}
            agg Agg11 14 prem at .7 lr 10 x 1 sev lognorm 20 cv 1.5       fixed note{14 prem at .7 lr derive frequency, lr is optional}

            agg Agg12: 14 prem at .7 lr (10 x 1) sev (lognorm 20 cv 1.5)  fixed note{trailing semi and other punct ignored};

            agg Agg13: 1 claim sev 50 @ beta 3 2 # 10 fixed note{scaled and shifted beta, two parameter distribution}
            agg Agg14: 1 claim sev 100 @ expon # 10   fixed note{exponential single parameter, needs scale, optional shift}
            agg Agg15: 1 claim sev 10 @ norm # 50     fixed note{normal is single parameter too, needs scale, optional shift}

            # any scipy.stat distribution taking one parameter can be used; only cts vars supported on R+ make sense
            agg Agg16: 1 claim sev 1 * invgamma 4.07 fixed  note{inverse gamma distribution}

            # mixtures
            agg MixedLine1: 1 claim 25 xs 0 sev lognorm 10                   cv [0.2, 0.4, 0.6, 0.8, 1.0] wts=5             fixed note{equally weighted mixture of 5 lognormals different cvs}
            agg MixedLine2: 1 claim 25 xs 0 sev lognorm [10, 15, 20, 25, 50] cv [0.2, 0.4, 0.6, 0.8, 1.0] wts=5             fixed note{equal weighted mixture of 5 lognormals different cvs and means}
            agg MixedLine3: 1 claim 25 xs 0 sev lognorm 10                   cv [0.2, 0.4, 0.6, 0.8, 1.0] wt [.2, .3, .3, .15, .05]   fixed note{weights scaled to equal 1 if input}

            # limit profile
            agg LimitProfile1: 1 claim [1, 5, 10, 20] xs 0 sev lognorm 10 cv 1.2 wt [.50, .20, .20, .1]   fixed note{maybe input EL by band for wt}
            agg LimitProfile2: 5 claim            20  xs 0 sev lognorm 10 cv 1.2 wt [.50, .20, .20, .1]   fixed note{input EL by band for wt}
            agg LimitProfile3: [10 10 10 10] claims [inf 10 inf 10] xs [0 0 5 5] sev lognorm 10 cv 1.25   fixed note{input counts directly}

            # limits and distribution blend
            agg Blend1 50  claims [5 10 15] x 0         sev lognorm 12 cv [1, 1.5, 3]          fixed note{options all broadcast against one another, 50 claims of each}
            agg Blend2 50  claims [5 10 15] x 0         sev lognorm 12 cv [1, 1.5, 3] wt=3     fixed note{options all broadcast against one another, 50 claims of each}

            agg Blend5cv1  50 claims  5 x 0 sev lognorm 12 cv 1 fixed
            agg Blend10cv1 50 claims 10 x 0 sev lognorm 12 cv 1 fixed
            agg Blend15cv1 50 claims 15 x 0 sev lognorm 12 cv 1 fixed

            agg Blend5cv15  50 claims  5 x 0 sev lognorm 12 cv 1.5 fixed
            agg Blend10cv15 50 claims 10 x 0 sev lognorm 12 cv 1.5 fixed
            agg Blend15cv15 50 claims 15 x 0 sev lognorm 12 cv 1.5 fixed

            # semi colon can be used for newline and backslash works
            agg Blend5cv3  50 claims  5 x 0 sev lognorm 12 cv 3 fixed; agg Blend10cv3 50 claims 10 x 0 sev lognorm 12 cv 3 fixed
            agg Blend15cv3 50 claims 15 x 0 sev \
            lognorm 12 cv 3 fixed

            # not sure if it will broadcast limit profile against severity mixture...
            agg LimitProfile4: [10 30 15 5] claims [inf 10 inf 10] xs [0 0 5 5] sev lognorm 10 cv [1.0, 1.25, 1.5] wts=3  fixed note{input counts directly}
            ''' \
            f'''
            # the logo
            agg logo 1 claim {np.linspace(10, 250, 20)} xs 0 sev lognorm 100 cv 1 fixed'''

            test_string_2 = '''
            # empirical distributions
            agg dHist1 1 claim sev dhistogram xps [1, 10, 40] [.5, .3, .2] fixed     note{discrete histogram}
            agg cHist1 1 claim sev chistogram xps [1, 10, 40] [.5, .3, .2] fixed     note{continuous histogram, guessed right hand endpiont}
            agg cHist2 1 claim sev chistogram xps [1 10 40 45] [.5 .3 .2]  fixed     note{continuous histogram, explicit right hand endpoint, don't need commas}
            agg BodoffWind  1 claim sev dhistogram xps [0,  99] [0.80, 0.20] fixed   note{examples from Bodoffs paper}
            agg BodoffQuake 1 claim sev dhistogram xps [0, 100] [0.95, 0.05] fixed

            # set up fixed sev for future use
            sev One dhistogram xps [1] [1]   note{a certain loss of 1}
            '''

            test_string_3 = '''
            # sev, agg and port: using built in objects [have to exist prior to running program]
            agg ppa:       0.01 * agg.PPAL       note{this is using lmult on aggs, needs a dictionary specification to adjust means}
            agg cautoQS:   1e-5 * agg.CAL        note{lmult is quota share or scale for rmul see below }
            agg cautoClms: agg.CAL * 1e-5        note{rmult adjusts the claim count}

            # scaling works with distributions already made by uw
            agg mdist: 5000 * agg.dHist1

            '''

            test_string_4 = '''
            # frequency options
            agg FreqFixed      10 claims sev sev.One fixed
            agg FreqPoisson    10 claims sev sev.One poisson                   note{Poisson frequency}
            agg FreqBernoulli  .8 claims sev sev.One bernoulli               note{Bernoulli en is frequency }
            agg FreqBinomial   10 claims sev sev.One binomial 0.5
            agg FreqPascal     10 claims sev sev.One pascal .8 3

            # mixed freqs
            agg FreqNegBin     10 claims sev sev.One (mixed gamma 0.65)     note{gamma mixed Poisson = negative binomial}
            agg FreqDelaporte  10 claims sev sev.One mixed delaporte .65 .25
            agg FreqIG         10 claims sev sev.One mixed ig  .65
            agg FreqSichel     10 claims sev sev.One mixed delaporte .65 -0.25
            agg FreqSichel.gamma  10 claims sev sev.One mixed sichel.gamma .65 .25
            agg FreqSichel.ig     10 claims sev sev.One mixed sichel.ig  .65 .25
            agg FreqBeta       10 claims sev sev.One mixed beta .5  4  note{second param is max mix}
            '''
            test_strings = [test_string_0, test_string_1, test_string_2, test_string_3, test_string_4]

            # run the various tests
            uw = agg.Underwriter()
            uw.glob = globals()
            uw.create_all = True
            uw.update = True
            uw.log2 = 8
            ans = {}
            # make sure we have this base first:
            uw('sev One dhistogram xps [1] [1]   note{a certain loss of 1}')
            for i, t in enumerate(test_strings):
                print(f'line {i} of {len(test_strings)}')
                ans.update(uw(t))

    Other Notes
        How Expected Claim Count is determined etc.
        * en determines en
        * prem x loss ratio -> el
        * severity x en -> el

        * always have en and el; may have prem and exp_lr
        * if prem then exp_lr computed
        * if exp_lr then premium computed

        * el is determined using np.where(el==0, prem*exp_lr, el)
        * if el==0 then el = freq * sev
        * assert np.all( el>0 or en>0 )

        * call with el (or prem x exp_lr) (or n) expressing a mixture, with the same severity
        * call with el expressing lines of business with an array of severities
        * call with single el and array of sevs expressing a mixture; [] broken down by weights

        * n is the CONDITIONAL claim count
        * X is the GROUND UP severity, so X | X > attachment is used and generates n claims

        * For fixed or histogram have to separate the parameter so they are not broad cast; otherwise
          you end up with multiple lines when you intend only one


        :param name:            name of the aggregate
        :param exp_el:          expected loss or vector
        :param exp_premium:     premium volume or vector  (requires loss ratio)
        :param exp_lr:          loss ratio or vector  (requires premium)
        :param exp_en:          expected claim count per segment (self.n = total claim count)
        :param exp_attachment:  occurrence attachment
        :param exp_limit:       occurrence limit
        :param sev_name:        severity name or sev.BUILTIN_SEV or meta.var agg or port or similar or vector or matrix
        :param sev_a:           scipy stats shape parameter
        :param sev_b:           scipy stats shape parameter
        :param sev_mean:        average (unlimited) severity
        :param sev_cv:          unlimited severity coefficient of variation
        :param sev_loc:         scipy stats location parameter
        :param sev_scale:       scipy stats scale parameter
        :param sev_xs:          xs and ps must be provided if sev_name is (c|d)histogram, xs are the bucket break points
        :param sev_ps:          ps are the probability densities within each bucket; if buckets equal size no adjustments needed
        :param sev_wt:          weight for mixed distribution
        :param freq_name:       name of frequency distribution
        :param freq_a:          cv of freq dist mixing distribution
        :param freq_b:          claims per occurrence (delaporte or sig), scale of beta or lambda (Sichel)
    """

    aggregate_keys = ['name', 'exp_el', 'exp_premium', 'exp_lr', 'exp_en', 'exp_attachment', 'exp_limit', 'sev_name',
                      'sev_a', 'sev_b', 'sev_mean', 'sev_cv', 'sev_loc', 'sev_scale', 'sev_xs', 'sev_ps',
                      'sev_wt', 'freq_name', 'freq_a', 'freq_b', 'note']

    @property
    def spec(self):
        """
        get the dictionary specification but treat as a read only
        property
        :return:
        """
        return self._spec

    @property
    def meta(self):
        """
        All relevant info

        :return:
        """
        if self._meta is None:
            self._meta = {'type': type(self), 'spec': self._spec, 'bs': self.bs, 'log2': self.log2,
                          'sevs': len(self.sevs)}
        return self._meta

    @property
    def density_df(self):
        """
        create and return the _density_df data frame
        read only property...though if you write d = a.density_df you can obviously edit d...
        :return:
        """
        if self._density_df is None:
            # really should have one of these anyway...
            if self.agg_density is None:
                raise ValueError('Update Aggregate before asking for density_df')
            # really convenient to have p=p_total to be consistent with Portfolio objects
            self._density_df = pd.DataFrame(dict(loss=self.xs, p=self.agg_density, p_total=self.agg_density,
                                                 p_sev=self.sev_density))
            # remove the fuzz
            eps = 2.5e-16
            # may not have a severity, remember...
            self._density_df.loc[:, self._density_df.select_dtypes(include=['float64']).columns] = \
                self._density_df.select_dtypes(include=['float64']).applymap(lambda x: 0 if abs(x) < eps else x)
            self._density_df = self._density_df.set_index('loss', drop=False)
            self._density_df['log_p'] = np.log(self._density_df.p)
            # when no sev this causes a problem
            if self._density_df.p_sev.dtype == np.dtype('O'):
                self._density_df['log_p_sev'] = np.nan
            else:
                self._density_df['log_p_sev'] = np.log(self._density_df.p_sev)
            self._density_df['F'] = self._density_df.p.cumsum()
            self._density_df['F_sev'] = self._density_df.p_sev.cumsum()
            # remember...better way to compute
            self._density_df['S'] = self._density_df.p.shift(-1, fill_value=0)[::-1].cumsum()
            self._density_df['S_sev'] = self._density_df.p_sev.shift(-1, fill_value=0)[::-1].cumsum()
            # add LEV,   TVaR to each threshold point...
            self._density_df['lev'] = self._density_df.S.shift(1, fill_value=0).cumsum() * self.bs
            self._density_df['exa'] = self._density_df['lev']
            self._density_df['exlea'] = \
                (self._density_df.lev - self._density_df.loss * self._density_df.S) / self._density_df.F
            # fix very small values, see port add_exa
            n_ = self._density_df.shape[0]
            if n_ < 1100:
                mult = 1
            elif n_ < 15000:
                mult = 10
            else:
                mult = 100
            loss_max = self._density_df[['loss', 'exlea']].query(' exlea > loss ').loss.max()
            if np.isnan(loss_max):
                loss_max = 0
            else:
                loss_max += mult * self.bs
            self._density_df.loc[0:loss_max, 'exlea'] = 0
            # expected value and epd
            self._density_df['e'] = np.sum(self._density_df.p * self._density_df.loss)
            self._density_df.loc[:, 'epd'] = \
                np.maximum(0, (self._density_df.loc[:, 'e'] - self._density_df.loc[:, 'lev'])) / \
                self._density_df.loc[:, 'e']
            self._density_df['exgta'] = self._density_df.loss + (
                    self._density_df.e - self._density_df.exa) / self._density_df.S
            self._density_df['exeqa'] = self._density_df.loss  # E(X | X=a) = a(!) included for symmetry was exa

        return self._density_df

    @property
    def reins_audit_df(self):
        """
        create and return the _density_df data frame
        read only property...though if you write d = a.density_df you can obviously edit d...
        :return:
        """
        if self._reins_audit_df is None:
            # really should have one of these anyway...
            if self.agg_density is None:
                raise ValueError('Update Aggregate before asking for density_df')

            ans = []
            keys = []
            if self.occ_reins is not None:
                ans.append(self._reins_audit_df_work(kind='occ'))
                keys.append('occ')
            if self.agg_reins is not None:
                ans.append(self._reins_audit_df_work(kind='agg'))
                keys.append('agg')

            if len(ans):
               self._reins_audit_df = pd.concat(ans, keys=keys, names=['kind', 'share', 'limit', 'attach'])

        return self._reins_audit_df

    def _reins_audit_df_work(self, kind='occ'):
        """
        Apply each re layer separetely and aggregate loss and other stats

        """
        ans = []
        assert self.sev_density is not None

        # TODO what about agg?
        if self.sev_gross_density is None:
            self.sev_gross_density = self.sev_density

        reins = self.occ_reins if kind == 'occ' else self.agg_reins

        for (s, y, a) in reins:
            c, n, df = self._apply_reins_work([(s,a,a)], self.sev_gross_density, False)
            ans.append(df)

        if kind == 'occ':
            ans.append(self.occ_reins_df)
        else:
            ans.append(self.agg_reins_df)

        df = pd.concat(ans, keys=reins + [('all', np.inf, 'gup')], names=['share', 'limit', 'attach', 'loss'])
        # subset and reindex
        df = df.filter(regex='^(F|p)')
        df.columns = df.columns.str.split('_', expand=True)
        df = df.sort_index(axis=1)

        # summarize
        def f(bit):
            # summary function to compute stats
            xs = bit.index.levels[3]
            xs2 = xs * xs
            xs3 = xs2 * xs

            def g(p):
                ex = np.sum(xs * p)
                ex2 = np.sum(xs2 * p)
                ex3 = np.sum(xs3 * p)
                mw = MomentWrangler()
                mw.noncentral = (ex, ex2, ex3)
                return mw.stats

            return bit['p'].apply(g)

        return df.groupby(level=(0,1,2)).apply(f).unstack(-1).sort_index(level='attach')

    def rescale(self, scale, kind='homog'):
        """
        return a rescaled Aggregate object - used to compute derivatives

        all need to be safe mults because of array specification there is an array that is not a numpy array

        TODO have parser return numpy arrays not lists!

        :param scale:  amount of scale
        :param kind:  homog of inhomog

        :return:
        """
        spec = self._spec.copy()

        def safe_scale(sc, x):
            """
            if x is a list wrap it

            :param x:
            :param sc:
            :return: sc x
            """

            if type(x) == list:
                return sc * np.array(x)
            else:
                return sc * x

        nm = spec['name']
        spec['name'] = f'{nm}:{kind}:{scale}'
        if kind == 'homog':
            # do NOT scale en... that is inhomog
            # do scale EL etc. to keep the count the same
            spec['exp_el'] = safe_scale(scale, spec['exp_el'])
            spec['exp_premium'] = safe_scale(scale, spec['exp_premium'])
            spec['exp_attachment'] = safe_scale(scale, spec['exp_attachment'])
            spec['exp_limit'] = safe_scale(scale, spec['exp_limit'])
            spec['sev_loc'] = safe_scale(scale, spec['sev_loc'])
            # note: scaling the scale takes care of the mean, so do not double count
            # default is 0. Can't ask if array is...but if array have to deal with it
            if (type(spec['sev_scale']) not in (int, float)) or spec['sev_scale']:
                spec['sev_scale'] = safe_scale(scale, spec['sev_scale'])
            else:
                spec['sev_mean'] = safe_scale(scale, spec['sev_mean'])
            if spec['sev_xs']:
                spec['sev_xs'] = safe_scale(scale, spec['sev_xs'])
        elif kind == 'inhomog':
            # just scale up the volume, including en
            spec['exp_el'] = safe_scale(scale, spec['exp_el'])
            spec['exp_premium'] = safe_scale(scale, spec['exp_premium'])
            spec['exp_en'] = safe_scale(scale, spec['exp_en'])
        else:
            raise ValueError(f'Inadmissible option {kind} passed to rescale, kind should be homog or inhomog.')
        return Aggregate(**spec)

    def __init__(self, name, exp_el=0, exp_premium=0, exp_lr=0, exp_en=0, exp_attachment=0, exp_limit=np.inf,
                 sev_name='', sev_a=0, sev_b=0, sev_mean=0, sev_cv=0, sev_loc=0, sev_scale=0,
                 sev_xs=None, sev_ps=None, sev_wt=1, sev_conditional=True,
                 occ_reins=None, occ_kind='',
                 freq_name='', freq_a=0, freq_b=0,
                 agg_reins=None, agg_kind='',
                 note=''):

        # assert np.allclose(np.sum(sev_wt), 1)

        # have to be ready for inputs to be in a list, e.g. comes that way from Pandas via Excel

        def get_value(v):
            if isinstance(v, list):
                return v[0]
            else:
                return v

        # class variables
        self.name = get_value(name)
        # for persistence, save the raw called spec... (except lookups have been replaced...)
        # TODO want to use the trick with setting properties so that if they are altered spec gets alterned...
        self._spec = dict(name=name, exp_el=exp_el, exp_premium=exp_premium, exp_lr=exp_lr, exp_en=exp_en,
                          exp_attachment=exp_attachment, exp_limit=exp_limit,
                          sev_name=sev_name, sev_a=sev_a, sev_b=sev_b, sev_mean=sev_mean, sev_cv=sev_cv,
                          sev_loc=sev_loc, sev_scale=sev_scale, sev_xs=sev_xs, sev_ps=sev_ps, sev_wt=sev_wt,
                          sev_conditional=sev_conditional,
                          occ_reins=occ_reins, occ_kind=occ_kind,
                          freq_name=freq_name, freq_a=freq_a, freq_b=freq_b,
                          agg_reins=agg_reins, agg_kind=agg_kind, note=note)
        logger.debug(
            f'Aggregate.__init__ | creating new Aggregate {self.name}')
        Frequency.__init__(self, get_value(freq_name), get_value(freq_a), get_value(freq_b))
        self.xs = None
        self.bs = 0
        self.log2 = 0
        self.ex = 0
        self.note = note
        self.en = None  # this is for a sublayer e.g. for limit profile
        self.n = 0  # this is total frequency
        self.attachment = None
        self.limit = None
        self.agg_density = None
        self.sev_density = None
        self.dh_agg_density = None
        self.dh_sev_density = None
        self.ftagg_density = None
        self.fzapprox = None
        self._tail_var = None
        self._tail_var2 = None
        self._inverse_tail_var = None
        self.agg_m, self.agg_cv, self.agg_skew = 0, 0, 0
        self._linear_quantile_function = None
        self._cdf = None
        self._pdf = None
        self.beta_name = ''  # name of the beta function used to create dh distortion
        self.sevs = None
        self.audit_df = None
        self.verbose_audit_df = None
        self._careful_q = None
        self._density_df = None
        self._reins_audit_df = None
        self._meta = None
        self.q_temp = None
        self.occ_reins = occ_reins
        self.occ_kind = occ_kind
        self.occ_netter = None
        self.occ_ceder = None
        self.occ_reins_df = None
        self.agg_reins = agg_reins
        self.agg_kind = agg_kind
        self.agg_netter = None
        self.agg_ceder = None
        self.agg_reins_df = None
        self.sev_ceded_density = None
        self.sev_net_density = None
        self.sev_gross_density = None
        self.agg_ceded_density = None
        self.agg_net_density = None
        self.agg_gross_density = None

        self.statistics_df = pd.DataFrame(columns=['name', 'limit', 'attachment', 'sevcv_param', 'el', 'prem', 'lr'] +
                                                  MomentAggregator.column_names() +
                                                  ['mix_cv'])
        self.statistics_total_df = self.statistics_df.copy()
        ma = MomentAggregator(self.freq_moms)

        # broadcast arrays: force answers all to be arrays (?why only these items?!)
        if not isinstance(exp_el, collections.Iterable):
            exp_el = np.array([exp_el])
        if not isinstance(sev_wt, collections.Iterable):
            sev_wt = np.array([sev_wt])

        # broadcast together and create container for the severity distributions
        if np.sum(sev_wt) == len(sev_wt):
            # do not perform the exp / sev product, in this case
            # broadcast all exposure and sev terms together
            exp_el, exp_premium, exp_lr, en, attachment, limit, \
            sev_name, sev_a, sev_b, sev_mean, sev_cv, sev_loc, sev_scale, sev_wt = \
                np.broadcast_arrays(exp_el, exp_premium, exp_lr, exp_en, exp_attachment, exp_limit,
                                    sev_name, sev_a, sev_b, sev_mean, sev_cv, sev_loc, sev_scale, sev_wt)
            exp_el = np.where(exp_el > 0, exp_el, exp_premium * exp_lr)
            all_arrays = list(zip(exp_el, exp_premium, exp_lr, en, attachment, limit,
                                  sev_name, sev_a, sev_b, sev_mean, sev_cv, sev_loc, sev_scale, sev_wt))
            self.en = en
            self.attachment = attachment
            self.limit = limit
            n_components = len(all_arrays)
            logger.debug(f'Aggregate.__init__ | Broadcast/align: exposures + severity = {len(exp_el)} exp = '
                         f'{len(sev_a)} sevs = {n_components} componets')
            self.sevs = np.empty(n_components, dtype=type(Severity))

        else:
            # perform exp / sev product
            # broadcast exposure terms (el, epremium, en, lr, attachment, limit) and sev terms (sev_) separately
            # then we take an "outer product" of the two parts...
            exp_el, exp_premium, exp_lr, en, attachment, limit = \
                np.broadcast_arrays(exp_el, exp_premium, exp_lr, exp_en, exp_attachment, exp_limit)
            sev_name, sev_a, sev_b, sev_mean, sev_cv, sev_loc, sev_scale, sev_wt = \
                np.broadcast_arrays(sev_name, sev_a, sev_b, sev_mean, sev_cv, sev_loc, sev_scale, sev_wt)
            exp_el = np.where(exp_el > 0, exp_el, exp_premium * exp_lr)
            exp_arrays = [exp_el, exp_premium, exp_lr, en, attachment, limit]
            sev_arrays = [sev_name, sev_a, sev_b, sev_mean, sev_cv, sev_loc, sev_scale, sev_wt]
            all_arrays = [[k for j in i for k in j] for i in itertools.product(zip(*exp_arrays), zip(*sev_arrays))]
            self.en = np.array([i[3] * i[-1] for i in all_arrays])
            self.attachment = np.array([i[4] for i in all_arrays])
            self.limit = np.array([i[5] for i in all_arrays])
            n_components = len(all_arrays)
            logger.debug(
                f'Aggregate.__init__ | Broadcast/product: exposures x severity = {len(exp_arrays)} x {len(sev_arrays)} '
                f'=  {n_components}')
            self.sevs = np.empty(n_components, dtype=type(Severity))

        # overall freq CV with common mixing
        mix_cv = self.freq_a
        # counter to label components
        r = 0
        # perform looping creation of severity distribution
        for _el, _pr, _lr, _en, _at, _y, _sn, _sa, _sb, _sm, _scv, _sloc, _ssc, _swt in all_arrays:

            # WARNING: note sev_xs and sev_ps are NOT broadcast
            self.sevs[r] = Severity(_sn, _at, _y, _sm, _scv, _sa, _sb, _sloc, _ssc, sev_xs, sev_ps, sev_conditional)
            sev1, sev2, sev3 = self.sevs[r].moms()

            # input claim count trumps input loss
            if _en > 0:
                _el = _en * sev1
            elif _el > 0:
                _en = _el / sev1
            # if premium compute loss ratio, if loss ratio compute premium
            if _pr > 0:
                _lr = _el / _pr
            elif _lr > 0:
                _pr = _el / _lr

            # for empirical freq claim count entered as -1
            if _en < 0:
                _en = np.sum(self.freq_a * self.freq_b)
                _el = _en * sev1

            # scale for the mix - OK because we have split the exposure and severity components
            _pr *= _swt
            _el *= _swt
            _lr *= _swt
            _en *= _swt

            # accumulate moments
            ma.add_f1s(_en, sev1, sev2, sev3)

            # store
            self.statistics_df.loc[r, :] = \
                [self.name, _y, _at, _scv, _el, _pr, _lr] + ma.get_fsa_stats(total=False) + [mix_cv]
            r += 1

        # average exp_limit and exp_attachment
        avg_limit = np.sum(self.statistics_df.limit * self.statistics_df.freq_1) / ma.tot_freq_1
        avg_attach = np.sum(self.statistics_df.attachment * self.statistics_df.freq_1) / ma.tot_freq_1
        # assert np.allclose(ma.freq_1, self.statistics_df.exp_en)

        # store answer for total
        tot_prem = self.statistics_df.prem.sum()
        tot_loss = self.statistics_df.el.sum()
        if tot_prem > 0:
            lr = tot_loss / tot_prem
        else:
            lr = np.nan
        self.statistics_total_df.loc[f'mixed', :] = \
            [self.name, avg_limit, avg_attach, 0, tot_loss, tot_prem, lr] + ma.get_fsa_stats(total=True, remix=True) \
            + [mix_cv]
        self.statistics_total_df.loc[f'independent', :] = \
            [self.name, avg_limit, avg_attach, 0, tot_loss, tot_prem, lr] + ma.get_fsa_stats(total=True, remix=False) \
            + [mix_cv]
        self.statistics_df['wt'] = self.statistics_df.freq_1 / ma.tot_freq_1
        self.statistics_total_df['wt'] = self.statistics_df.wt.sum()  # better equal 1.0!
        self.n = ma.tot_freq_1
        self.agg_m = self.statistics_total_df.loc['mixed', 'agg_m']
        self.agg_cv = self.statistics_total_df.loc['mixed', 'agg_cv']
        self.agg_skew = self.statistics_total_df.loc['mixed', 'agg_skew']
        # finally, need a report_ser series for Portfolio to consolidate
        self.report_ser = ma.stats_series(self.name, np.max(self.limit), 0.999, remix=True)
        self._middle_q = None
        self._q = None

    def __repr__(self):
        """
        wrap default with name
        :return:
        """
        return f'{super(Aggregate, self).__repr__()} name: {self.name}'

    def __str__(self):
        """
        Goal: readability

        :return:
        """
        # pull out agg statistics_df
        ags = self.statistics_total_df.loc['mixed', :]
        s = f"Aggregate: {self.name}\n\tEN={ags['freq_1']}, CV(N)={ags['freq_cv']:5.3f}\n\t" \
            f"{len(self.sevs)} severit{'ies' if len(self.sevs) > 1 else 'y'}, EX={ags['sev_1']:,.1f}, " \
            f"CV(X)={ags['sev_cv']:5.3f}\n\t" \
            f"EA={ags['agg_1']:,.1f}, CV={ags['agg_cv']:5.3f}"
        return s

    def _repr_html_(self):
        s = [f'<h3>Aggregate object: {self.name}</h3>']
        s.append(f'Claim count {self.n:0,.2f}, {self.freq_name} distribution<br>')
        n = len(self.statistics_df)
        if n == 1:
            sv = self.sevs[0]
            if sv.limit == np.inf and sv.attachment == 0:
                _la = 'unlimited'
            else:
                _la = f'{sv.limit} xs {sv.attachment}'
            s.append(f'Severity{sv.long_name} distribution, {_la}<br>')
        else:
            s.append(f'Severity with {n} components<br>')
        if self.bs > 0:
            s.append(f'Updated with bucket size {self.bs:.6g} and log2 = {self.log2}')
        df = self.describe()
        return '\n'.join(s) + df.to_html()

    def discretize(self, sev_calc, discretization_calc, normalize):
        """
        Continuous is used when you think of the resulting distribution as continuous across the buckets
        (which we generally don't). We use the discretized distribution as though it is fully discrete
        and only takes values at the bucket points. Hence we should use sev_calc='discrete'. The buckets are
        shifted left by half a bucket, so :math:`Pr(X=b_i) = Pr( b_i - b/2 < X <= b_i + b/2)`.

        The other wrinkle is the right hand end of the range. If we extend to np.inf then we ensure we have
        probabilities that sum to 1. But that method introduces a probability mass in the last bucket that
        is often not desirable (we expect to see a smooth continuous distribution and we get a mass). The
        other alternative is to use endpoint = 1 bucket beyond the last, which avoids this problem but can leave
        the probabilities short. We opt here for the latter and rescale

        defaults: discrete, survival, True

        :param sev_calc:  continuous or discrete or raw (for...);
        and method becomes discrete otherwise
        :param discretization_calc:  survival, distribution or both; in addition
        the method then becomes survival
        :param normalize: if true, normalize the severity so sum probs = 1. This is generally what you want; but
        when dealing with thick tailed distributions it can be helpful to turn it off.
        :return:
        """

        if sev_calc == 'continuous':
            adj_xs = np.hstack((self.xs, self.xs[-1] + self.bs))
        elif sev_calc == 'discrete':
            # adj_xs = np.hstack((self.xs - self.bs / 2, np.inf))
            # mass at the end undesirable. can be put in with reinsurance layer in spec
            adj_xs = np.hstack((self.xs - self.bs / 2, self.xs[-1] + self.bs / 2))
        elif sev_calc == 'raw':
            adj_xs = self.xs
        else:
            raise ValueError(
                f'Invalid parameter {sev_calc} passed to discretize; options are raw, discrete, continuous, inf or double.')

        # bed = bucketed empirical distribution
        beds = []
        for fz in self.sevs:
            if discretization_calc == 'both':
                # see comments: we rescale each severity...
                appx = np.maximum(np.diff(fz.cdf(adj_xs)), -np.diff(fz.sf(adj_xs)))
            elif discretization_calc == 'survival':
                appx = -np.diff(fz.sf(adj_xs))
                # beds.append(appx / np.sum(appx))
            elif discretization_calc == 'distribution':
                appx = np.diff(fz.cdf(adj_xs))
                # beds.append(appx / np.sum(appx))
            else:
                raise ValueError(
                    f'Invalid options {discretization_calc} to double_diff; options are density, survival or both')
            if normalize:
                beds.append(appx / np.sum(appx))
            else:
                beds.append(appx)
        return beds

    def snap(self, x):
        """
        snap value x to the index of density_df

        :param x:
        :return:
        """
        ix = self.density_df.index.get_loc(x, 'nearest')
        return self.density_df.iat[ix, 0]

    def easy_update(self, log2=13, bs=0, **kwargs):
        """
        Convenience function, delegates to update. Avoids having to pass xs.


        :param log2:
        :param bs:
        :param kwargs:  passed through to update
        :return:
        """
        # guess bucket and update
        if bs == 0:
            bs = round_bucket(self.recommend_bucket(log2))
        xs = np.arange(0, 1 << log2, dtype=float) * bs
        if 'approximation' not in kwargs:
            if self.n > 100:
                kwargs['approximation'] = 'slognorm'
            else:
                kwargs['approximation'] = 'exact'
        return self.update(xs, **kwargs)

    def update(self, xs, padding=1, tilt_vector=None, approximation='exact', sev_calc='discrete',
               discretization_calc='survival', normalize=True, force_severity=False, debug=False):
        """
        Compute the density
        Pre-0.9.3....does not have reinsurance features.

        0.9.3 removed verbose option: it just makes plots you can get with .plot
        0.9.3: multi-way switch force_severity: if "yes" then update exists after sev comp (eg for plot).
        else if True create severity and perform an update.

        Quick simple test with log2=13 update took 5.69 ms and _eff took 2.11 ms. So quicker
        but not an issue unless you are doing many!

        :param xs:  range of x values used to discretize
        :param padding: for FFT calculation
        :param tilt_vector: tilt_vector = np.exp(self.tilt_amount * np.arange(N)), N=2**log2, and
                tilt_amount * N < 20 recommended
        :param approximation: exact = perform frequency / severity convolution using FFTs. slognorm or
                sgamma apply shifted lognormal or shifted gamma approximations.
        :param sev_calc:   discrete = suitable for fft, continuous = for rv_histogram cts version
        :param discretization_calc: use survival, distribution or both (=max(cdf, sf)) which is most accurate calc
        :param normalize: normalize severity to 1.0
        :param force_severity: make severities even if using approximation, for plotting
        :param verbose: make partial plots and return details of all moments by limit profile or
                severity mixture component.
        :return:
        """
        self._density_df = None  # invalidate
        self._meta = None

        self.xs = xs
        self.bs = xs[1]
        # WHOA! WTF
        self.log2 = int(np.log(len(xs)) / np.log(2))

        # make the severity vector: a claim count weighted average of the severities
        if approximation == 'exact' or force_severity:
            wts = self.statistics_df.freq_1 / self.statistics_df.freq_1.sum()
            self.sev_density = np.zeros_like(xs)
            beds = self.discretize(sev_calc, discretization_calc, normalize)
            for temp, w, a, l, n in zip(beds, wts, self.attachment, self.limit, self.en):
                self.sev_density += temp * w

        if force_severity == 'yes':
            # only asking for severity (used by plot)
            return

        # deal with per occ reinsurance
        # TODO issues with force_severity = False.... get rid of that option entirely?
        if self.occ_reins is not None:
            logger.info('Applying occurrence reinsurance.')
            if self.sev_gross_density is not None:
                # make the function an involution...
                self.sev_density = self.sev_gross_density
            self.apply_occ_reins(debug)

        if approximation == 'exact':
            if self.n > 100:
                logger.warning(f'Aggregate.update | warning, claim count {self.n} is high; consider an approximation ')

            if self.n == 0:
                # for dynamics it is helpful to have a zero risk return zero appropriately
                # z = ft(self.sev_density, padding, tilt_vector)
                self.agg_density = np.zeros_like(self.xs)
                self.agg_density[0] = 1
                # extreme idleness...but need to make sure it is the right shape and type
                self.ftagg_density = ft(self.agg_density, padding, tilt_vector)
            else:
                # usual calculation...this is where the magic happens!
                # have already dealt with per occ reinsurance
                z = ft(self.sev_density, padding, tilt_vector)
                self.ftagg_density = self.mgf(self.n, z)
                self.agg_density = np.real(ift(self.ftagg_density, padding, tilt_vector))

                # NOW have to apply agg reinsurance to this line
                self.apply_agg_reins(debug)

        else:
            # regardless of request if skew == 0 have to use normal
            # must check there is no per occ reinsurance... it won't work
            assert self.occ_reins is None

            if self.agg_skew == 0:
                self.fzapprox = ss.norm(scale=self.agg_m * self.agg_cv, loc=self.agg_m)
            elif approximation == 'slognorm':
                shift, mu, sigma = sln_fit(self.agg_m, self.agg_cv, self.agg_skew)
                self.fzapprox = ss.lognorm(sigma, scale=np.exp(mu), loc=shift)
            elif approximation == 'sgamma':
                shift, alpha, theta = sgamma_fit(self.agg_m, self.agg_cv, self.agg_skew)
                self.fzapprox = ss.gamma(alpha, scale=theta, loc=shift)
            else:
                raise ValueError(f'Invalid approximation {approximation} option passed to CAgg density. '
                                 'Allowable options are: exact | slogorm | sgamma')

            ps = self.fzapprox.pdf(xs)
            self.agg_density = ps / np.sum(ps)
            self.ftagg_density = ft(self.agg_density, padding, tilt_vector)
            # can still apply aggregate in this mode
            self.apply_agg_reins(debug)

        # make a suitable audit_df
        # originally...irritating no freq cv or sev cv
        # cols = ['name', 'limit', 'attachment', 'el', 'freq_1', 'sev_1', 'agg_m', 'agg_cv', 'agg_skew']
        cols = ['name', 'limit', 'attachment', 'el', 'freq_1', 'freq_cv', 'freq_skew',
                'sev_1', 'sev_cv', 'sev_skew', 'agg_m', 'agg_cv', 'agg_skew']
        self.audit_df = pd.concat((self.statistics_df[cols],
                                   self.statistics_total_df.loc[['mixed'], cols]),
                                  axis=0)
        # add empirical stats
        if self.sev_density is not None:
            _m, _cv = xsden_to_meancv(self.xs, self.sev_density)
        else:
            _m = np.nan
            _cv = np.nan
        self.audit_df.loc['mixed', 'emp_sev_1'] = _m
        self.audit_df.loc['mixed', 'emp_sev_cv'] = _cv
        _m, _cv = xsden_to_meancv(self.xs, self.agg_density)
        self.audit_df.loc['mixed', 'emp_agg_1'] = _m
        self.ex = _m
        self.audit_df.loc['mixed', 'emp_agg_cv'] = _cv

        # invalidate stored functions
        self.nearest_quantile_function = None
        self._cdf = None

    def update_efficiently(self, xs, padding=1, approximation='exact', sev_calc='discrete',
                           discretization_calc='survival', normalize=True):
        """
        Compute the density with absolute minimum overhead. Called by port.update_efficiently
        Started with code for update and removed frills
        No tilting!
        :param xs:  range of x values used to discretize
        :param padding: for FFT calculation
        :param approximation: exact = perform frequency / severity convolution using FFTs. slognorm or
                sgamma apply shifted lognormal or shifted gamma approximations.
        :param sev_calc:   discrete = suitable for fft, continuous = for rv_histogram cts version
        :param discretization_calc: use survival, distribution or both (=max(cdf, sf)) which is most accurate calc
        :return:
        """

        r = 0
        self.xs = xs
        self.bs = xs[1]
        self.log2 = int(np.log(len(xs)) / np.log(2))
        tilt_vector = None

        # make the severity vector: a claim count weighted average of the severities
        if approximation == 'exact':
            wts = self.statistics_df.freq_1 / self.statistics_df.freq_1.sum()
            self.sev_density = np.zeros_like(xs)
            beds = self.discretize(sev_calc, discretization_calc, normalize)
            for temp, w, a, l, n in zip(beds, wts, self.attachment, self.limit, self.en):
                self.sev_density += temp * w

        if approximation == 'exact':
            if self.n == 0:
                # for dynamics it is helpful to have a zero risk return zero appropriately
                # z = ft(self.sev_density, padding, tilt_vector)
                self.agg_density = np.zeros_like(self.xs)
                self.agg_density[0] = 1
                # extreme idleness...but need to make sure it is the right shape and type
                self.ftagg_density = ft(self.agg_density, padding, tilt_vector)
            else:
                # usual calculation...this is where the magic happens!
                z = ft(self.sev_density, padding, tilt_vector)
                self.ftagg_density = self.mgf(self.n, z)
                self.agg_density = np.real(ift(self.ftagg_density, padding, tilt_vector))
        else:
            # regardless of request if skew == 0 have to use normal
            if self.agg_skew == 0:
                self.fzapprox = ss.norm(scale=self.agg_m * self.agg_cv, loc=self.agg_m)
            elif approximation == 'slognorm':
                shift, mu, sigma = sln_fit(self.agg_m, self.agg_cv, self.agg_skew)
                self.fzapprox = ss.lognorm(sigma, scale=np.exp(mu), loc=shift)
            elif approximation == 'sgamma':
                shift, alpha, theta = sgamma_fit(self.agg_m, self.agg_cv, self.agg_skew)
                self.fzapprox = ss.gamma(alpha, scale=theta, loc=shift)
            else:
                raise ValueError(f'Invalid approximation {approximation} option passed to CAgg density. '
                                 'Allowable options are: exact | slogorm | sgamma')

            ps = self.fzapprox.pdf(xs)
            self.agg_density = ps / np.sum(ps)
            self.ftagg_density = ft(self.agg_density, padding, tilt_vector)

        # invalidate stored functions
        self.nearest_quantile_function = None
        self._cdf = None
        self.verbose_audit_df = None

    def _apply_reins_work(self, reins_list, base_density, debug):
        """
        Actually do the work. Called by apply_reins and reins_audit_df.
        Only needs self to get limits, which it must guess without q (not computed
        at this stage). Does not need to know if occ or agg reins,
        only that the correct base_density is supplied.

        :param reins_list:
        :param kind: occ or agg, for debug plotting
        :param debug:
        :return: ceder, netter,
        """
        ans = make_ceder_netter(reins_list, debug)
        if debug:
            # debug xs and ys are the knot points of the interpolation function; good for plotting
            ceder, netter, xs, ys = ans
        else:
            ceder, netter = ans
        # assemble df for answers
        reins_df = pd.DataFrame({'loss': self.xs, 'p_subject': base_density,
                                 'F_subject': base_density.cumsum()}).set_index('loss', drop=False)
        reins_df['loss_net'] = netter(reins_df.loss)
        reins_df['loss_ceded'] = ceder(reins_df.loss)
        # summarized n and c
        sn = reins_df.groupby('loss_net').p_subject.sum()
        sc = reins_df.groupby('loss_ceded').p_subject.sum()
        # -100: this value should never appear. use big value to make it obvious
        netter_interp = interp1d(sn.index, sn.cumsum(), fill_value=(-100, 1), bounds_error=False)
        ceder_interp = interp1d(sc.index, sc.cumsum(), fill_value=(-100, 1), bounds_error=False)
        reins_df['F_net'] = netter_interp(reins_df.loss)
        reins_df['F_ceded'] = ceder_interp(reins_df.loss)
        reins_df['p_net'] = np.diff(reins_df.F_net, prepend=0)
        reins_df['p_ceded'] = np.diff(reins_df.F_ceded, prepend=0)

        if debug is False:
            return ceder, netter, reins_df

        # quick debug; need to know kind=occ|agg here
        f = plt.figure(constrained_layout=True, figsize=(12, 9))
        axd = f.subplot_mosaic('AB\nCD')
        xlim = self.limits()
        # scale??
        x = np.linspace(0, xlim[1], 201)
        y = ceder(x)
        n = x - y
        nxs = netter(xs)

        ax = axd['A']
        ax.plot(xs, ys, 'o')
        ax.plot(x, y)
        ax.plot(x, x, lw=.5, c='C7')
        ax.set(aspect='equal', xlim=xlim, ylim=xlim,
               xlabel='Subject', ylabel='Ceded',
               title=f'Subject and ceded\nMax ceded loss {y[-1]:,.1f}')

        ax = axd['B']
        ax.plot(xs, nxs, 'o')
        ax.plot(x, n)
        ax.plot(x, x, lw=.5, c='C7')
        ax.set(aspect='equal', ylim=xlim,
               xlabel='Subject', ylabel='Net',
               title=f'Subject and net\nMax net loss {n[-1]:,.1f}')

        ax = axd['C']
        sn.cumsum().plot(ax=ax, lw=4, alpha=0.3, label='net')
        sc.cumsum().plot(ax=ax, lw=4, alpha=0.3, label='ceded')
        reins_df.filter(regex='F').plot(xlim=xlim, ax=ax)
        ax.set(title=f'Subject, net and ceded\ndistributions')
        ax.legend()

        ax = axd['D']
        reins_df.filter(regex='p_').plot(xlim=xlim, drawstyle='steps-post', ax=ax)
        ax.set(title=f'Subject, net and ceded\ndensities')
        ax.legend()

        return ceder, netter, reins_df

    def apply_occ_reins(self, debug=False):
        """
        Apply the entire occ reins structure and save output
        For by layer detail create reins_audit_df
        Makes sev_gross_density, sev_net_density and sev_ceded_density, and updates sev_density to the requested view.

        Treatment in stats?

        :return:
        """
        # generic function makes netter and ceder functions
        if self.occ_reins is None:
            return

        occ_ceder, occ_netter, occ_reins_df = self._apply_reins_work(self.occ_reins, self.sev_density, debug)
        # store stuff
        self.occ_reins_df = occ_reins_df
        self.sev_gross_density = self.sev_density
        self.sev_net_density = occ_reins_df['p_net']
        self.sev_ceded_density = occ_reins_df['p_ceded']
        if self.occ_kind == 'ceded to':
            self.sev_density = self.sev_net_density
        elif self.occ_kind == 'net of':
            self.sev_density = self.sev_ceded_density
        else:
            raise ValueError(f'Unexpected kind of occ reinsurace, {self.occ_kind}')

    def apply_agg_reins(self, debug=False, padding=1, tilt_vector=None):
        """
        Apply the entire agg reins structure and save output
        For by layer detail create reins_audit_df
        Makes sev_gross_density, sev_net_density and sev_ceded_density, and updates sev_density to the requested view.

        Treatment in stats?

        :return:
        """
        # generic function makes netter and ceder functions
        if self.agg_reins is None:
            return
        logger.info(f'Applying aggregate reinsurance for {self.name}')
        # aggregate moments (lose f x sev view) are computed after this step, so no adjustment needed there
        # agg: no way to make total = f x sev
        # initial empirical moments
        _m, _cv = xsden_to_meancv(self.xs, self.agg_density)

        agg_ceder, agg_netter, agg_reins_df = self._apply_reins_work(self.agg_reins, self.agg_density, debug)
        # store stuff
        self.agg_reins_df = agg_reins_df
        self.agg_gross_density = self.agg_density
        self.agg_net_density = agg_reins_df['p_net']
        self.agg_ceded_density = agg_reins_df['p_ceded']
        if self.agg_kind == 'ceded to':
            self.agg_density = self.agg_net_density
        elif self.agg_kind == 'net of':
            self.agg_density = self.agg_ceded_density
        else:
            raise ValueError(f'Unexpected kind of agg reinsurace, {self.agg_kind}')

        # update ft of agg
        self.ftagg_density = ft(self.agg_density, padding, tilt_vector)

        # see impact on moments
        _m2, _cv2 = xsden_to_meancv(self.xs, self.agg_density)
        # self.audit_df.loc['mixed', 'emp_agg_1'] = _m
        # old_m = self.ex
        self.ex = _m2
        # self.audit_df.loc['mixed', 'emp_agg_cv'] = _cv
        logger.log(35,
            f'Applying agg reins to {self.name}\tOld mean and cv= {_m:,.3f}\t{_m:,.3f}\n'
            f'New mean and cv = {_m2:,.3f}\t{_cv2:,.3f}')

    def apply_distortion(self, dist):
        """
        apply distortion to the aggregate density and append as exag column to density_df
        TODO: implement original and revised calculation method
        :param dist:
        :return:
        """
        if self.agg_density is None:
            logger.warning(f'You must update before applying a distortion ')
            return

        S = self.density_df.S
        # some dist return np others don't this converts to numpy...
        gS = np.array(dist.g(S))

        self.density_df['gS'] = gS
        self.density_df['exag'] = np.hstack((0, gS[:-1])).cumsum() * self.bs

    def cramer_lundberg(self, rho, cap=0, excess=0, stop_loss=0, kind='index', padding=0):
        """
        return the CL function relating surplus to eventual probability of ruin

        Assumes frequency is Poisson

        rho = prem / loss - 1 is the margin-to-loss ratio

        cap = cap severity at cap - replace severity with X | X <= cap
        excess = replace severit with X | X > cap (i.e. no shifting)
        stop_loss = apply stop loss reinsurance to cap, so  X > stop_loss replaced with Pr(X > stop_loss) mass

        Embrechts, Kluppelberg, Mikosch 1.2 Page 28 Formula 1.11

        Pollaczeck-Khinchine Capital

        returns ruin vector as pd.Series
            function to lookup (no interpolation if kind==index; else interp) capitals

        """

        if self.sev_density is None:
            raise ValueError("Must recalc before computing Cramer Lundberg distribution.")

        bit = self.density_df.p_sev.copy()
        if cap:
            idx = np.searchsorted(bit.index, cap, 'right')
            bit.iloc[idx:] = 0
            bit = bit / bit.sum()
        elif excess:
            # excess may not be in the index...
            idx = np.searchsorted(bit.index, excess, 'right')
            bit.iloc[:idx] = 0
            bit = bit / bit.sum()
        elif stop_loss:
            idx = np.searchsorted(bit.index, stop_loss, 'left')
            xsprob = bit.iloc[idx + 1:].sum()
            bit.iloc[idx] += xsprob
            bit.iloc[idx + 1:] = 0
        mean = np.sum(bit * bit.index)

        # integrated F function
        fi = bit.shift(-1, fill_value=0)[::-1].cumsum()[::-1].cumsum() * self.bs / mean
        # difference = probability density
        dfi = np.diff(fi, prepend=0)
        # use loc FFT, with wrapping
        fz = ft(dfi, padding, None)
        mfz = 1 / (1 - fz / (1 + rho))
        f = ift(mfz, padding, None)
        f = np.real(f) * rho / (1 + rho)
        f = np.cumsum(f)
        ruin = pd.Series(1 - f, index=bit.index)

        if kind == 'index':
            def find_u(p):
                idx = len(ruin) - ruin[::-1].searchsorted(p, 'left')
                return ruin.index[idx]
        else:
            def find_u(p):
                below = len(ruin) - ruin[::-1].searchsorted(p, 'left')
                above = below - 1
                q_below = ruin.index[below]
                q_above = ruin.index[above]
                p_below = ruin.iloc[below]
                p_above = ruin.iloc[above]
                q = q_below + (p - p_below) / (p_above - p_below) * (q_above - q_below)
                return q

        return ruin, find_u, mean, dfi  # , ruin2

    def delbaen_haezendonck_density(self, xs, padding, tilt_vector, beta, beta_name=""):
        """
        Compare the base and Delbaen Haezendonck transformed aggregates

        * beta(x) = alpha + gamma(x)
        * alpha = log(freq' / freq): log of the increase in claim count
        * gamma = log(Radon Nikodym derv of adjusted severity) = log(tilde f / f)

        Adjustment guarantees a positive loading iff beta is an increasing function
        iff gamma is increasing iff tilde f / f is increasing.
        cf. eqn 3.7 and 3.8

        Note conditions that E(exp(beta(X)) and E(X exp(beta(X)) must both be finite (3.4, 3.5)
        form of beta function described in 2.23 via, 2.16-17 and 2.18

        From examples on last page of paper:

        ::

            beta(x) = a ==> adjust frequency by factor of e^a
            beta(x) = log(1 + b(x - E(X)))  ==> variance principle EN(EX + bVar(X))
            beta(x) = ax- logE_P(exp(a x))  ==> Esscher principle

        To make a 'multiple' of an existing distortion you can use a simple wrapper class like this:

        ::

            class dist_wrap(agg.Distortion):
                '''
                wrap a distortion to include higher or lower freq
                in DH α is actually exp(α)
                this will pass isinstance(g2, agg.Distortion)
                '''
                def __init__(self, α, dist):
                    def loc_g(s):
                        return α * dist.g(s)
                    self.g = loc_g
                    self.name = dist.name

        :param xs: is part of agg so can use that
        :param padding: = 1 (default)
        :param tilt_vector: None (default)
        :param beta: function R+ to R with appropriate properties or name of prob distortion function
        :param beta_name:
        :return:
        """
        if self.agg_density is None:
            # update
            self.update(xs, padding, tilt_vector, 'exact')
        if isinstance(beta, Distortion):
            # passed in a distortion function
            beta_name = beta.name
            self.dh_sev_density = -np.diff(beta.g(1 - np.cumsum(np.hstack((0, self.sev_density)))))
            # ex_beta from Radon N derv, e^beta = dh / objective, so E[e^beta] = int dh/obj x obj = sum(dh)
            # which we expect to equal 1...hummm not adjusting the freq?!
            ex_beta = np.sum(self.dh_sev_density)
        else:
            self.dh_sev_density = self.sev_density * np.exp(beta.g(xs))
            ex_beta = np.sum(self.dh_sev_density)
        self.dh_sev_density = self.dh_sev_density / ex_beta
        adj_n = ex_beta * self.n
        if self.freq_name == 'poisson':
            # convolve for compound Poisson
            ftagg_density = np.exp(adj_n * (ft(self.dh_sev_density, padding, tilt_vector) - 1))
            self.dh_agg_density = np.real(ift(ftagg_density, padding, tilt_vector))
        else:
            raise ValueError('Must use compound Poisson for DH density')
        self.beta_name = beta_name

    def plot(self, kind='quick', axiter=None, aspect=1, figsize=(10, 3)):
        """
        plot computed density and aggregate

        **kind** option:

        * quick (default): Density for sev and agg on nominal and log scale; Lee diagram sev and agg
        * long: severity, log sev density, sev dist, agg with sev, agg on own, agg on log, S, Lee, return period

        :param kind: quick or long
        :param axiter: optional axiter object
        :param aspect: optional aspect ratio of individual plots
        :param figsize: optional overall figure size
        :return:
        """

        if self.agg_density is None:
            raise ValueError('Cannot plot before update')
            return
        if self.sev_density is None:
            self.update(self.xs, 1, None, sev_calc='discrete', force_severity='yes')

        set_tight = (axiter is None)

        if kind == 'long':
            axiter = axiter_factory(axiter, 10, aspect=aspect, figsize=figsize)

            max_lim = min(self.xs[-1], np.max(self.limit)) * 1.05
            if max_lim < 1: max_lim = 1

            next(axiter).plot(self.xs, self.sev_density)  # , drawstyle='steps-post')
            axiter.ax.set(title='Severity', xlim=(0, max_lim))

            next(axiter).plot(self.xs, self.sev_density)
            axiter.ax.set(title='Log Severity')
            if np.sum(self.sev_density == 1) >= 1:
                # sev density is degenerate, 1,0,0,... log scales won't work
                axiter.ax.set(title='Severity Degenerate')
                axiter.ax.set(xlim=(0, max_lim * 2))
            else:
                axiter.ax.set(title='Log Severity')
                axiter.ax.set(title='Log Severity', yscale='log')
                axiter.ax.set(xlim=(0, max_lim))

            next(axiter).plot(self.xs, self.sev_density.cumsum(), drawstyle='steps-post')
            axiter.ax.set(title='Severity Distribution')
            axiter.ax.set(xlim=(0, max_lim))

            next(axiter).plot(self.xs, self.agg_density, label='aggregate_project')
            axiter.ax.plot(self.xs, self.sev_density, lw=0.5, drawstyle='steps-post', label='severity')
            axiter.ax.set(title='Aggregate')
            axiter.ax.legend()

            next(axiter).plot(self.xs, self.agg_density, label='aggregate_project')
            axiter.ax.set(title='Aggregate')

            next(axiter).plot(self.xs, self.agg_density, label='aggregate_project')
            axiter.ax.set(yscale='log', title='Aggregate, log scale')

            F = self.agg_density.cumsum()
            next(axiter).plot(self.xs, 1 - F)
            axiter.ax.set(title='Survival Function')

            next(axiter).plot(self.xs, 1 - F)
            axiter.ax.set(title='Survival Function, log scale', yscale='log')

            next(axiter).plot(1 - F, self.xs, label='aggregate_project')
            axiter.ax.plot(1 - self.sev_density.cumsum(), self.xs, label='severity')
            axiter.ax.set(title='Lee Diagram')
            axiter.ax.legend()

            # figure for extended plotting of return period:
            max_p = F[-1]
            if max_p > 0.9999:
                _n = 10
            else:
                _n = 5
            if max_p >= 1:
                max_p = 1 - 1e-10
            k = (max_p / 0.99) ** (1 / _n)
            extraps = 0.99 * k ** np.arange(_n)
            q = interpolate.interp1d(F, self.xs, kind='linear', fill_value='extrapolate', bounds_error=False)
            ps = np.hstack((np.linspace(0, 1, 100, endpoint=False), extraps))
            qs = q(ps)
            next(axiter).plot(1 / (1 - ps), qs)
            axiter.ax.set(title='Return Period', xscale='log')

        elif kind == 'oldquick':
            # original quick
            if self.dh_agg_density is not None:
                n = 4
            else:
                n = 3

            axiter = axiter_factory(axiter, n, figsize, aspect=aspect)

            F = np.cumsum(self.agg_density)
            mx = np.argmax(F > 1 - 1e-5)
            if mx == 0:
                mx = len(F) + 1
            else:
                mx += 1  # a little extra room
            dh_F = None
            if self.dh_agg_density is not None:
                dh_F = np.cumsum(self.dh_agg_density)
                mx = max(mx, np.argmax(dh_F > 1 - 1e-5))
                dh_F = dh_F[:mx]
            F = F[:mx]

            xs = self.xs[:mx]
            d = self.agg_density[:mx]
            sevF = np.cumsum(self.sev_density)
            sevF = sevF[:mx]
            f = self.sev_density[:mx]

            ax = next(axiter)
            # ? correct format?
            ax.plot(xs, d, label='agg', drawstyle='steps-post')
            ax.plot(xs, f, label='sev', drawstyle='steps-post')
            if np.sum(f > 1e-6) < 20:
                # if there are few points...highlight the points
                ax.plot(xs, f, 'o', label=None, )
            if self.dh_agg_density is not None:
                ax.plot(xs, self.dh_agg_density[:mx], label='dh {:} agg'.format(self.beta_name))
                ax.plot(xs, self.dh_sev_density[:mx], label='dh {:} sev'.format(self.beta_name))
            max_y = min(2 * np.max(d), np.max(f[1:])) * 1.05  # want some extra space...
            if max_y > 0:
                ax.set_ylim(0, max_y)
            ax.legend()
            ax.set_title('Density')
            ax = next(axiter)
            ax.plot(xs, d, label='agg')
            ax.plot(xs, f, label='sev')
            if self.dh_agg_density is not None:
                ax.plot(xs, self.dh_agg_density[:mx], label='dh {:} agg'.format(self.beta_name))
                ax.plot(xs, self.dh_sev_density[:mx], label='dh {:} sev'.format(self.beta_name))
            ax.set_yscale('log')
            ax.legend()
            ax.set_title('Log Density')

            ax = next(axiter)
            ax.plot(F, xs, label='Agg')
            ax.plot(sevF, xs, label='Sev')
            if self.dh_agg_density is not None:
                dh_F = np.cumsum(self.dh_agg_density[:mx])
                ax.plot(dh_F, xs, label='dh {:} agg'.format(self.beta_name))
            ax.legend()
            ax.set_title('Lee Diagram')

            if self.dh_agg_density is not None:
                # if dh computed graph comparision
                ax = next(axiter)
                ax.plot(1 - F, 1 - dh_F, label='g(S) vs S')
                ax.plot(1 - F, 1 - F, 'k', linewidth=.5, label=None)

            if set_tight:
                axiter.tidy()
                suptitle_and_tight(f'Aggregate {self.name}')

        else:
            # new quick no DH, colors!
            # TODO LIMITS!
            f = plt.figure(constrained_layout=True, figsize=figsize)
            axd = f.subplot_mosaic('ABC')

            if self.bs == 1:
                mx = self.q(1)
                span = mx // 6

                df = self.density_df[['p_total', 'p_sev', 'F', 'loss']].copy()
                df['sevF'] = df.p_sev.cumsum()
                df.loc[-0.5, :] = (0, 0, 0, 0, 0)
                df = df.sort_index()
                df.p_total.plot(ax=axd['A'], drawstyle='steps-mid', lw=2, label='Aggregate')
                df.p_sev.plot(ax=axd['A'], drawstyle='steps-mid', lw=1, label='Severity')
                axd['A'].set(xlim=[-mx / 25, mx + 1], title='Probability mass functions')
                axd['A'].legend()
                axd['A'].xaxis.set_major_locator(ticker.MultipleLocator(span))
                # for discrete plot F next
                df.F.plot(ax=axd['B'], drawstyle='steps-post', lw=2, label='Aggregate')
                df.p_sev.cumsum().plot(ax=axd['B'], drawstyle='steps-post', lw=1, label='Severity')
                axd['B'].set(xlim=[-mx / 25, mx + 1], title='Distribution functions')
                axd['B'].legend()
                axd['B'].xaxis.set_major_locator(ticker.MultipleLocator(span))

                # for Lee diagrams
                ax = axd['C']
                ax.plot(df.F, df.loss, drawstyle='steps-pre', lw=2, label='Aggregate')
                ax.plot(df.p_sev.cumsum(), df.loss, drawstyle='steps-pre', lw=1, label='Severity')
                ax.set(xlim=[-0.025, 1.025], ylim=[-mx / 25, mx + 1], title='Lee diagram')
                ax.legend()
            else:
                # continuous
                df = self.density_df
                mx = self.q(.999) + 0.5
                xlim = self.limits(stat='range', kind='linear')
                xlim2 = self.limits(stat='range', kind='log')

                ax = axd['A']
                df.p_total.plot(ax=ax, lw=2, label='Aggregate')
                df.p_sev.plot(ax=ax, lw=1, label='Severity')
                ax.set(xlim=xlim, title='Probability density')
                ax.legend()

                df.p_total.plot(ax=axd['B'], lw=2, label='Aggregate')
                df.p_sev.plot(ax=axd['B'], lw=1, label='Severity')
                axd['B'].set(xlim=xlim2, title='Log density', yscale='log')
                axd['B'].legend()

                ax = axd['C']
                ax.plot(df.F, df.loss, lw=2, label='Aggregate')
                ax.plot(df.p_sev.cumsum(), df.loss, lw=1, label='Severity')
                ax.set(xlim=[-0.02, 1.02], ylim=xlim, title='Lee (quantile) plot')
                ax.legend()

    def limits(self, stat='range', kind='linear', zero_mass='include'):
        """
        Suggest sensible plotting limits for kind=range, density, ..

        Should optionally return a locator for plots?

        Called by ploting routines. Single point of failure!

        Must work without q function when not computed (apply_reins_work for
        occ reins...uses report_ser instead).

        :param stat:  range or density
        :param kind:  linear or log (this is the y-axis, not log of range...that is rarely plotted)
        :param zero_mass:  include exclude, for densities
        :return:
        """

        # fudge l/r factors
        def f(x):
            fl, fr = 0.02, 1.02
            return [-fl * x, fr * x]

        # lower bound for log plots
        eps = 1e-16

        # if not computed
        # GOTCH: if you call q and it fails because not agg_density then q is set to {}
        # which is not None
        if self.agg_density is None:
            return f(self.report_ser[('agg', 'P99.9e')])

        if stat == 'range':
            if kind == 'linear':
                return f(self.q(0.999))
            else:
                # wider range for log density plots
                return f(self.q(0.99999))
        elif stat == 'density':
            mx = self.agg_density.max()
            mxx0 = self.agg_density[1:].max()
            if kind == 'linear':
                if zero_mass == 'include':
                    return f(mx)
                else:
                    return f(mxx0)
            else:
                return [eps, mx * 1.5]
        else:
            # if you fall through to here, wrong args
            raise ValueError(f'Inadmissible stat/kind passsed, expected range/density and log/linear.')

    def report(self, report_list='audit'):
        """
        statistics, quick or audit reports

        TODO better audit report?
        :param report_list:
        :return:
        """
        full_report_list = ['statistics', 'quick', 'audit']
        if report_list == 'all':
            report_list = full_report_list

        if 'quick' in report_list:
            html_title(f'{self.name} Quick Report (Theoretic)', 1, False)
            display(pd.DataFrame(self.report_ser).unstack())

        if 'audit' in report_list:
            if self.audit_df is not None:
                html_title(f'{self.name} Audit Report', 1, False)
                # massaged version of original audit_df
                df = self.audit_df.T.copy()
                # put empirical beside model for easier viewing
                df['empirical'] = np.nan
                df.loc[['sev_1', 'sev_cv', 'agg_m', 'agg_cv'], 'empirical'] = \
                    df.loc[['emp_sev_1', 'emp_sev_cv', 'emp_agg_1', 'emp_agg_cv'], 'mixed'].values
                df = df.iloc[:-4].fillna('')
                if df.shape[1] == 3:
                    # only one sev, don't show extra sev column
                    df = df.iloc[:, 1:]
                display(df.style.format(lambda x: x if type(x) == str else f'{x:,.3f}'))

        if 'statistics' in report_list:
            if len(self.statistics_df) > 1:
                df = pd.concat((self.statistics_df, self.statistics_total_df), axis=1)
            else:
                df = self.statistics_df
            html_title(f'{self.name} Statistics Report', 1, False)
            return df.T

    def describe(self):
        """
        Theoretic and empirical stats. Used in _repr_html_.

        """
        st = self.statistics_total_df.loc['mixed', :]
        sev_m = st.sev_m
        sev_cv = st.sev_cv
        sev_skew = st.sev_skew
        n_m = st.freq_m
        n_cv = st.freq_cv
        a_m = st.agg_m
        a_cv = st.agg_cv
        df = pd.DataFrame({'E(X)': [sev_m, n_m, a_m], 'CV(X)': [sev_cv, n_cv, a_cv],
                           'Skew(X)': [sev_skew, self.statistics_total_df.loc['mixed', 'freq_skew'], st.agg_skew]},
                          index=['Sev', 'Freq', 'Agg'])
        df.index.name = 'X'
        if self.audit_df is not None:
            esev_m = self.audit_df.loc['mixed', 'emp_sev_1']
            esev_cv = self.audit_df.loc['mixed', 'emp_sev_cv']
            ea_m = self.audit_df.loc['mixed', 'emp_agg_1']
            ea_cv = self.audit_df.loc['mixed', 'emp_agg_cv']
            df.loc['Sev', 'Est E(X)'] = esev_m
            df.loc['Agg', 'Est E(X)'] = ea_m
            df.loc[:, 'Err E(X)'] = df['Est E(X)'] / df['E(X)'] - 1
            df.loc['Sev', 'Est CV(X)'] = esev_cv
            df.loc['Agg', 'Est CV(X)'] = ea_cv
            df.loc[:, 'Err CV(X)'] = df['Est CV(X)'] / df['CV(X)'] - 1
            df = df[['E(X)', 'Est E(X)', 'Err E(X)', 'CV(X)', 'Est CV(X)', 'Err CV(X)', 'Skew(X)']]
        df = df.fillna('')
        return df

    def recommend_bucket(self, log2=10, verbose=False):
        """
        recommend a bucket size given 2**N buckets

        :param log2: log2 of number of buckets. log2=10 is default.
        :return:
        """
        N = 1 << log2
        if not verbose:
            moment_est = estimate_agg_percentile(self.agg_m, self.agg_cv, self.agg_skew) / N
            limit_est = self.limit.max() / N
            if limit_est == np.inf:
                limit_est = 0
            logger.debug(f'Agg.recommend_bucket | {self.name} moment: {moment_est}, limit {limit_est}')
            return max(moment_est, limit_est)
        else:
            for n in sorted({log2, 16, 13, 10}):
                rb = self.recommend_bucket(n)
                if n == log2:
                    rbr = rb
                print(f'Recommended bucket size with {2 ** n} buckets: {rb:,.0f}')
            if self.bs != 0:
                print(f'Bucket size set with {N} buckets at {self.bs:,.0f}')
            return rbr

    def q_old(self, p):
        """
        return lowest quantile, appropriate for discrete bucketing.
        quantile guaranteed to be in the index
        nearest does not work because you always want to pick rounding up

        Definition 2.1 (Quantiles)
        x(α) = qα(X) = inf{x ∈ R : P[X ≤ x] ≥ α} is the lower α-quantile of X
        x(α) = qα(X) = inf{x ∈ R : P[X ≤ x] > α} is the upper α-quantile of X.

        We use the x-notation if the dependence on X is evident, otherwise the q-notion.
        Acerbi and Tasche (2002)

        :param p:
        :return:
        """
        if self._q is None:
            self._q = interpolate.interp1d(self.density_df.F, self.density_df.loss, kind='linear')
        l = float(self._q(p))
        # find next nearest index value if not an exact match (this is slightly faster and more robust
        # than l/bs related math)
        l1 = self.density_df.index.get_loc(l, 'bfill')
        l1 = self.density_df.index[l1]
        return l1

    def q(self, p, kind='lower'):
        """
        Exact same code from Portfolio.q

        kind==middle reproduces middle_q

        :param p:
        :param kind:
        :return:
        """
        if self._linear_quantile_function is None:
            # revised Dec 2019
            try:
                self._linear_quantile_function = {}
                self.q_temp = self.density_df[['loss', 'F']].groupby('F').agg({'loss': np.min})
                self.q_temp.loc[1, 'loss'] = self.q_temp.loss.iloc[-1]
                self.q_temp.loc[0, 'loss'] = 0
                self.q_temp = self.q_temp.sort_index()
                # that q_temp left cts, want right continuous:
                self.q_temp['loss_s'] = self.q_temp.loss.shift(-1)
                self.q_temp.iloc[-1, 1] = self.q_temp.iloc[-1, 0]
                self._linear_quantile_function['upper'] = \
                    interpolate.interp1d(self.q_temp.index, self.q_temp.loss_s, kind='previous', bounds_error=False,
                                         fill_value='extrapolate')
                # Jan 2020 see note in Portfolio: changed previous to next
                self._linear_quantile_function['lower'] = \
                    interpolate.interp1d(self.q_temp.index, self.q_temp.loss, kind='next', bounds_error=False,
                                         fill_value='extrapolate')
                # changed to loss_s
                self._linear_quantile_function['middle'] = \
                    interpolate.interp1d(self.q_temp.index, self.q_temp.loss_s, kind='linear', bounds_error=False,
                                         fill_value='extrapolate')
            except Exception as e:
                # if fails reset in case this code is within a try .... except block
                self._linear_quantile_function = None
                raise e
        l = float(self._linear_quantile_function[kind](p))
        # because we are not interpolating the returned value must (should) be in the index...
        if not (kind == 'middle' or l in self.density_df.index):
            logger.error(f'Unexpected weirdness in {self.name} quantile...computed {p}th {kind} percentile as {l} '
                         'which is not in the index but is expected to be. Make sure bs has nice binary expansion!')
        return l

    def careful_q(self, p):
        """
        careful calculation of q handling jumps (based of SRM_Examples Noise class originally).
        Note this is automatically vectorized and returns and array whereas q isn't.
        It doesn't necessarily return an element of the index.

        Just for reference here is code to illustrate the problem. This code is used in Vig_0_Audit.ipynb.

            uw = agg.Underwriter(create_all=True)

            def plot_eg_agg(b, e, w, n=32, axs=None, x_range=1):
                '''
                makes a tricky distribution function with a poss isolated jump
                creates an agg object and checks the quantile function is correct

                mass at w

                '''

                if axs is None:
                    f, axs0 = plt.subplots(2,3, figsize=(9,6))
                    axs = iter(axs0.flatten())

                tm = np.linspace(0, 1, 33)
                tf = lambda x : f'{32*x:.0f}'

                def pretty(axis, ticks, formatter):
                    maj = ticks[::4]
                    mnr = [i for i in ticks if i not in maj]
                    labels = [formatter(i) for i in maj]
                    axis.set_ticks(maj)
                    axis.set_ticks(mnr, True)
                    axis.set_ticklabels(labels)
                    axis.grid(True, 'major', lw=0.707, c='lightblue')
                    axis.grid(True, 'minor', lw=0.35, c='lightblue')

                # make the distribution
                xs = np.linspace(0, x_range, n+1)
                Fx = np.zeros_like(xs)
                Fx[b:13] = 1
                Fx[20:e] = 1
                Fx[w] = 32 - np.sum(Fx)
                Fx = Fx / Fx.sum()
                Fx = np.cumsum(Fx)

                # make an agg version: find the jumps and create a dhistogram
                temp = pd.DataFrame(dict(x=xs, F=Fx))
                temp['f'] = np.diff(temp.F, prepend=0)
                temp = temp.query('f > 0')
                pgm = f'agg Tricky 1 claim sev dhistogram xps {temp.x.values} {temp.f.values} fixed'
                a = uw(pgm)
                a.easy_update(10, 0.001)
                # plot
                a.plot(axiter=axs)
                pretty(axs0[0,0].xaxis, tm, tf)
                pretty(axs0[0,2].xaxis, tm, tf)
                pretty(axs0[0,2].yaxis, tm, tf)

                # lower left plot: distribution function
                ax = next(axs)
                ax.step(xs, Fx, where='post', marker='.')
                ax.plot(a.xs, a.agg_density.cumsum(), linewidth=3, alpha=0.5, label='from agg')
                ax.set(title=f'b={b}, e={e}, w={w}', ylim=-0.05, aspect='equal')
                if x_range  == 1:
                    ax.set(aspect='equal')
                ax.legend(frameon=False, loc='upper left')
                pretty(ax.xaxis, tm, tf)
                pretty(ax.yaxis, tm, tf)

                # lower middle plot
                ps = np.linspace(0, 1, 301)
                agg_careful = a.careful_q(ps)
                ax = next(axs)
                ax.step(Fx, xs, where='pre', marker='.', label='input')
                ax.plot(Fx, xs, ':', label='input joined')
                ax.plot(ps, agg_careful, linewidth=1, label='agg careful')
                ax.set(title='Inverse', ylim=-0.05)
                if x_range  == 1:
                    ax.set(aspect='equal')
                pretty(ax.xaxis, tm, tf)
                pretty(ax.yaxis, tm, tf)
                ax.legend()

                # lower right plot
                ax = next(axs)
                dmq = np.zeros_like(ps)
                for i, p in enumerate(ps):
                    try:
                        dmq[i] = a.q(p)
                    except:
                        dmq[i] = 0
                ax.plot(ps, agg_careful, label='careful (agg obj)', linewidth=1, alpha=1)
                ax.plot(ps, dmq, label='agg version')
                ax.legend(frameon=False, loc='upper left')
                pretty(ax.xaxis, tm, tf)
                pretty(ax.yaxis, tm, tf)
                ax.set(title='Check with agg version')

                plt.tight_layout()

                return a

            aw = plot_eg_agg(6, 29, 16)

        :param p: single or vector of values of ps, 0<1
        :return:  quantiles
        """
        if self._careful_q is None:
            self._careful_q = CarefulInverse.dist_inv1d(self.xs, self.agg_density)

        return self._careful_q(p)

    def tvar(self, p, kind='interp'):
        """
        Compute the tail value at risk at threshold p

        Definition 2.6 (Tail mean and Expected Shortfall)
        Assume E[X−] < ∞. Then
        x¯(α) = TM_α(X) = α^{−1}E[X 1{X≤x(α)}] + x(α) (α − P[X ≤ x(α)])
        is α-tail mean at level α the of X.
        Acerbi and Tasche (2002)

        We are interested in the right hand exceedence [?? note > vs ≥]
        α^{−1}E[X 1{X > x(α)}] + x(α) (P[X ≤ x(α)] − α)

        McNeil etc. p66-70 - this follows from def of ES as an integral
        of the quantile function

        q is exact quantile (most of the time)
        q1 is the smallest index element (bucket multiple) greater than or equal to q

        tvar integral is int_p^1 q(s)ds = int_q^infty xf(x)dx = q + int_q^infty S(x)dx
        we use the last approach. np.trapz approxes the integral. And the missing piece
        between q and q1 approx as a trapezoid too.

        :param p:
        :param kind:
        :return:
        """
        # match Portfolio method
        assert self.density_df is not None

        if kind == 'tail':
            # original
            # _var = self.q(p)
            # ex = self.density_df.loc[_var + self.bs:, ['p_total', 'loss']].product(axis=1).sum()
            # pip = (self.density_df.loc[_var, 'F'] - p) * _var
            # t_var_old = 1 / (1 - p) * (ex + pip)
            # revised
            if self._tail_var2 is None:
                self._tail_var2 = self.density_df[['p_total', 'loss']].product(axis=1).iloc[::-1].cumsum().iloc[::-1]
            _var = self.q(p)
            ex = self._tail_var2.loc[_var + self.bs]
            pip = (self.density_df.loc[_var, 'F'] - p) * _var
            t_var = 1 / (1 - p) * (ex + pip)
            return t_var
        elif kind == 'interp':
            # original implementation interpolated
            if self._tail_var is None:
                # make tvar function
                sup = (self.density_df.p_total[::-1] > 0).idxmax()
                if sup == self.density_df.index[-1]:
                    sup = np.inf
                    _x = self.density_df.F
                    _y = self.density_df.exgta
                else:
                    _x = self.density_df.F.values[:self.density_df.index.get_loc(sup)]
                    _y = self.density_df.exgta.values[:self.density_df.index.get_loc(sup)]
                p0 = self.density_df.at[0.0, 'F']
                if p0 > 0:
                    ps = np.linspace(0, p0, 200, endpoint=False)
                    tempx = np.hstack((ps, _x))
                    tempy = np.hstack((self.ex / (1 - ps), _y))
                    self._tail_var = interpolate.interp1d(tempx, tempy,
                                                          kind='linear', bounds_error=False,
                                                          fill_value=(self.ex, sup))
                else:
                    self._tail_var = interpolate.interp1d(_x, _y, kind='linear', bounds_error=False,
                                                          fill_value=(self.ex, sup))
            if type(p) in [float, np.float]:
                return float(self._tail_var(p))
            else:
                return self._tail_var(p)
        elif kind == 'inverse':
            if self._inverse_tail_var is None:
                # make tvar function
                self._inverse_tail_var = interpolate.interp1d(self.density_df.exgta, self.density_df.F,
                                                              kind='linear', bounds_error=False,
                                                              fill_value='extrapolate')
            if type(p) in [int, np.int, float, np.float]:
                return float(self._inverse_tail_var(p))
            else:
                return self._inverse_tail_var(p)
        else:
            raise ValueError(f'Inadmissible kind passed to tvar; options are interp (default) or tail')

        # original version
        # function not vectorized
        # q = float(self.q(p, 'middle'))
        # l1 = self.density_df.index.get_loc(q, 'bfill')
        # q1 = self.density_df.index[l1]
        #
        # i1 = np.trapz(self.density_df.loc[q1:, 'S'], dx=self.bs)
        # i2 = (q1 - q) * (2 - p - self.density_df.at[q1, 'F']) / 2  # trapz adj for first part
        # return q + (i1 + i2) / (1 - p)

    def cdf(self, x):
        """
        return cumulative probability distribution using linear interpolation

        :param x: loss size
        :return:
        """
        if self._cdf is None:
            self._cdf = interpolate.interp1d(self.xs, self.agg_density.cumsum(), kind='linear',
                                             bounds_error=False, fill_value='extrapolate')
        return self._cdf(x)

    def sf(self, x):
        """
        return survival function using linear interpolation

        :param x: loss size
        :return:
        """
        return 1 - self.cdf(x)

    def pdf(self, x):
        """
        probability density function, assuming a continuous approximation of the bucketed density
        :param x:
        :return:
        """
        if self._pdf is None:
            self._pdf = interpolate.interp1d(self.xs, self.agg_density, kind='linear',
                                             bounds_error=False, fill_value='extrapolate')
        return self._pdf(x) / self.bs

    def json(self):
        """
        write in json
        :return:
        """
        return json.dumps(self._spec)

    def entropy_fit(self, n_moments, tol=1e-10, verbose=False):
        """
        Find the max entropy fit to the aggregate based on n_moments fit
        The constant is added (sum of probabilities constraint), for two
        moments there are n_const = 3 constrains.

        Based on discussions with and R code from Jon Evans

        Run ::

            ans = obj.entropy_fit(2)
            ans['ans_df'].plot()

        to compare the fits.

        :param n_moments: number of moments to match
        :param tol:
        :param verbose:
        :return:
        """
        # sum of probs constraint
        n_constraints = n_moments + 1

        # don't want to mess up the object...
        xs = self.xs.copy()
        p = self.agg_density.copy()
        # more aggressively de-fuzz
        p = np.where(abs(p) < 1e-16, 0, p)
        p = p / np.sum(p)
        p1 = p.copy()

        mtargets = np.zeros(n_constraints)
        for i in range(n_constraints):
            mtargets[i] = np.sum(p)
            p *= xs

        parm1 = np.zeros(n_constraints)
        x = np.array([xs ** i for i in range(n_constraints)])

        probs = np.exp(-x.T @ parm1)
        machieved = x @ probs
        der1 = -(x * probs) @ x.T

        er = 1
        iters = 0
        while er > tol:
            iters += 1
            try:
                parm1 = parm1 - inv(der1) @ (machieved - mtargets)
            except np.linalg.LinAlgError:
                print('Singluar matrix')
                print(der1)
                return None
            probs = np.exp(-x.T @ parm1)
            machieved = x @ probs
            der1 = -(x * probs) @ x.T
            er = (machieved - mtargets).dot(machieved - mtargets)
            if verbose:
                print(f'Error: {er}\nParameter {parm1}')
        ans = pd.DataFrame(dict(xs=xs, agg=p1, fit=probs))
        ans = ans.set_index('xs')
        return dict(params=parm1, machieved=machieved, mtargets=mtargets, ans_df=ans)

    def var_dict(self, p, kind='lower', snap=False):
        """
        make a dictionary of value at risks for the line, mirrors Portfolio.var_dict.
        Here is just marshalls calls to the appropriate var or tvar function

        No epd. Allows the price function to run consistently with Portfolio version.

        Example:

            for p, arg in zip([.996, .996, .996, .985, .01], ['var', 'lower', 'upper', 'tvar', 'epd']):
                print(port.var_dict(p, arg,  snap=True))

        :param p:
        :param kind: var (defaults to lower), upper, lower, tvar
        :param snap: snap tvars to index
        :return:
        """
        if kind == 'var': kind = 'lower'
        if kind == 'tvar':
            d = {self.name: self.tvar(p)}
        else:
            d = {self.name: self.q(p, kind)}
        if snap and kind == 'tvar':
            d = {self.name: self.snap(d[self.name])}
        return d

    def price(self, p, g, kind='var'):
        """
        Price using regulatory and pricing g functions, mirroring Portfolio.price.
        Unlike Portfolio, cannot calibrate. Applying specified Distortions only.
        If calibration is needed, embed Aggregate in a one-line Portfolio object.

            Compute E_price (X wedge E_reg(X) ) where E_price uses the pricing distortion and E_reg uses
            the regulatory distortion

            regulatory capital distortion is applied on unlimited basis: ``reg_g`` can be:

            * if input < 1 it is a number interpreted as a p value and used to determine VaR capital
            * if input > 1 it is a directly input  capital number
            * d dictionary: Distortion; spec { name = dist name | var, shape=p value a distortion used directly

            ``pricing_g`` is  { name = ph|wang and shape=}, if shape (lr or roe not allowed; require calibration).

            if ly it must include ro in spec

        :param p: a distortion function spec or just a number; if >1 assets, if <1 a prob converted to quantile
        :param kind: var lower upper tvar
        :param g:  pricing distortion function
        :return:
        """

        # figure regulatory assets; applied to unlimited losses
        vd = self.var_dict(p, kind, snap=True)
        a_reg = vd[self.name]

        # figure pricing distortion
        if isinstance(g, Distortion):
            # just use it
            pass
        else:
            # Distortion spec as dict
            g = Distortion(**g)

        self.apply_distortion(g)
        aug_row = self.density_df.loc[a_reg]

        # holder for the answer
        df = pd.DataFrame(columns=['line', 'L', 'P', 'M', 'Q'], dtype=float)
        df.columns.name = 'statistic'
        df = df.set_index('line', drop=True)

        el = aug_row['exa']
        P = aug_row['exag']
        M = P - el
        Q = a_reg - P

        df.loc[self.name, :] = [el, P, M, Q]
        df['a'] = a_reg
        df['LR'] = df.L / df.P
        df['PQ'] = df.P / df.Q
        df['ROE'] = df.M / df.Q
        # ap = namedtuple('AggregatePricing', ['df', 'distortion'])
        # return ap(df, g)  # kinda dumb...
        return df


class Severity(ss.rv_continuous):
    """

    A continuous random variable, subclasses ``scipy.statistics_df.rv_continuous``.

    adds layer and attachment to scipy statistics_df continuous random variable class
    overrides

    * cdf
    * pdf
    * isf
    * ppf
    * moments

    Should consider over-riding: sf, **statistics_df** ?munp

    TODO issues remain using numerical integration to compute moments for distributions having
    infinite support and a low standard deviation. See logger for more information in particular
    cases.

    """

    def __init__(self, sev_name, exp_attachment=0, exp_limit=np.inf, sev_mean=0, sev_cv=0, sev_a=0, sev_b=0,
                 sev_loc=0, sev_scale=0, sev_xs=None, sev_ps=None, sev_conditional=True, name='', note=''):
        """

        :param sev_name: scipy statistics_df continuous distribution | (c|d)histogram  cts or discerte | fixed
        :param exp_attachment:
        :param exp_limit:
        :param sev_mean:
        :param sev_cv:
        :param sev_a:
        :param sev_b:
        :param sev_loc:
        :param sev_scale:
        :param sev_xs: for fixed or histogram classes
        :param sev_ps:
        :param sev_conditional: conditional or unconditional; for severities use conditional
        """

        from .port import Portfolio

        ss.rv_continuous.__init__(self, name=f'{sev_name}[{exp_limit} xs {exp_attachment:,.0f}]')
        # I think this is preferred now, but these are the same (probably...)
        # super().__init__(name=f'{sev_name}[{exp_limit} xs {exp_attachment:,.0f}]')
        self.limit = exp_limit
        self.attachment = exp_attachment
        self.detachment = exp_limit + exp_attachment
        self.fz = None
        self.pattach = 0
        self.pdetach = 0
        self.conditional = sev_conditional
        self.sev_name = sev_name
        self.name = name
        self.long_name = f'{sev_name}[{exp_limit} xs {exp_attachment:,.0f}'
        self.note = note
        self.sev1 = self.sev2 = self.sev3 = None
        logger.debug(
            f'Severity.__init__  | creating new Severity {self.sev_name} at {super(Severity, self).__repr__()}')
        # there are two types: if sev_xs and sev_ps provided then fixed/histogram, else scpiy dist
        # allows you to define fixed with just xs=1 (no log)
        if sev_xs is not None:
            if sev_name == 'fixed':
                # fixed is a special case of dhistogram with just one point
                sev_name = 'dhistogram'
                sev_ps = np.array(1)
            assert sev_name[1:] == 'histogram'
            # TODO: make histogram work with exp_limit and exp_attachment; currently they are ignored
            try:
                xs, ps = np.broadcast_arrays(np.array(sev_xs), np.array(sev_ps))
            except ValueError:
                # for empirical
                logger.warning(f'Severity.init | {sev_name} sev_xs and sev_ps cannot be broadcast')
                xs = np.array(sev_xs)
                ps = np.array(sev_ps)
            if not np.isclose(np.sum(ps), 1.0):
                logger.error(f'Severity.init | {sev_name} histogram/fixed severity with probs do not sum to 1, '
                             f'{np.sum(ps)}')
            # need to exp_limit distribution
            exp_limit = min(np.min(exp_limit), xs.max())
            if sev_name == 'chistogram':
                # continuous histogram: uniform between xs's
                # if the inputs are not evenly spaced this messes up because it interprets p as the
                #  height of the density over the range...hence have to rescale
                #  it DOES NOT matter that the p's add up to 1...that is handled automatically
                # changed 1 to -2 so the last bucket is bigger WHY SORTED???
                if len(xs) == len(ps):
                    xss = np.sort(np.hstack((xs, xs[-1] + xs[-2])))
                else:
                    # allows to pass in with the right hand end specified
                    xss = xs
                aps = ps / np.diff(xss)
                # this is now slightly bigger
                exp_limit = min(np.min(exp_limit), xss.max())
                # midpoints
                xsm = (xss[:-1] + xss[1:]) / 2
                self.sev1 = np.sum(xsm * ps)
                self.sev2 = np.sum(xsm ** 2 * ps)
                self.sev3 = np.sum(xsm ** 3 * ps)
                self.fz = ss.rv_histogram((aps, xss))
            elif sev_name == 'dhistogram':
                # discrete histogram: point masses at xs's
                self.sev1 = np.sum(xs * ps)
                self.sev2 = np.sum(xs ** 2 * ps)
                self.sev3 = np.sum(xs ** 3 * ps)
                # binary consistent
                xss = np.sort(np.hstack((xs - 2 ** -14, xs)))  # was + but F(x) = Pr(X<=x) so seems shd be to left
                pss = np.vstack((ps, np.zeros_like(ps))).reshape((-1,), order='F')[:-1]
                self.fz = ss.rv_histogram((pss, xss))
            else:
                raise ValueError('Histogram must be chistogram (continuous) or dhistogram (discrete)'
                                 f', you passed {sev_name}')

        elif isinstance(sev_name, Severity):
            self.fz = sev_name

        elif not isinstance(sev_name, (str, np.str_)):
            # must be a meta object - replaced in Underwriter.write
            log2 = sev_a
            bs = sev_b  # if zero it is happy to take whatever....
            if isinstance(sev_name, Aggregate):
                if log2 and (log2 != sev_name.log2 or (bs != sev_name.bs and bs != 0)):
                    # recompute
                    sev_name.easy_update(log2, bs)
                xs = sev_name.xs
                ps = sev_name.agg_density
            elif isinstance(sev_name, Portfolio):
                if log2 and (log2 != sev_name.log2 or (bs != sev_name.bs and bs != 0)):
                    # recompute
                    sev_name.update(log2, bs, add_exa=False)
                xs = sev_name.density_df.loss.values
                ps = sev_name.density_df.p_total.values
            else:
                raise ValueError(f'Object {sev_name} passed as a proto-severity type but'
                                 f' only Aggregate, Portfolio and Severity objects allowed')
            # will make as a combo discrete/continuous histogram
            # nail the bucket at zero and use a continuous approx +/- bs/2 around each other bucket
            # leaves an ugly gap between 0 and bs/2...which is ignored
            b1size = 1e-7  # size of the first "bucket"
            xss = np.hstack((-bs * b1size, 0, xs[1:] - bs / 2, xs[-1] + bs / 2))
            pss = np.hstack((ps[0] / b1size, 0, ps[1:]))
            self.fz = ss.rv_histogram((pss, xss))
            self.sev1 = np.sum(xs * ps)
            self.sev2 = np.sum(xs ** 2 * ps)
            self.sev3 = np.sum(xs ** 3 * ps)

        elif sev_name in ['norm', 'expon', 'uniform']:
            # distributions with no shape parameters
            #     Normal (and possibly others) does not have a shape parameter
            if sev_loc == 0 and sev_mean > 0:
                sev_loc = sev_mean
            if sev_scale == 0 and sev_cv > 0:
                sev_scale = sev_cv * sev_loc
            gen = getattr(ss, sev_name)
            self.fz = gen(loc=sev_loc, scale=sev_scale)

        elif sev_name in ['beta']:
            # distributions with two shape parameters
            # require specific inputs
            # for Kent examples input sev_scale=maxl, sev_mean=el and sev_cv as input
            #     beta sev_a and sev_b params given expected loss, max loss exposure and sev_cv
            #     Kent E.'s specification. Just used to create the CAgg classes for his examples (in agg.examples)
            #     https://en.wikipedia.org/wiki/Beta_distribution#Two_unknown_parameters
            if sev_name == 'beta' and sev_mean > 0 and sev_cv > 0:
                m = sev_mean / sev_scale
                v = m * m * sev_cv * sev_cv
                sev_a = m * (m * (1 - m) / v - 1)
                sev_b = (1 - m) * (m * (1 - m) / v - 1)
                self.fx = ss.beta(sev_a, sev_b, loc=0, scale=sev_scale)
            else:
                gen = getattr(ss, sev_name)
                self.fz = gen(sev_a, sev_b, loc=sev_loc, scale=sev_scale)
        else:
            # distributions with one shape parameter
            # TODO assumes 0 is an invalid shape parameter....
            if sev_a == 0:
                sev_a, _ = self.cv_to_shape(sev_cv)
            if sev_scale == 0 and sev_mean > 0:
                sev_scale, self.fz = self.mean_to_scale(sev_a, sev_mean, sev_loc)
            else:
                gen = getattr(ss, sev_name)
                self.fz = gen(sev_a, scale=sev_scale, loc=sev_loc)

        if self.detachment == np.inf:
            self.pdetach = 0
        else:
            self.pdetach = self.fz.sf(self.detachment)

        if self.attachment == 0:
            self.pattach = 1
        else:
            self.pattach = self.fz.sf(self.attachment)

        if sev_mean > 0 or sev_cv > 0:
            # if you input a sev_mean or sev_cv check we are close to target
            st = self.fz.stats('mv')
            m = st[0]
            acv = st[1] ** .5 / m  # achieved sev_cv
            # sev_loc added so you can write lognorm 5 cv .3 + 10 a shifted lognorm mean 5
            if sev_mean > 0 and not np.isclose(sev_mean + sev_loc, m):
                print(f'WARNING target mean {sev_mean} and achieved mean {m} not close')
                # assert (np.isclose(sev_mean, m))
            if sev_cv > 0 and not np.isclose(sev_cv * sev_mean / (sev_mean + sev_loc), acv):
                print(f'WARNING target cv {sev_cv} and achieved cv {acv} not close')
                # assert (np.isclose(sev_cv, acv))
            # print('ACHIEVED', sev_mean, sev_cv, m, acv, self.fz.statistics_df(), self._stats())
            logger.debug(
                f'Severity.__init__ | parameters {sev_a}, {sev_scale}: target/actual {sev_mean} vs {m};  '
                f'{sev_cv} vs {acv}')

        if exp_limit < np.inf or exp_attachment > 0:
            layer_text = f'[{exp_limit:,.0f}' if exp_limit != np.inf else "Unlimited"
            layer_text += f' xs {exp_attachment:,.0f}]'
        else:
            layer_text = ''
        try:
            self.long_name = f'{name}: {sev_name}({self.fz.arg_dict[0]:.2f}){layer_text}'
        except:
            # 'rv_histogram' object has no attribute 'arg_dict'
            self.long_name = f'{name}: {sev_name}{layer_text}'

        assert self.fz is not None

    def __repr__(self):
        """
        wrap default with name
        :return:
        """
        return f'{super(Severity, self).__repr__()} of type {self.sev_name}'

    def cv_to_shape(self, cv, hint=1):
        """
        create a frozen object of type dist_name with given cv

        lognormal, gamma, inverse gamma and inverse gaussian solved analytically.

        Other distributions solved numerically and may be unstable.

        :param cv:
        :param hint:
        :return:
        """
        # some special cases we can handle:
        if self.sev_name == 'lognorm':
            shape = np.sqrt(np.log(cv * cv + 1))
            fz = ss.lognorm(shape)
            return shape, fz

        if self.sev_name == 'gamma':
            shape = 1 / (cv * cv)
            fz = ss.gamma(shape)
            return shape, fz

        if self.sev_name == 'invgamma':
            shape = 1 / cv ** 2 + 2
            fz = ss.invgamma(shape)
            return shape, fz

        if self.sev_name == 'invgauss':
            shape = cv ** 2
            fz = ss.invgauss(shape)
            return shape, fz

        # pareto with loc=-1 alpha = 2 cv^2  / (cv^2 - 1)

        gen = getattr(ss, self.sev_name)

        def f(shape):
            fz0 = gen(shape)
            temp = fz0.stats('mv')
            return cv - temp[1] ** .5 / temp[0]

        try:
            ans = newton(f, hint)
        except RuntimeError:
            logger.error(f'cv_to_shape | error for {self.sev_name}, {cv}')
            ans = np.inf
            return ans, None
        fz = gen(ans)
        return ans, fz

    def mean_to_scale(self, shape, mean, loc=0):
        """
        adjust scale of fz to have desired mean
        return frozen instance

        :param shape:
        :param mean:
        :param loc: location parameter (note: location is added to the mean...)
        :return:
        """
        gen = getattr(ss, self.sev_name)
        fz = gen(shape)
        m = fz.stats('m')
        scale = mean / m
        fz = gen(shape, scale=scale, loc=loc)
        return scale, fz

    def __enter__(self):
        """ Support with Severity as f: """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        del self

    def _pdf(self, x, *args):
        if self.conditional:
            return np.where(x > self.limit, 0,
                            np.where(x == self.limit, np.inf if self.pdetach > 0 else 0,
                                     self.fz.pdf(x + self.attachment) / self.pattach))
        else:
            if self.pattach < 1:
                return np.where(x < 0, 0,
                                np.where(x == 0, np.inf,
                                         np.where(x == self.detachment, np.inf,
                                                  np.where(x > self.detachment, 0,
                                                           self.fz.pdf(x + self.attachment, *args)))))
            else:
                return np.where(x < 0, 0,
                                np.where(x == self.detachment, np.inf,
                                         np.where(x > self.detachment, 0,
                                                  self.fz.pdf(x + self.attachment, *args))))

    def _cdf(self, x, *args):
        if self.conditional:
            return np.where(x > self.limit, 1,
                            np.where(x < 0, 0,
                                     (self.fz.cdf(x + self.attachment) - (1 - self.pattach)) / self.pattach))
        else:
            return np.where(x < 0, 0,
                            np.where(x == 0, 1 - self.pattach,
                                     np.where(x > self.limit, 1,
                                              self.fz.cdf(x + self.attachment, *args))))

    def _sf(self, x, *args):
        if self.conditional:
            return np.where(x > self.limit, 0,
                            np.where(x < 0, 1,
                                     self.fz.sf(x + self.attachment, *args) / self.pattach))
        else:
            return np.where(x < 0, 1,
                            np.where(x == 0, self.pattach,
                                     np.where(x > self.limit, 0,
                                              self.fz.sf(x + self.attachment, *args))))

    def _isf(self, q, *args):
        if self.conditional:
            return np.where(q < self.pdetach / self.pattach, self.limit,
                            self.fz.isf(q * self.pattach) - self.attachment)
        else:
            return np.where(q >= self.pattach, 0,
                            np.where(q < self.pdetach, self.limit,
                                     self.fz.isf(q, *args) - self.attachment))

    def _ppf(self, q, *args):
        if self.conditional:
            return np.where(q > 1 - self.pdetach / self.pattach, self.limit,
                            self.fz.ppf(1 - self.pattach * (1 - q)) - self.attachment)
        else:
            return np.where(q <= 1 - self.pattach, 0,
                            np.where(q > 1 - self.pdetach, self.limit,
                                     self.fz.ppf(q, *args) - self.attachment))

    def _stats(self, *args, **kwds):
        ex1, ex2, ex3 = self.moms()
        var = ex2 - ex1 ** 2
        skew = (ex3 - 3 * ex1 * ex2 + 2 * ex1 ** 3) / var ** 1.5
        return np.array([ex1, var, skew, np.nan])

    def moms(self):
        """
        revised moments for Severity class. Trying to compute moments of

            X(a,d) = min(d, (X-a)+)

        ==> E[X(a,d)^n] = int_a^d (x-a)^n f(x) dx + (d-a)^n S(d).

        Let x = q(p), F(x) = p, f(x)dx = dp.

        E[X(a,d)^n] = int_{F(a)}^{F(d)} (q(p)-a)^n dp + (d-a)^n S(d)

        The base is to compute int_{F(a)}^{F(d)} q(p)^n dp. These are exi below. They are then adjusted to create
        the moments needed.

        Old moments tried to compute int S(x)dx, but that is over a large, non-compact domain and
        did not work so well. With 0.9.3 old_moms was removed. Old_moms code did this:

            ex1 = safe_integrate(lambda x: self.fz.sf(x), 1)
            ex2 = safe_integrate(lambda x: 2 * (x - self.attachment) * self.fz.sf(x), 2)
            ex3 = safe_integrate(lambda x: 3 * (x - self.attachment) ** 2 * self.fz.sf(x), 3)

        # tests examples
        def test(mu, sigma, a, y):
            global moms
            import types
            # analytic with no layer attachment
            fz = ss.lognorm(sigma, scale=np.exp(mu))
            tv = np.array([np.exp(k*mu + k * k * sigma**2/2) for k in range(1,4)])

            # old method
            s = agg.Severity('lognorm', sev_a=sigma, sev_scale=np.exp(mu), exp_attachment=a, exp_limit=y)
            est = np.array(s.old_moms())

            # swap out moment routine
            setattr(s, moms.__name__, types.MethodType(moms, s))
            ans = np.array(s.moms())

            # summarize and report
            sg = f'Example: mu={mu}  sigma={sigma}  a={a}  y={y}'
            print(f'{sg}\n{"="*len(sg)}')
            print(pd.DataFrame({'new_ans' : ans, 'old_ans': est, 'err': ans/est-1, 'no_la_analytic' : tv}))


        test(8.7, .5, 0, np.inf)
        test(8.7, 2.5, 0, np.inf)
        test(8.7, 2.5, 10e6, 200e6)

        Example: mu=8.7  sigma=0.5  a=0  y=inf
        ======================================
                new_ans       old_ans           err  no_la_analytic
        0  6.802191e+03  6.802191e+03  3.918843e-11    6.802191e+03
        1  5.941160e+07  5.941160e+07  3.161149e-09    5.941160e+07
        2  6.662961e+11  6.662961e+11  2.377354e-08    6.662961e+11

        Example: mu=8.7  sigma=2.5  a=0  y=inf [here OLD METHOD WAS POOR]
        ======================================
                new_ans       old_ans           err  no_la_analytic
        0  1.366256e+05  1.366257e+05 -6.942541e-08    1.366257e+05
        1  9.663487e+12  1.124575e+11  8.493016e+01    9.669522e+12
        2  2.720128e+23  7.597127e+19  3.579469e+03    3.545017e+23

        Example: mu=8.7  sigma=2.5  a=10000000.0  y=200000000.0
        =======================================================
                new_ans       old_ans           err  no_la_analytic
        0  1.692484e+07  1.692484e+07  2.620126e-14    1.366257e+05
        1  1.180294e+15  1.180294e+15  5.242473e-13    9.669522e+12
        2  1.538310e+23  1.538310e+23  9.814372e-14    3.545017e+23

        """

        def safe_integrate(f, lower, upper, level):
            """ remember, you are integrating Survival funciton """

            argkw = dict(limit=100, epsabs=1e-6, epsrel=1e-6, full_output=1)
            ex = quad(f, lower, upper, **argkw)
            if len(ex) == 4 or ex[0] == np.inf:  # 'The integral is probably divergent, or slowly convergent.':
                msg = ex[-1].replace("\n", " ") if ex[-1] == str else "no message"
                logger.warning(
                    f'Severity.moms | ansr={ex[0]}, message {msg} ->')
                # this is too slow...and we don't really use it...
                ϵ = 0.001
                if lower == 0 and upper > ϵ:
                    logger.warning(
                        f'Severity.moms | splitting {self.sev_name} EX^{level} integral for convergence reasons')
                    exa = quad(f, 1e-16, ϵ, **argkw)
                    exb = quad(f, ϵ, upper, **argkw)
                    if len(exa) == 4:
                        msg = exa[-1].replace("\n", " ")
                        logger.warning(f'Severity.moms | [ϵ, 0.01] split EX^{level} integral returned {msg}')
                    if len(exb) == 4:
                        msg = exb[-1].replace("\n", " ")
                        logger.warning(f'Severity.moms | [ϵ, 1] split EX^{level} integral returned {msg}')
                    ex = (exa[0] + exb[0], exa[1] + exb[1])
            ex = ex[0]
            return ex

        # we integrate isf not q, so upper and lower are swapped
        if self.attachment == 0:
            upper = 1
        else:
            upper = self.fz.sf(self.attachment)
        if self.detachment == np.inf:
            lower = 0
        else:
            lower = self.fz.sf(self.detachment)

        # compute moments: histograms are tricky to integrate and we know the answer already...so
        if self.attachment == 0 and self.detachment == np.inf and self.sev_name.endswith('histogram'):
            ex1 = self.sev1
            ex2 = self.sev2
            ex3 = self.sev3
        else:
            ex1 = safe_integrate(self.fz.isf, lower, upper, 1)
            ex2 = safe_integrate(lambda x: self.fz.isf(x) ** 2, lower, upper, 2)
            ex3 = safe_integrate(lambda x: self.fz.isf(x) ** 3, lower, upper, 3)

        # adjust
        dma = self.detachment - self.attachment
        uml = upper - lower
        a = self.attachment
        if a > 0:
            ex1a = ex1 - a * uml
            ex2a = ex2 - 2 * a * ex1 + a ** 2 * uml
            ex3a = ex3 - 3 * a * ex2 + 3 * a ** 2 * ex1 - a ** 3 * uml
        else:
            ex1a = ex1
            ex2a = ex2
            ex3a = ex3

        if self.detachment < np.inf:
            ex1a += dma * lower
            ex2a += dma ** 2 * lower
            ex3a += dma ** 3 * lower

        if self.conditional:
            ex1a /= self.pattach
            ex2a /= self.pattach
            ex3a /= self.pattach

        return ex1a, ex2a, ex3a

    def plot(self, N=100, figsize=(12, 3)):
        """
        quick plot, updated for 0.9.3 with mosaic and no grid lines. (F(x), x) plot
        replaced with log density plot.

        TODO better coordination of figsize! Better axis formats and ranges.

        :param N:
        :param figsize:
        :return:
        """

        xs = np.linspace(0, self._isf(1e-4), N)
        xs2 = np.linspace(0, self._isf(1e-12), N)

        f = plt.figure(constrained_layout=True, figsize=figsize)
        axd = f.subplot_mosaic('ABCD')

        ds = 'steps-post' if self.sev_name == 'dhistogram' else 'default'

        ax = axd['A']
        ys = self._pdf(xs)
        ax.plot(xs, ys, drawstyle=ds, lw=1)
        ax.set(title='Probability density', xlabel='Loss')
        yl = ax.get_ylim()

        ax = axd['B']
        ys2 = self._pdf(xs2)
        ax.plot(xs2, ys2, drawstyle=ds, lw=1)
        ax.set(title='Log density', xlabel='Loss', yscale='log', ylim=[1e-14, 2 * yl[1]])

        ax = axd['C']
        ys = self._cdf(xs)
        ax.plot(xs, ys, drawstyle=ds, lw=1)
        ax.set(title='Probability distribution', xlabel='Loss', ylim=[-0.025, 1.025])

        ax = axd['D']
        ax.plot(ys, xs, drawstyle=ds, lw=1)
        ax.set(title='Quantile (Lee) plot', xlabel='Non-exceeding probability p (or ω)', xlim=[-0.025, 1.025])


class CarefulInverse(object):
    """
    from SRM_Examples Noise: careful inverse functions

    """

    @staticmethod
    def make1d(xs, ys, agg_fun=None, kind='linear', **kwargs):
        """
        Wrapper to make a reasonable 1d interpolation function with reasonable extrapolation
        Does NOT handle inverse functions, for those use dist_inv1d
        :param xs:
        :param ys:
        :param agg_fun:
        :param kind:
        :param kwargs:
        :return:
        """
        temp = pd.DataFrame(dict(x=xs, y=ys))
        if agg_fun:
            temp = temp.groupby('x').agg(agg_fun)
            fill_value = ((temp.y.iloc[0]), (temp.y.iloc[-1]))
            f = interpolate.interp1d(temp.index, temp.y, kind=kind, bounds_error=False, fill_value=fill_value, **kwargs)
        else:
            fill_value = ((temp.y.iloc[0]), (temp.y.iloc[-1]))
            f = interpolate.interp1d(temp.x, temp.y, kind=kind, bounds_error=False, fill_value=fill_value, **kwargs)
        return f

    @staticmethod
    def dist_inv1d(xs, fx, kind='linear', max_Fx=1.):
        """
        from SRM_Examples Noise
        Careful inverse of distribution function with jumps. Assumes xs is evenly spaced.
        Assumes that if there are two or more xs values between changes in dist it is a jump,
        otherwise is is a continuous part. Puts in -eps values to make steps around jumps.
        :param xs:
        :param fx:  density
        :param kind:
        :param max_Fx: what is the max allowable value of F(x)?
        """

        # make dataframe to allow summarization
        df = pd.DataFrame(dict(x=xs, fx=fx))
        # lots of problems with noise...strip it off
        df['fx'] = np.where(np.abs(df.fx) < 1e-16, 0, df.fx)
        # compute cumulative probabilities
        df['Fx'] = df.fx.cumsum()
        gs = df.groupby('Fx').agg({'x': [np.min, np.max, len]})
        gs.columns = ['mn', 'mx', 'n']
        # figure if a jump or not
        gs['jump'] = 0
        gs.loc[gs.n > 1, 'jump'] = 1
        gs = gs.reset_index(drop=False)
        # figure the right hand end of the jump
        gs['nextFx'] = gs.Fx.shift(-1, fill_value=1)

        # space for answer
        ans = np.zeros((2 * len(gs), 2))
        rn = 0
        eps = 1e-10
        max_Fx -= eps / 100
        # write out known (x, y) points for lin interp
        for n, r in gs.iterrows():
            ans[rn, 0] = r.Fx
            ans[rn, 1] = r.mn if r.Fx >= max_Fx else r.mx
            rn += 1
            if r.Fx >= max_Fx:
                break
            if r.jump:
                if r.nextFx >= max_Fx:
                    break
                ans[rn, 0] = r.nextFx - eps
                ans[rn, 1] = r.mx
                rn += 1
        # trim up ans
        ans = ans[:rn, :]

        # make interpolation function and return
        fv = ((ans[0, 1]), (ans[-1, 1]))
        ff = interpolate.interp1d(ans[:, 0], ans[:, 1], bounds_error=False, fill_value=fv, kind=kind)
        # df = input in data frame; gs = grouped df, ans = carefully selected points for inverse
        return ff
