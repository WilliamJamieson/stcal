"""Ramp fitting routines.

The simulator need not actually fit any ramps, but we would like to do a good
job simulating the noise induced by ramp fitting.  That requires computing the
covariance matrix coming out of ramp fitting.  But that's actually a big part
of the work of ramp fitting.

There are a few different proposed ramp fitting algorithms, differing in their
weights.  The final derived covariances are all somewhat similarly difficult
to compute, however, since we ultimately end up needing to compute

.. math:: (A^T C^{-1} A)^{-1}

for the "optimal" case, or

.. math:: (A^T W^{-1} A)^{-1} A^T W^{-1} C W^{-1} A (A^T W^{-1} A)^{-1}

for some alternative weighting.

We start trying the "optimal" case below.

For the "optimal" case, a challenge is that we don't want to compute
:math:`C^{-1}` for every pixel individually.  Fortunately, we only
need :math:`(A^T C^{-1} A)^{-1}` (which is only a 2x2 matrix) for variances,
and only :math:`(A^T C^{-1} A)^{-1} A^T C^{-1}` for ramp fitting, which is 2xn.
Both of these matrices are effectively single parameter families, depending
after rescaling by the read noise only on the ratio of the read noise and flux.

So the routines in these packages construct these different matrices, store
them, and interpolate between them for different different fluxes and ratios.
"""
from enum import Enum
from typing import NamedTuple

import numpy as np
from astropy import units as u

from ._ols_cas22 import FixedOffsets, Parameter, PixelOffsets, Variance
from ._ols_cas22 import fit_ramps as _fit_ramps

__all__ = ["fit_ramps", "Parameter", "Variance", "DefaultThreshold", "RampFitOutputs"]


class DefaultThreshold(Enum):
    INTERCEPT = np.float32(5.5)
    CONSTANT = np.float32(1 / 3)


class RampFitOutputs(NamedTuple):
    """
    Simple tuple wrapper for outputs from the ramp fitting algorithm
        This clarifies the meaning of the outputs via naming them something
        descriptive.

    Attributes
    ----------
        parameters: np.ndarray[n_pixel, 2]
            the slope and intercept for each pixel's ramp fit. see Parameter enum
            for indexing indicating slope/intercept in the second dimension.
        variances: np.ndarray[n_pixel, 3]
            the read, poisson, and total variances for each pixel's ramp fit.
            see Variance enum for indexing indicating read/poisson/total in the
            second dimension.
        dq: np.ndarray[n_resultants, n_pixel]
            the dq array, with additional flags set for jumps detected by the
            jump detection algorithm.
        fits: list of RampFits
            the raw ramp fit outputs, these are all structs which will get mapped to
            python dictionaries.
    """

    parameters: np.ndarray
    variances: np.ndarray
    dq: np.ndarray


