"""Tools for working with FVC images
"""
import numpy as np

import skimage.exposure


def measure_subtract_bias(D, plot=False):
    """Measure and subtract the mean bias (and dark current) in each half image.

    The measurement uses the maximum of a histogram of pixel values,
    assuming that most pixels are not illuminated to a good approximation.

    Parameters
    ----------
    D : array
        2D array of FVC raw pixel values, normally 6000x6000 uint16 but this
        is not assumed.
    plot : bool
        When true, plot the histograms used to measure the "zero" level in
        each half of the image.

    Returns
    -------
    array
        2D numpy array of float32 values. Note that the output array data
        type (float32) is generally different from the input type (uint16)
        so this operation cannot be performed in place.
    """
    ny,nx = D.shape
    L, R = D[:,:nx//2], D[:, nx//2:]
    result = np.array(D, np.float32)
    bias = []
    for LR,label in zip((L,R),('left','right')):
        lo, hi = np.percentile(LR[LR>0], (0.1, 65))
        lo = np.floor(lo)
        hi = np.ceil(hi)
        bins = np.arange(lo-0.5, hi+0.5)
        midpt = 0.5*(bins[1:] + bins[:-1])
        hist,_ = np.histogram(LR.reshape(-1), bins=bins)
        LR0 = midpt[np.argmax(hist)]
        bias.append(LR0)
        if plot:
            import matplotlib.pyplot as plt
            plt.plot(midpt, hist, label='f{label}={bias[-1]:.1f}')
            plt.axvline(LR0, c='k', ls='--')
    if plot:
        plt.yscale('log')
        plt.legend()
    result[:,:nx//2] -= bias[0]
    result[:,nx//2:] -= bias[1]
    return result


def process_front_illuminated(D):
    """Perform bias & dark currrent subtract and perform local contrast
    enhancement to compensate for the non-uniform front illumination.
    """
    D = measure_subtract_bias(D, plot=False)
    # Normalize to 0-1 in place.
    lo, hi = np.min(D), np.max(D)
    D -= lo
    D /= hi - lo
    # Perform local contrast enhancement.
    return skimage.exposure.equalize_adapthist(D, clip_limit=0.005, nbins=1<<18)
