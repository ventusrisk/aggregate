import scipy.stats as ss
import numpy as np
from scipy.integrate import quad
import pandas as pd
import collections
import logging
from .utils import sln_fit, sgamma_fit, ft, ift, \
    axiter_factory, estimate_agg_percentile, suptitle_and_tight, html_title, \
    MomentAggregator, xsden_to_meancv
from .spectral import Distortion
from scipy import interpolate
from scipy.optimize import newton
from IPython.core.display import display
from scipy.special import kv, gammaln, hyp1f1
from scipy.optimize import broyden2, newton_krylov
from scipy.optimize.nonlin import NoConvergence
import itertools


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
    * ``sichel.nb``: generalized inverse gaussian mixture where the parameters match the moments of a
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


        """
        self.freq_name = freq_name
        self.freq_a = freq_a
        self.freq_b = freq_b

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
                        logging.warning(
                            f'Frequency.__init__ | {self.freq_name} type Broyden gave large result {params},'
                            f'Newton Krylov {params1}')
                        if np.linalg.norm(params) > np.linalg.norm(params1):
                            params = params1
                            logging.warning('Frequency.__init__ | using Newton K')
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
            logging.info(f'{self.freq_name} type, params from Broyden {params}')
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


class Aggregate(Frequency):
    """
    Aggregate distribution class manages creation and calculation of aggregate distributions.
        Aggregate allows for very flexible creation of Aggregate distributions. Severity
        can express a limit profile, a mixed severity or both. Mixed frequency types share
        a mixing distribution across all broadcast terms to ensure an appropriate inter-
        class correlation.

    Limit Profiles
        The exposure variables can be vectors to express a *limit profile*.
        All exp_ elements are broadcast against one-another. For example

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
        off reserve, where the current year is lognormal mean 100 cv 1 and the reserves are
        larger lognormal mean 150 cv 1.25 claims requires

        ::

            agg prem_reserve [100 200] claims sev lognorm [100 150] cv [1 1.25]

        so that the result is not the four-way exposure / severity product but just a two-way
        combination. These two cases are distinguished looking at the total weights. If the weights sum to
        one then the result is an exposure / severity product. If the weights are missing or sum to the number
        of severity components (i.e. are all equal to 1) then the result is a row by row combination.

    Other notes
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

    def __init__(self, name, exp_el=0, exp_premium=0, exp_lr=0, exp_en=0, exp_attachment=0, exp_limit=np.inf,
                 sev_name='', sev_a=0, sev_b=0, sev_mean=0, sev_cv=0, sev_loc=0, sev_scale=0,
                 sev_xs=None, sev_ps=None, sev_wt=1,
                 freq_name='', freq_a=0, freq_b=0, note=''):

        # assert np.allclose(np.sum(sev_wt), 1)

        # have to be ready for inputs to be in a list, e.g. comes that way from Pandas via Excel
        def get_value(v):
            if isinstance(v, list):
                return v[0]
            else:
                return v

        # class variables
        self.name = get_value(name)
        Frequency.__init__(self, get_value(freq_name), get_value(freq_a), get_value(freq_b))
        self.note = note
        self.sev_density = None
        self.ftagg_density = None
        self.agg_density = None
        self.xs = None
        self.fzapprox = None
        self.agg_m, self.agg_cv, self.agg_skew = 0, 0, 0
        self._linear_quantile_function = None
        self._cdf = None
        self._pdf = None
        self.density_df = None
        self.en = None  # this is for a sublayer e.g. for limit profile
        self.n = 0  # this is total frequency
        self.attachment = None
        self.limit = None
        self.sev_density = None
        self.fzapprox = None
        self.agg_density = None
        self.ftagg_density = None
        self.xs = None
        self.bs = 0
        self.log2 = 0
        self.dh_agg_density = None
        self.dh_sev_density = None
        self.beta_name = ''  # name of the beta function used to create dh distortion
        self.sevs = None
        self.audit_df = None
        self.statistics_df = pd.DataFrame(columns=['name', 'limit', 'attachment', 'sevcv_param', 'el', 'prem', 'lr'] +
                                                  MomentAggregator.column_names() +
                                                  ['mix_cv'])
        self.statistics_total_df = self.statistics_df.copy()
        ma = MomentAggregator(self.freq_moms)

        # broadcast arrays: force answers all to be arrays
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
            logging.info(f'Aggregate.__init__ | Broadcast/align: exposures + severity = {len(exp_el)} exp = '
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
            logging.info(f'Aggregate.__init__ | Broadcast/product: exposures x severity = {len(exp_arrays)} x {len(sev_arrays)} '
                         f'=  {n_components}')
            self.sevs = np.empty(n_components, dtype=type(Severity))

        # overall freq CV with common mixing TODO this is dubious
        mix_cv = self.freq_a
        # counter to label components
        r = 0
        # perform looping creation of severity distribution
        for _el, _pr, _lr, _en, _at, _y, _sn, _sa, _sb, _sm, _scv, _sloc, _ssc, _swt in all_arrays:

            # XXXX TODO WARNING: note sev_xs and sev_ps are NOT broadcast
            self.sevs[r] = Severity(_sn, _at, _y, _sm, _scv, _sa, _sb, _sloc, _ssc, sev_xs, sev_ps, True)
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

    def __str__(self):
        """
        Goal: readability

        :return:
        """
        # pull out agg statistics_df
        ags = self.statistics_total_df.loc['mixed', :]
        s = f"Aggregate: {self.name}\n\tEN={ags['freq_1']}, CV(N)={ags['freq_cv']:5.3f}\n\t" \
            f"{len(self.sevs)} severit{'ies' if len(self.sevs)>1 else 'y'}, EX={ags['sev_1']:,.1f}, ' " \
            f"CV(X)={ags['sev_cv']:5.3f}\n\t" \
            f"EA={ags['agg_1']:,.1f}, CV={ags['agg_cv']:5.3f}"
        return s

    def _repr_html_(self):
        s = [f'<h3>Aggregate object: {self.name}</h3>']
        s.append(f'Claim count {self.n:0,.2f}, {self.freq_name} distribution<br>')
        _n = len(self.statistics_df)
        if _n == 1:
            sv = self.sevs[0]
            if sv.limit == np.inf and sv.attachment == 0:
                _la = 'unlimited'
            else:
                _la = f'{sv.limit} xs {sv.attachment}'
            s.append(f'Severity{sv.long_name} distribution, {_la}<br>')
        else:
            s.append(f'Severity with {_n} components<br>')
        if self.bs > 0:
            s.append(f'Updated with bucket size {self.bs:.2f} and log2 = {self.log2}')
        st = self.statistics_total_df.loc['mixed', :]
        sev_m = st.sev_m
        sev_cv = st.sev_cv
        sev_skew = st.sev_skew
        n_m = st.freq_m
        n_cv = st.freq_cv
        a_m = st.agg_m
        a_cv = st.agg_cv
        _df = pd.DataFrame({'E(X)': [sev_m, n_m, a_m], 'CV(X)': [sev_cv, n_cv, a_cv],
                            'Skew(X)': [sev_skew, self.statistics_total_df.loc['mixed', 'freq_skew'], st.agg_skew]},
                           index=['Sev', 'Freq', 'Agg'])
        _df.index.name = 'X'
        if self.audit_df is not None:
            esev_m = self.audit_df.loc['mixed', 'emp_sev_1']
            esev_cv = self.audit_df.loc['mixed', 'emp_sev_cv']
            ea_m = self.audit_df.loc['mixed', 'emp_agg_1']
            ea_cv = self.audit_df.loc['mixed', 'emp_agg_cv']
            _df.loc['Sev', 'Est E(X)'] = esev_m
            _df.loc['Agg', 'Est E(X)'] = ea_m
            _df.loc[:, 'Err E(X)'] = _df['Est E(X)'] / _df['E(X)'] - 1
            _df.loc['Sev', 'Est CV(X)'] = esev_cv
            _df.loc['Agg', 'Est CV(X)'] = ea_cv
            _df.loc[:, 'Err CV(X)'] = _df['Est CV(X)'] / _df['CV(X)'] - 1
            _df = _df[['E(X)', 'Est E(X)', 'Err E(X)', 'CV(X)', 'Est CV(X)', 'Err CV(X)', 'Skew(X)']]
        _df = _df.fillna('')
        return '\n'.join(s) + _df._repr_html_()

    # def __repr__(self):
    #     """
    #     Goal unmbiguous
    #     :return: MUST return a string
    #     """
    #     return str(self.spec)

    def discretize(self, sev_calc, discretization_calc='survival'):
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

        :param sev_calc:  continuous or discrete or raw (for...)
        :param discretization_calc:  survival, distribution or both
        :return:
        """

        if sev_calc == 'continuous':
            # adj_xs = np.hstack((self.xs, np.inf))  # undesirable mass at the end
            adj_xs = np.hstack((self.xs, self.xs[-1] + self.bs))
        elif sev_calc == 'discrete':
            # adj_xs = np.hstack((self.xs - self.bs / 2, np.inf))  # as above
            adj_xs = np.hstack((self.xs - self.bs / 2, self.xs[-1] + self.bs / 2))
        elif sev_calc == 'raw':
            adj_xs = self.xs
        else:
            raise ValueError(
                f'Invalid parameter {sev_calc} passed to double_diff; options are raw, discrete or histogram')

        # bed = bucketed empirical distribution
        beds = []
        for fz in self.sevs:
            if discretization_calc == 'both':
                # see comments: we rescale each severity...
                appx = np.maximum(np.diff(fz.cdf(adj_xs)), -np.diff(fz.sf(adj_xs)))
                beds.append(appx / np.sum(appx))
            elif discretization_calc == 'survival':
                appx = -np.diff(fz.sf(adj_xs))
                beds.append(appx / np.sum(appx))
            elif discretization_calc == 'distribution':
                appx = np.diff(fz.cdf(adj_xs))
                beds.append(appx / np.sum(appx))
            else:
                raise ValueError(
                    f'Invalid options {discretization_calc} to double_diff; options are density, survival or both')

        return beds

    def easy_update(self, log2=13, bs=0, **kwargs):
        """
        Convenience function, delegates to update. Avoids having to pass xs.


        :param log2:
        :param bs:
        :param reporting_level:
        :param kwargs:  passed through to update
        :return:
        """
        # guess bucket and update
        if bs == 0:
            bs = self.recommend_bucket(log2)
        xs = np.arange(0, 1 << log2, dtype=float) * bs
        if 'approximation' not in kwargs:
            if self.n > 100:
                kwargs['approximation'] = 'slognorm'
            else:
                kwargs['approximation'] = 'exact'
        return self.update(xs, **kwargs)

    def update(self, xs, padding=1, tilt_vector=None, approximation='exact', sev_calc='discrete',
               discretization_calc='survival', force_severity=False, verbose=False):
        """
        Compute the density

        :param xs:  range of x values used to discretize
        :param padding: for FFT calculation
        :param tilt_vector:
        :param approximation: exact = perform frequency / severity convolution using FFTs. slognorm or
                sgamma apply shifted lognormal or shifted gamma approximations.
        :param sev_calc:   discrete = suitable for fft, continuous = for rv_histogram cts version
        :param discretization_calc: use survival, distribution or both (=max(cdf, sf)) which is most accurate calc
        :param force_severity: make severities even if using approximation, for plotting
        :param verbose: make partial plots and return details of all moments by limit profile or
                severity mixture component.
        :return:
        """
        axiter = None
        verbose_audit_df = None
        if verbose:
            axiter = axiter_factory(None, len(self.sevs) + 2, aspect=1.414)
            verbose_audit_df = pd.DataFrame(
                columns=['n', 'limit', 'attachment', 'en', 'emp ex1', 'emp cv', 'sum p_i', 'wt', 'nans', 'max', 'wtmax',
                         'min'])
            verbose_audit_df = verbose_audit_df.set_index('n')

        r = 0
        self.xs = xs
        self.bs = xs[1]
        self.log2 = int(np.log(len(xs)) / np.log(2))

        # make the severity vector: a claim count weighted average of the severities
        if approximation == 'exact' or force_severity:
            wts = self.statistics_df.freq_1 / self.statistics_df.freq_1.sum()
            self.sev_density = np.zeros_like(xs)
            beds = self.discretize(sev_calc, discretization_calc)
            for temp, w, a, l, n in zip(beds, wts, self.attachment, self.limit, self.en):
                self.sev_density += temp * w
                if verbose:
                    _m, _cv = xsden_to_meancv(xs, temp)
                    verbose_audit_df.loc[r, :] = [l, a, n, _m, _cv,
                                                  temp.sum(),
                                                  w, np.sum(np.where(np.isinf(temp), 1, 0)),
                                                  temp.max(), w * temp.max(), temp.min()]
                    r += 1
                    next(axiter).plot(xs, temp, label='compt', lw=0.5, drawstyle='steps-post')
                    axiter.ax.plot(xs, self.sev_density, label='run tot', lw=0.5, drawstyle='steps-post')
                    if np.all(self.limit < np.inf):
                        axiter.ax.set(xlim=(0, np.max(self.limit) * 1.1), title=f'{l:,.0f} xs {a:,.0f}, wt={w:.2f}')
                    else:
                        axiter.ax.set(title=f'{l:,.0f} xs {a:,.0f}, wt={w:.2f}')
                    axiter.ax.legend()
            if verbose:
                next(axiter).plot(xs, self.sev_density, lw=0.5, drawstyle='steps-post')
                axiter.ax.set_title('Occurrence Density')
                aa = float(np.sum(verbose_audit_df.attachment * verbose_audit_df.wt))
                al = float(np.sum(verbose_audit_df.limit * verbose_audit_df.wt))
                if np.all(self.limit < np.inf):
                    axiter.ax.set_xlim(0, np.max(self.limit) * 1.05)
                else:
                    axiter.ax.set_xlim(0, xs[-1])
                _m, _cv = xsden_to_meancv(xs, self.sev_density)
                # TODO fix issue with Occ ==> sort key error>>
                verbose_audit_df.loc[10001, :] = [al, aa, self.n, _m, _cv,
                                                  self.sev_density.sum(),
                                                  np.sum(wts), np.sum(np.where(np.isinf(self.sev_density), 1, 0)),
                                                  self.sev_density.max(), np.nan, self.sev_density.min()]
        if force_severity:
            # only asking for severity (used by plot)
            return

        if approximation == 'exact':
            if self.n > 100:
                logging.warning(f'Aggregate.update | warning, claim count {self.n} is high; consider an approximation ')
            # assert self.n < 100
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

        # make a suitable audit_df
        cols = ['name', 'limit', 'attachment', 'el', 'freq_1', 'sev_1', 'agg_m', 'agg_cv', 'agg_skew']
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
        self.audit_df.loc['mixed', 'emp_agg_cv'] = _cv

        if verbose:
            ax = next(axiter)
            ax.plot(xs, self.agg_density, 'b')
            ax.set(xlim=(0, self.agg_m * (1 + 5 * self.agg_cv)), title='Aggregate Density')
            suptitle_and_tight(f'Severity Audit For: {self.name}')
            verbose_audit_df = pd.concat((verbose_audit_df[['limit', 'attachment', 'emp ex1', 'emp cv']],
                                          self.statistics_df[['freq_1', 'sev_1', 'sev_cv']]),
                                         sort=True, axis=1)
            verbose_audit_df.loc[10001, 'freq_1'] = self.statistics_total_df.loc['mixed', 'freq_1']
            verbose_audit_df.loc[:, 'freq_cv'] = np.hstack((self.statistics_df.loc[:, 'freq_cv'],
                                                            self.statistics_total_df.loc['mixed', 'freq_cv']))
            verbose_audit_df.loc[10001, 'sev_1'] = self.statistics_total_df.loc['mixed', 'sev_1']
            verbose_audit_df.loc[10001, 'sev_cv'] = self.statistics_total_df.loc['mixed', 'sev_cv']
            verbose_audit_df['abs sev err'] = verbose_audit_df.sev_1 - verbose_audit_df['emp ex1']
            verbose_audit_df['rel sev err'] = verbose_audit_df['abs sev err'] / verbose_audit_df['emp ex1']

        # invalidate stored functions
        self.nearest_quantile_function = None
        self._cdf = None
        return verbose_audit_df

    def emp_stats(self):
        """
        report_ser on empirical statistics_df - useful when investigating dh transformations.

        :return:
        """

        ex = np.sum(self.xs * self.agg_density)
        ex2 = np.sum(self.xs ** 2 * self.agg_density)
        # ex3 = np.sum(self.xs**3 * self.agg_density)
        v = ex2 - ex * ex
        sd = np.sqrt(v)
        cv = sd / ex
        s1 = pd.Series([ex, sd, cv], index=['mean', 'sd', 'cv'])
        if self.dh_sev_density is not None:
            ex = np.sum(self.xs * self.dh_agg_density)
            ex2 = np.sum(self.xs ** 2 * self.dh_agg_density)
            # ex3 = np.sum(self.xs**3 * self.dh_agg_density)
            v = ex2 - ex * ex
            sd = np.sqrt(v)
            cv = sd / ex
            s2 = pd.Series([ex, sd, cv], index=['mean', 'sd', 'cv'])
            df = pd.DataFrame({'numeric': s1, self.beta_name: s2})
        else:
            df = pd.DataFrame(s1, columns=['numeric'])
        df.loc['mean', 'theory'] = self.statistics_total_df.loc['Agg', 'agg1']
        df.loc['sd', 'theory'] = np.sqrt(self.statistics_total_df.loc['Agg', 'agg2'] -
                                         self.statistics_total_df.loc['Agg', 'agg1'] ** 2)
        df.loc['cv', 'theory'] = self.statistics_total_df.loc['Agg', 'agg_cv']  # report_ser[('agg', 'cv')]
        df['err'] = df['numeric'] / df['theory'] - 1
        return df

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

    def plot(self, kind='quick', axiter=None, aspect=1, figsize=None):
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
            print('Cannot plot before update')
            return
        if self.sev_density is None:
            self.update(self.xs, 1, None, sev_calc='discrete', force_severity=True)

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

        else:  # kind == 'quick':
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

    def report(self, report_list='quick'):
        """
        statistics, quick or audit reports


        :param report_list:
        :return:
        """
        full_report_list = ['statistics', 'quick', 'audit']
        if report_list == 'all':
            report_list = full_report_list

        if 'quick' in report_list:
            html_title(f'{self.name} Quick Report (Theoretic)', 1)
            display(pd.DataFrame(self.report_ser).unstack())

        if 'audit' in report_list:
            if self.audit_df is not None:
                html_title(f'{self.name} Audit Report', 1)
                display(self.audit_df)

        if 'statistics' in report_list:
            if len(self.statistics_df) > 1:
                df = pd.concat((self.statistics_df, self.statistics_total_df), axis=1)
            else:
                df = self.statistics_df
            html_title(f'{self.name} Statistics Report', 1)
            display(df)

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
            logging.info(f'Agg.recommend_bucket | {self.name} moment: {moment_est}, limit {limit_est}')
            return max(moment_est, limit_est)
        else:
            for n in sorted({log2, 16, 13, 10}):
                rb = self.recommend_bucket(n)
                if n == log2:
                    rbr = rb
                print(f'Recommended bucket size with {2**n} buckets: {rb:,.0f}')
            if self.bs != 0:
                print(f'Bucket size set with {N} buckets at {self.bs:,.0f}')
            return rbr

    def q(self, p):
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
        if self._linear_quantile_function is None:
            # really should have one of these anyway...
            self.density_df = pd.DataFrame(dict(loss=self.xs, p=self.agg_density))
            self.density_df = self.density_df.set_index('loss', drop=False)
            self.density_df['F'] = self.density_df.p.cumsum()

        q = interpolate.interp1d(self.density_df.F, self.density_df.loss, kind='linear')
        l = float(q(p))
        # find next nearest index value if not an exact match (this is slightly faster and more robust
        # than l/bs related math)
        l1 = self.density_df.index.get_loc(l, 'bfill')
        l1 = self.density_df.index[l1]
        return l1

    def var(self, p):
        """
        value at risk = alias for quantile function

        :param p:
        :return:
        """
        return self.q(p)

    def tvar(self, p):
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


        :param p:
        :return:
        """
        if self.density_df is None:
            # force recomputation
            throwaway = self.q(p)

        _var = self.q(p)
        # evil floating point issue here... this is XXXX TODO kludge because 13 is not generally applicable
        ex = self.density_df.loc[np.round(_var + self.bs, 13):, ['p', 'loss']].product(axis=1).sum()
        pip = (self.density_df.loc[_var, 'F'] - p) * _var
        t_var = 1 / (1 - p) * (ex + pip)
        return t_var

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

    # def quantile_function(self, kind='nearest'):
    #     """
    #     return an approximation to the quantile function
    #
    #     :param kind: ```interpolate.interp1d``` kind variable
    #     :return:
    #     """
    #     q = interpolate.interp1d(self.agg_density.cumsum(), self.xs, kind=kind,
    #                              bounds_error=False, fill_value='extrapolate')
    #     return q


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
    infinite support and a low standard deviation. See logging for more information in particular
    cases.

    """

    def __init__(self, sev_name, exp_attachment=0, exp_limit=np.inf, sev_mean=0, sev_cv=0, sev_a=0, sev_b=0,
                 sev_loc=0, sev_scale=0, sev_xs=None, sev_ps=None, conditional=True, name='', note=''):
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
        :param conditional: conditional or unconditional; for severities use conditional
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
        self.conditional = conditional
        self.sev_name = sev_name
        self.name = name
        self.long_name = f'{sev_name}[{exp_limit} xs {exp_attachment:,.0f}'
        self.note = note
        self.sev1 = self.sev2 = self.sev3 = None

        # there are two types: if sev_xs and sev_ps provided then fixed/histogram, else scpiy dist
        # allows you to define fixed with just xs=1 (no log)
        if sev_xs is not None:
            if sev_name == 'fixed':
                # fixed is a special case of dhistogram with just one point
                sev_name = 'dhistogram'
                sev_ps = np.array(1)
            assert sev_name[1:] == 'histogram'
            # TODO: make histogram work with exp_limit and exp_attachment; currently they are ignored
            xs, ps = np.broadcast_arrays(np.array(sev_xs), np.array(sev_ps))
            if not np.isclose(np.sum(ps), 1.0):
                logging.error(f'Severity.init | {sev_name} histogram/fixed severity with probs do not sum to 1, '
                              f'{np.sum(ps)}')
            # need to exp_limit distribution
            exp_limit = min(np.min(exp_limit), xs.max())
            if sev_name == 'chistogram':
                # continuous histogram: uniform between xs's
                # if the inputs are not evenly spaced this messes up because it interprets p as the
                #  height of the density over the range...hence have to rescale
                #  it DOES NOT matter that the p's add up to 1...that is handled automatically
                # changed 1 to -2 so the last bucket is bigger WHY SORTED???
                xss = np.sort(np.hstack((xs, xs[-1] + xs[-2])))
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
                xss = np.sort(np.hstack((xs - 1e-5, xs)))  # was + but F(x) = Pr(X<=x) so seems shd be to left
                pss = np.vstack((ps, np.zeros_like(ps))).reshape((-1,), order='F')[:-1]
                self.fz = ss.rv_histogram((pss, xss))
            else:
                raise ValueError('Histogram must be chistogram (continuous) or dhistogram (discrete)'
                                 f', you passed {sev_name}')

        elif isinstance(sev_name, Severity):
            self.fz = sev_name

        elif not isinstance(sev_name, (str, np.str_)):
            # must be a meta object - replaced in Undewriter.write
            log2 = sev_a
            bs = sev_b       # if zero it is happy to take whatever....
            if isinstance(sev_name, Aggregate):
                if log2 and (log2 != sev_name.log2 or (bs != sev_name.bs and bs!=0)):
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
                raise ValueError(f'Object {sev_name} passed as a proto-severity type {type(obj)}'
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
            if sev_a == 0:  # TODO figuring 0 is invalid shape...
                sev_a, _ = self.cv_to_shape(sev_cv)
            if sev_scale == 0 and sev_mean > 0:
                sev_scale, self.fz = self.mean_to_scale(sev_a, sev_mean)
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
            if sev_mean > 0 and not np.isclose(sev_mean, m):
                print(f'WARNING target mean {sev_mean} and achieved mean {m} not close')
                # assert (np.isclose(sev_mean, m))
            if sev_cv > 0 and not np.isclose(sev_cv, acv):
                print(f'WARNING target cv {sev_cv} and achieved cv {acv} not close')
                # assert (np.isclose(sev_cv, acv))
            # print('ACHIEVED', sev_mean, sev_cv, m, acv, self.fz.statistics_df(), self._stats())
            logging.info(
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
            logging.error(f'cv_to_shape | error for {self.sev_name}, {cv}')
            ans = np.inf
            return ans, None
        fz = gen(ans)
        return ans, fz

    def mean_to_scale(self, shape, mean):
        """
        adjust scale of fz to have desired mean
        return frozen instance

        :param shape:
        :param mean:
        :return:
        """
        gen = getattr(ss, self.sev_name)
        fz = gen(shape)
        m = fz.stats('m')
        scale = mean / m
        fz = gen(shape, scale=scale)
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

    def _munp(self, n, *args):
        print('wow, called munp')
        pass

    def moms(self):
        """
        moms method
        remember have to use integral of survival function otherwise quad can fail to "see" the distribution
        for unlimited integral of a low sd variable

        :return:
        """

        # if self.sev1 is not None:
        #     # precomputed (fixed and (c|d)histogram classes
        #     # TODO ignores layer and attach...
        #     return self.sev1, self.sev2, self.sev3

        median = self.fz.isf(0.5)

        def safe_integrate(f, level):
            ex = quad(f, self.attachment, self.detachment, limit=100, full_output=1)
            if len(ex) == 4:  # 'The integral is probably divergent, or slowly convergent.':
                # TODO just wing it for now
                logging.warning(
                    f'Severity.moms | splitting {self.sev_name} EX^{level} integral for convergence reasons')
                exa = quad(f, self.attachment, median, limit=100, full_output=1)
                exb = quad(f, median, self.detachment, limit=100, full_output=1)
                if len(exa) == 4:
                    logging.warning(f'Severity.moms | left hand split EX^{level} integral returned {exa[-1]}')
                if len(exb) == 4:
                    logging.warning(f'Severity.moms | right hand split EX^{level} integral returned {exb[-1]}')
                ex = (exa[0] + exb[0], exa[1] + exb[1])
            return ex

        ex1 = safe_integrate(lambda x: self.fz.sf(x), 1)
        ex2 = safe_integrate(lambda x: 2 * (x - self.attachment) * self.fz.sf(x), 2)
        ex3 = safe_integrate(lambda x: 3 * (x - self.attachment) ** 2 * self.fz.sf(x), 3)

        # quad returns abs error
        eps = 1e-5
        if not ((ex1[1] / ex1[0] < eps or ex1[1] < 1e-4) and
                (ex2[1] / ex2[0] < eps or ex2[1] < 1e-4) and
                (ex3[1] / ex3[0] < eps or ex3[1] < 1e-6)):
            logging.info(f'Severity.moms | **DOUBTFUL** convergence of integrals, abs errs '
                         f'\t{ex1[1]}\t{ex2[1]}\t{ex3[1]} \trel errors \t{ex1[1]/ex1[0]}\t{ex2[1]/ex2[0]}\t'
                         f'{ex3[1]/ex3[0]}')
            # raise ValueError(f' Severity.moms | doubtful convergence of integrals, abs errs '
            #                  f'{ex1[1]}, {ex2[1]}, {ex3[1]} rel errors {ex1[1]/ex1[0]}, {ex2[1]/ex2[0]}, '
            #                  f'{ex3[1]/ex3[0]}')
        # logging.info(f'Severity.moms | GOOD convergence of integrals, abs errs '
        #                      f'\t{ex1[1]}\t{ex2[1]}\t{ex3[1]} \trel errors \
        #  t{ex1[1]/ex1[0]}\t{ex2[1]/ex2[0]}\t{ex3[1]/ex3[0]}')

        ex1 = ex1[0]
        ex2 = ex2[0]
        ex3 = ex3[0]

        if self.conditional:
            ex1 /= self.pattach
            ex2 /= self.pattach
            ex3 /= self.pattach

        return ex1, ex2, ex3

    def plot(self, N=100, axiter=None):
        """
        quick plot

        :param axiter:
        :param N:
        :return:
        """

        ps = np.linspace(0, 1, N, endpoint=False)
        xs = np.linspace(0, self._isf(1e-4), N)

        do_tight = axiter is None
        axiter = axiter_factory(None, 4)

        ax = next(axiter)
        ys = self._pdf(xs)
        ax.plot(xs, ys)
        ax.grid(which='major', axis='both', linestyle='-', linewidth='0.1', color='blue', alpha=0.5)
        ax.set_title('PDF')

        ys = self._cdf(xs)
        ax = next(axiter)
        ax.plot(xs, ys, drawstyle='steps-post', lw=1)
        ax.grid(which='major', axis='both', linestyle='-', linewidth='0.1', color='blue', alpha=0.5)
        ax.set(title='CDF', ylim=(0, 1))

        ys = self._isf(ps)
        ax = next(axiter)
        ax.plot(ps, ys, drawstyle='steps-post', lw=1)
        ax.grid(which='major', axis='both', linestyle='-', linewidth='0.1', color='blue', alpha=0.5)
        ax.set(title='ISF', xlim=(0, 1))

        ax = next(axiter)
        ax.plot(1 - ps, ys, drawstyle='steps-post', lw=1)
        ax.grid(which='major', axis='both', linestyle='-', linewidth='0.1', color='blue', alpha=0.5)
        ax.set(title='Lee diagram', xlim=(0, 1))

        if do_tight:
            axiter.tidy()
            suptitle_and_tight(self.long_name)