def fit_ramps(
    resultants,
    dq,
    read_noise,
    read_time,
    read_pattern,
    use_jump=False,
    *,
    threshold_intercept=DefaultThreshold.INTERCEPT.value,
    threshold_constant=DefaultThreshold.CONSTANT.value,
    include_diagnostic=False,
):
    """Fit ramps following Casertano+2022, including averaging partial ramps.

    Ramps are broken where dq != 0, and fits are performed on each sub-ramp.
    Resultants containing multiple ramps have their ramp fits averaged using
    inverse variance weights based on the variance in the individual slope fits
    due to read noise.

    Parameters
    ----------
    resultants : np.ndarry[nresultants, ...]
        the resultants in electrons
    dq : np.ndarry[nresultants, ...]
        the dq array.  dq != 0 implies bad pixel / CR.
    read_noise : float
        the read noise in electrons
    read_time : float
        Read time. For Roman data this is the FRAME_TIME keyword.
    read_pattern : list[list[int]]
        The read pattern prescription. If None, use `ma_table`.
        One of `ma_table` or `read_pattern` must be defined.
    use_jump : bool
        If True, use the jump detection algorithm to identify CRs.
        If False, use the DQ array to identify CRs.
    threshold_intercept : float (optional, keyword-only)
        Override the intercept parameter for threshold for the jump detection
        algorithm.
    theshold_constant : float (optional, keyword-only)
        Override the constant parameter for threshold for the jump detection
        algorithm.

    Returns
    -------
    RampFitOutputs
        parameters: np.ndarray[n_pixel, 2]
            the slope and intercept for each pixel's ramp fit. see Parameter enum
            for indexing indicating slope/intercept in the second dimension.
        variances: np.ndarray[n_pixel, 3]
            the read, poisson, and total variances for each pixel's ramp fit.
            see Variance enum for indexing indicating read/poisson/total in the
            second dimension.
        dq: np.ndarray[n_resultants, n_pixel]
            the dq array, with additional flags set for jumps detected by the
            jump detection algorithm.
        fits: always None, this is a hold over which can contain the diagnostic
            fit information from the jump detection algorithm.
    """
    # Trickery to avoid having to specify the defaults for the threshold
    #   parameters outside the cython code.
    resultants_unit = getattr(resultants, "unit", None)
    if resultants_unit is not None:
        resultants = resultants.to(u.electron).value

    resultants = np.array(resultants).astype(np.float32)

    dq = np.array(dq).astype(np.int32)
    if np.ndim(read_noise) <= 1:
        read_noise = read_noise * np.ones(resultants.shape[1:])
    read_noise = np.array(read_noise).astype(np.float32)

    # Raise error if input data is inconsistent
    n_resultants = resultants.shape[0]
    if n_resultants != len(read_pattern):
        msg = (
            f"The read pattern length {len(read_pattern)} does "
            f"not match number of resultants {n_resultants}"
        )
        raise RuntimeError(msg)

    orig_shape = resultants.shape
    if len(resultants.shape) == 1:
        # single ramp.
        resultants = resultants.reshape((*orig_shape, 1))
        dq = dq.reshape((*orig_shape, 1))
        read_noise = read_noise.reshape(orig_shape[1:] + (1,))

    # Pre-allocate the output arrays
    n_pixels = np.prod(resultants.shape[1:])
    parameters = np.empty((n_pixels, Parameter.n_param), dtype=np.float32)
    variances = np.empty((n_pixels, Variance.n_var), dtype=np.float32)

    # Pre-allocate the working memory arrays
    #   This prevents bouncing to and from cython for this allocation, which
    #   is slower than just doing it all in python to start.
    t_bar, tau, n_reads = _create_metadata(read_pattern, read_time)
    if use_jump:
        single_pixel = np.empty((PixelOffsets.n_pixel_offsets, n_resultants - 1), dtype=np.float32)
        double_pixel = np.empty((PixelOffsets.n_pixel_offsets, n_resultants - 2), dtype=np.float32)
        single_fixed = np.empty((FixedOffsets.n_fixed_offsets, n_resultants - 1), dtype=np.float32)
        double_fixed = np.empty((FixedOffsets.n_fixed_offsets, n_resultants - 2), dtype=np.float32)
    else:
        single_pixel = np.empty((0, 0), dtype=np.float32)
        double_pixel = np.empty((0, 0), dtype=np.float32)
        single_fixed = np.empty((0, 0), dtype=np.float32)
        double_fixed = np.empty((0, 0), dtype=np.float32)

    _fit_ramps(
        resultants.reshape(resultants.shape[0], -1),
        dq.reshape(resultants.shape[0], -1),
        read_noise.reshape(-1),
        parameters,
        variances,
        t_bar,
        tau,
        n_reads,
        single_pixel,
        double_pixel,
        single_fixed,
        double_fixed,
        use_jump,
        threshold_intercept,
        threshold_constant,
        include_diagnostic,
    )

    parameters = parameters.reshape(orig_shape[1:] + (2,))
    variances = variances.reshape(orig_shape[1:] + (3,))
    dq = dq.reshape(orig_shape)

    if resultants.shape != orig_shape:
        parameters = parameters[0]
        variances = variances[0]

    if resultants_unit is not None:
        parameters = parameters * resultants_unit

    # return ols_cas22.RampFitOutputs(output.fits, parameters, variances, dq)
    return RampFitOutputs(parameters, variances, dq)


def _create_metadata(read_pattern, read_time):
    n_resultants = len(read_pattern)

    t_bar = np.empty(n_resultants, dtype=np.float32)
    tau = np.empty(n_resultants, dtype=np.float32)
    n_reads = np.empty(n_resultants, dtype=np.int32)

    for i, resultant in enumerate(read_pattern):
        n_read = len(resultant)

        n_reads[i] = n_read
        t_bar[i] = read_time * np.mean(resultant)
        tau[i] = np.sum((2 * (n_read - np.arange(n_read)) - 1) * resultant) * read_time / n_read**2

    return t_bar, tau, n_reads